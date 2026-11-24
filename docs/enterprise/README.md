# GemstoneAI Enterprise

**The pricing and verification layer for colored gemstones, delivered as infrastructure.**

GemstoneAI Enterprise sells five products to companies that price, accept, list or insure colored
gemstones at volume: auction houses, luxury resale platforms, marketplaces and insurers. All five run
on one engine (the V2 kNN comps engine) and one enterprise platform (tenants, scoped API keys,
audit store, metering). Each product is a separate SKU, so a customer can buy one without the rest.

| Product | What it does | First buyer | Status |
| --- | --- | --- | --- |
| **Comps** | Price band, the comparables behind it and a confidence score for any stone | Insurers, appraisal platforms, marketplaces | **API v1 built** (this document) |
| **Intake** | Triages consigned pieces into list now, gemologist review, lab or decline | Luxury resale | Planned; scope `intake` reserved |
| **Estimate** | Pre-sale estimates and reserves with documented realized comps | Auction houses | Planned; scope `estimate` reserved |
| **Verify** | Risk-scores listings and claims whose price, treatment, origin or photos don't add up | Marketplaces, insurers | Planned; scope `verify` reserved |
| **Index** | Licenses the GemstoneAI Price Index (GPI) and category price data | Auction houses, insurers, lenders | Planned; scope `index` reserved |

Comps is the core: every other product calls it. This release builds Comps v1 and the minimum
platform layer it needs.

---

## Architecture

```
 Customer system ──HTTPS──▶  /enterprise/v1  (separate FastAPI app, own OpenAPI)
                                   │
                    ┌──────────────┴──────────────┐
                    │ Auth: API key → tenant       │  per-product scopes, tenant status
                    │ Idempotency-Key handling     │
                    └──────────────┬──────────────┘
                                   ▼
                          ValuationService              enterprise rules:
                                   │                    5-comp floor, basis, confidence,
                                   │                    scenarios, model version pinning
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   RemoteCompsEngine (live tenants)          SandboxEngine (gsk_test_ keys)
   HTTP → V2 kNN engine in the backend       deterministic synthetic stones
              │
              ▼
   Postgres ent_* tables: tenants, api keys, valuations (audit), idempotency, usage, batch jobs
```

**Isolation from consumer and dealer surfaces.** The enterprise API is its own FastAPI application
(`enterprise/app.py`) with its own routes, auth, error format and OpenAPI document. Its tables are all
prefixed `ent_` and migrated by a separate Alembic history (`ent_alembic_version`), so it can share a
Postgres database with the main backend without touching its schema. To serve it from the main
backend, mount it:

```python
from enterprise.app import create_enterprise_app
backend_app.mount("/enterprise/v1", create_enterprise_app())
```

**The engine boundary.** The enterprise layer never builds comps. It asks an engine for
similarity-scored candidates, each tagged `realized` or `asking`, and applies the enterprise rules on
top. The production engine (V2) lives in the GemstoneAI backend; `enterprise/engine/remote.py`
documents the one internal endpoint it needs (`POST /comps/search`). Until that is wired up, live
tenants get `503 engine_unavailable`, never a silent fallback to synthetic data.

---

## Quickstart (local)

```bash
pip install -r requirements.txt
export ENTERPRISE_DATABASE_URL=sqlite:///./data/enterprise.db
python -m enterprise.cli init-db                      # local SQLite only; use Alembic elsewhere

python -m enterprise.cli create-tenant --name "Demo Insurer" --environment sandbox
# {"tenant_id": "ten_01M8...", ...}
python -m enterprise.cli create-key --tenant ten_01M8... --scopes comps
# {"key_id": "key_...", "api_key": "gsk_test_..."}   <- shown once

uvicorn --factory enterprise.app:create_app --reload
# Interactive docs: http://localhost:8000/enterprise/v1/docs

curl -s localhost:8000/enterprise/v1/valuations \
  -H "Authorization: Bearer gsk_test_..." \
  -H "Idempotency-Key: demo-1" \
  -H "Content-Type: application/json" \
  -d '{"variety": "blue sapphire", "carat": 2.1, "shape": "oval",
       "origin": "Sri Lanka", "valuation_basis": "retail_replacement"}'
```

Run the tests with `pytest tests/enterprise`. Set `ENTERPRISE_TEST_POSTGRES_URL` to a disposable
Postgres database to also run the integration test on Postgres.

---

## Comps v1 API contract

Base URL: `https://api.gemstoneaiapp.com/enterprise/v1`. The machine-readable contract is
[`openapi.json`](openapi.json) (regenerate with `python -m enterprise.cli export-openapi`; a test
fails if it drifts from the code).

### Authentication and scopes

Send `Authorization: Bearer <key>` (or `X-API-Key: <key>`). Keys belong to one tenant and carry
per-product scopes; Comps endpoints require the `comps` scope.

| Key prefix | Tenant environment | Engine |
| --- | --- | --- |
| `gsk_live_` | live | Production V2 engine |
| `gsk_test_` | sandbox | Synthetic stones; every response has `synthetic_data: true` |

Keys are stored as SHA-256 hashes and shown once at creation. Revoked keys and suspended tenants are
refused (`401 invalid_api_key`, `403 tenant_inactive`).

### Endpoints

| Method and path | Purpose |
| --- | --- |
| `POST /valuations` | Value one stone |
| `GET /valuations/{valuation_id}` | Retrieve a stored valuation exactly as returned (audit trail) |
| `POST /valuations/batch` | Upload a CSV; starts an async job |
| `GET /valuations/batch/{job_id}` | Job status and counts |
| `GET /valuations/batch/{job_id}/results` | Results CSV |
| `GET /models` | Model versions available for pinning, the default and your account's pin |
| `GET /usage` | Valuations per day for your account (`start_date`, `end_date`) |

### `POST /valuations` request

| Field | Required | Notes |
| --- | --- | --- |
| `variety` | yes | e.g. `blue sapphire`, `ruby`, `emerald`, `spinel`. Normalized (`Sapphire` → `blue_sapphire`). |
| `carat` | yes | > 0 |
| `shape` | yes | e.g. `oval`, `cushion` |
| `valuation_basis` | yes | `retail_replacement`, `fair_market`, `wholesale` or `auction_realized` |
| `species` | no | Inferred from variety when omitted |
| `color_grade`, `clarity` | no | Free text, normalized |
| `treatment` | no | `none`, `heated`, `oiled`, `diffused`, `filled`, `irradiated`, `other`. Synonyms accepted (`unheated`, `no heat`, `minor oil`). **Omit when unknown**: the response then includes scenario pricing. |
| `origin` | no | Omit when unknown. Aliases accepted (`Myanmar` → `burma`, `Ceylon` → `sri_lanka`). |
| `lab`, `lab_report_number` | no, together | Lab is one of `GIA`, `AGL`, `SSEF`, `Gubelin` (or `Gübelin`), `GRS` |
| `dimensions_mm` | no | `{"length", "width", "depth"?}` |
| `model_version` | no | Pin a model version for this request (overrides the account pin) |
| `client_reference` | no | Your identifier, echoed back |

Unknown fields are rejected (`422 invalid_request`) so that typos such as `orgin` cannot silently
widen a valuation.

### `POST /valuations` response (sandbox example, trimmed)

```jsonc
{
  "object": "valuation",
  "valuation_id": "val_01M87FMN3YG6FDPT0FDG02Y8VV",
  "created_at": "2026-11-24T15:04:11Z",
  "status": "priced",                       // or "insufficient_data"
  "model_version": "comps-2026.11.0",
  "model_version_source": "default",        // "request" | "tenant_pin" | "default"
  "engine_version": "v2.4",
  "synthetic_data": true,                   // sandbox only
  "valuation_basis": "retail_replacement",
  "currency": "USD",
  "client_reference": "POLICY-88231/ITEM-2",
  "inputs": { "variety": "blue sapphire", "carat": 2.1, "shape": "oval", "origin": "Sri Lanka", "...": "..." },
  "normalized_inputs": { "variety": "blue_sapphire", "species": "corundum", "origin": "sri_lanka", "treatment": null, "...": "..." },
  "price": {
    "per_carat": { "low": 1379, "mid": 2052, "high": 2564 },
    "total":     { "low": 2896, "mid": 4309, "high": 5385 }
  },
  "confidence": {
    "score": 0.7,
    "level": "medium",
    "reasons": [
      { "code": "missing_treatment",
        "message": "Treatment was not provided, so comparables include treated and untreated stones. See scenarios for each case." }
    ]
  },
  "comps": [
    {
      "comp_id": "sbx_blue_sapphire_0133",
      "source_type": "asking",              // "realized" = completed sale; "asking" = list price
      "source": "marketplace:sandbox-market-1",
      "date": "2024-02-21",
      "currency": "USD",
      "carat": 2.04,
      "price_total": 6709.0,
      "price_per_carat": 3288.73,
      "adjusted_price_per_carat": null,     // engine adjustment toward the subject stone, if any
      "basis_factor": 1.0,
      "basis_price_per_carat": 3288.73,
      "similarity": 0.9398,
      "qualified": true,
      "attributes": { "variety": "blue_sapphire", "treatment": "none", "origin": "sri_lanka", "...": "..." }
    }
  ],
  "comps_summary": {
    "considered": 60, "rejected": 0, "qualified": 60, "used": 25,
    "realized": 4, "asking": 21, "min_required": 5, "similarity_threshold": 0.55
  },
  "scenarios": [
    { "scenario_id": "treatment:none", "label": "If unheated", "assumptions": { "treatment": "none" },
      "status": "priced", "price": { "per_carat": { "low": 2054, "mid": 2561, "high": 3228 }, "total": { "...": "..." } },
      "comps_used": 25, "confidence": { "score": 1.0, "level": "high", "reasons": [] } },
    { "scenario_id": "treatment:heated", "label": "If heated", "assumptions": { "treatment": "heated" },
      "status": "priced", "price": { "per_carat": { "low": 1084, "mid": 1547, "high": 1748 }, "total": { "...": "..." } },
      "comps_used": 25, "confidence": { "score": 1.0, "level": "high", "reasons": [] } }
  ],
  "warnings": ["Sandbox: prices are computed from synthetic stones and are not market values."],
  "batch_job_id": null
}
```

### The 5-comp floor (hard rule)

If fewer than **5** comps qualify (similarity at or above the model version's threshold, a valid
`source_type`, USD, positive price and carat), the API returns **no price**:

```jsonc
{
  "status": "insufficient_data",
  "price": null,
  "confidence": { "score": 0.0, "level": "none",
    "reasons": [{ "code": "insufficient_comps",
                  "message": "Only 1 qualifying comparable; at least 5 are required, so no price is returned." }, "..."] },
  "comps": [ "... the qualifying comps, then the closest near misses (qualified: false), up to 10 ..." ],
  "comps_summary": { "qualified": 1, "used": 0, "min_required": 5, "...": "..." }
}
```

The floor is enforced in three places: the service (`MIN_COMPS = 5`, not configurable and not part
of a model version), the response schema (a `priced` response with fewer than 5 comps fails
validation before it is stored or returned), and each scenario independently. Valuations that end in
`insufficient_data` are still stored and metered (as total, not priced), which gives the pilot
"coverage" metric directly from `/usage`.

### Valuation basis

Each comp's price per carat (or the engine's adjusted price per carat) is multiplied by a factor
that depends on its source type and the requested basis. The band is the similarity- and
source-weighted 25th, 50th and 75th percentile of those converted prices; realized sales weigh 1.0
and asking prices 0.7 (model version `comps-2026.11.0`). Totals are per-carat values times the
subject carat weight.

| Comp source → basis | `retail_replacement` | `fair_market` | `wholesale` | `auction_realized` |
| --- | --- | --- | --- | --- |
| `asking` (list price) | 1.00 | 0.80 | 0.55 | 0.70 |
| `realized` (completed sale) | 1.40 | 1.00 | 0.75 | 1.00 |

> **These factors are placeholders.** They encode the right structure (asking prices overstate
> transaction prices; realized sales are the best evidence of fair market value) but must be
> calibrated by the pricing team on realized-sale data before any live pilot. They are versioned with
> the model version, so changing them means releasing a new version, not editing an old one.

### Confidence reason codes

The score starts at 1.0 and each applicable reason subtracts a penalty (level: `high` ≥ 0.75,
`medium` ≥ 0.5, else `low`; `none` when there is no price).

| Code | When | Penalty |
| --- | --- | --- |
| `insufficient_comps` | Fewer than 5 qualifying comps (no price) | score = 0 |
| `few_comps` | 5–9 qualifying comps | 0.15 |
| `missing_treatment` | Treatment not provided | 0.15 (0.30 for corundum and emerald) |
| `missing_origin` | Origin not provided for an origin-sensitive variety | 0.10 |
| `rare_origin` | Fewer than 3 used comps share the stated origin | 0.15 |
| `asking_only` | No realized sales among the comps used | 0.10 |
| `wide_band` | (high − low) / mid > 0.6 | 0.10 |
| `stale_comps` | Median comp older than 24 months | 0.10 |
| `low_similarity` | Mean similarity of comps used below 0.70 | 0.10 |
| `no_lab_report` | No lab report provided | 0.05 |

### Scenario pricing

When `treatment` is omitted, the stone is also priced under each plausible treatment (corundum:
`none` labelled "If unheated", and `heated`; emerald: `none` and `oiled`). When `origin` is omitted
for an origin-sensitive variety (blue sapphire, ruby, emerald, spinel, padparadscha, Paraíba
tourmaline, alexandrite), it is also priced under that variety's main origins. Each scenario has its
own status, price, comp count and confidence, and obeys the 5-comp floor.

### Model versions and pinning

Every response carries `model_version`, which fixes the engine version and every enterprise rule
that can move a number (threshold, comp cap, weights, basis factors, quantiles). Resolution order:

1. `model_version` in the request
2. the tenant's pinned version (`python -m enterprise.cli pin-model --tenant ... --version ...`)
3. the current default

Unknown or retired versions return `422 unknown_model_version` with the list of available versions.
A batch is resolved once at submission and every row uses that version.

| Version | Engine | Status |
| --- | --- | --- |
| `comps-2026.11.0` | `v2.4` | current |
| `comps-2026.09.0` | `v2.3` | supported |

The engine version labels are placeholders until they are mapped to the V2 engine's real releases.

### Idempotency

Send `Idempotency-Key: <unique string>` (1–255 characters) on `POST /valuations` and
`POST /valuations/batch`. Within 24 hours, the same key and body returns the original response with
`Idempotent-Replayed: true`; nothing is re-computed or re-metered. The same key with a different body
returns `422 idempotency_key_reused`. Errors are not stored, so a corrected request can reuse its key.
Keys are scoped per tenant.

### Batch CSV

`POST /valuations/batch` takes multipart form data: `file` (UTF-8 CSV, ≤ 5 MB, ≤ 5,000 rows),
optional `valuation_basis` (default for rows without one) and optional `model_version`.

Input columns (header required; `*` = required; extra columns are ignored):
`row_id`, `variety*`, `species`, `carat*`, `shape*`, `color_grade`, `clarity`, `treatment`, `origin`,
`lab`, `lab_report_number`, `length_mm`, `width_mm`, `depth_mm`, `valuation_basis`.

The response is `202` with a `job_id`. Poll `GET /valuations/batch/{job_id}` until `status` is
`completed`, then download `results_url`. Results have one row per input row:
`row_number, row_id, status, valuation_id, currency, valuation_basis, per_carat_low, per_carat_mid,
per_carat_high, total_low, total_mid, total_high, confidence_score, confidence_level, reason_codes,
comps_used, realized_comps, asking_comps, model_version, synthetic_data, error`.

Rows that fail validation get `status = error` with the reason; they do not fail the job. Every
priced row is an ordinary stored valuation (retrievable by `valuation_id`, linked by `batch_job_id`).
Text cells that start with `=`, `+`, `-` or `@` are prefixed with `'` to prevent spreadsheet formula
injection.

### Errors

Every error has the same shape: `{"error": {"code": "...", "message": "...", "details": ...}}`.

| Status | Codes |
| --- | --- |
| 401 | `missing_api_key`, `invalid_api_key` |
| 403 | `insufficient_scope`, `tenant_inactive` |
| 404 | `valuation_not_found`, `batch_job_not_found` (also for other tenants' ids) |
| 409 | `batch_not_completed`, `idempotency_conflict` |
| 413 | `file_too_large` |
| 422 | `invalid_request`, `unknown_model_version`, `idempotency_key_reused`, `invalid_idempotency_key`, `invalid_csv`, `too_many_rows`, `invalid_date_range` |
| 503 | `engine_unavailable`, `model_version_unavailable` |

---

## Data: realized vs asking

Auction and resale customers judge everything by realized prices, so every comp must say what kind of
price it is:

- **Engine output:** `source_type` (`realized` | `asking`) is required on every comp. Comps without
  it are rejected (counted in `comps_summary.rejected`), never priced.
- **Scraped listings (this repo):** `gemstone_items.price_source_type` was added. Dealer sites
  (Leibish, James Allen, Blue Nile) default to `asking`. Auction houses are `realized`, because the
  only price an auction adapter may store is a sold-lot result. For existing databases,
  `storage/migrations/001_add_price_source_type.sql` backfills dealer rows to `asking` and leaves
  existing auction rows NULL (unverified, excluded from comps).
- **The V2 listings table (backend, not in this repo)** needs the same column. Suggested migration:

  ```sql
  ALTER TABLE <listings> ADD COLUMN source_type VARCHAR(16)
    CHECK (source_type IN ('realized', 'asking'));
  UPDATE <listings> SET source_type = 'asking' WHERE source_type IS NULL;  -- scraped dealer listings
  ALTER TABLE <listings> ALTER COLUMN source_type SET NOT NULL;
  ```

The data gap is real: the scraping pipeline here does not capture prices at all (its site configs
list `price` as an ignored field), and the backend's ~15K listings come from dealers, so today
essentially every comp is an asking price. Expect `asking_only` on most valuations until
realized-price sources are added.

---

## Operations

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENTERPRISE_DATABASE_URL` | `sqlite:///./data/enterprise.db` | Postgres in production (`postgresql://...` uses psycopg2) |
| `ENTERPRISE_ENGINE_BACKEND` | `none` | `none` (live → 503), `remote` (V2 engine), `sandbox` (synthetic, dev only) |
| `ENTERPRISE_ENGINE_URL`, `ENTERPRISE_ENGINE_API_KEY` | | V2 engine endpoint and credential |
| `ENTERPRISE_ENGINE_TIMEOUT_SECONDS` | `5.0` | |
| `ENTERPRISE_DEFAULT_MODEL_VERSION` | registry `current` | Override the default model version |
| `ENTERPRISE_IDEMPOTENCY_TTL_HOURS` | `24` | |
| `ENTERPRISE_BATCH_MAX_ROWS`, `ENTERPRISE_BATCH_MAX_BYTES` | `5000`, `5000000` | |

**Migrations:** `ENTERPRISE_DATABASE_URL=... alembic -c enterprise/alembic.ini upgrade head`.
Verified against SQLite and Postgres 16. Do not run against production without review.

**Operator CLI** (`python -m enterprise.cli --help`): `create-tenant`, `create-key`, `revoke-key`,
`pin-model`, `export-openapi`, `init-db` (SQLite only).

## Known limitations (v1)

- No real prices until the V2 engine adapter is connected (`ENTERPRISE_ENGINE_BACKEND=remote`).
- USD only; the engine adapter must normalize currency.
- Batch jobs run in-process after the response. `process_batch_job` is a plain function, ready to
  move onto the existing Celery workers; a job interrupted by a restart stays `running`.
- Audit retention: valuations are never deleted by the API (the spec asks for 7+ years); no
  retention job or export exists yet.
- No admin console, SSO, billing or webhooks (out of scope for v1).
- Raw/Make/Market breakdown for set pieces and photo inputs are not in v1.
