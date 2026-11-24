# GemstoneAI Enterprise

**The pricing and verification layer for jewelry, delivered as infrastructure.**

GemstoneAI Enterprise sells five products to companies that price, accept, list or insure jewelry at
volume: auction houses, luxury resale platforms, marketplaces and insurers. Coverage spans finished
fine jewelry (rings, earrings, necklaces, pendants, bracelets, brooches, including signed and period
pieces) and loose diamonds (natural and lab-grown) and gemstones. All five products run on one
engine (the V2 kNN comps engine) and one enterprise platform (tenants, scoped API keys, audit store,
metering). Each product is a separate SKU, so a customer can buy one without the rest.

| Product | What it does | First buyer | Status |
| --- | --- | --- | --- |
| **Comps** | Price band, Raw/Make/Market breakdown, the comparables behind it and a confidence score for any piece or loose stone | Insurers, appraisal platforms, marketplaces | **API v1 built** (this document) |
| **Intake** | Triages consigned jewelry into list now, specialist review, lab or decline | Luxury resale | Planned; scope `intake` reserved |
| **Estimate** | Pre-sale estimates and reserves for jewelry lots with documented realized comps | Auction houses | Planned; scope `estimate` reserved |
| **Verify** | Risk-scores listings and claims whose price, materials, stones or photos don't add up | Marketplaces, insurers | Planned; scope `verify` reserved |
| **Index** | Licenses the GemstoneAI Price Index (GPI) and price data for jewelry, diamonds and gemstones | Auction houses, insurers, lenders | Planned; scope `index` reserved |

Comps is the core: every other product calls it. This release builds Comps v1 and the minimum
platform layer it needs.

The product spec (the GemstoneAI Enterprise Claude Doc) was updated on 2026-11-24 to reflect
all-jewelry scope.

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
                                   │                    scenarios, breakdown, version pinning
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   RemoteCompsEngine (live tenants)          SandboxEngine (gsk_test_ keys)
   HTTP → V2 kNN engine in the backend       deterministic synthetic jewelry and stones
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
similarity-scored candidates, each tagged `realized` or `asking` (plus, for pieces, the Raw/Make/Market
shares of the subject's value), and applies the enterprise rules on top. The production engine (V2)
lives in the GemstoneAI backend; `enterprise/engine/remote.py` documents the one internal endpoint it
needs (`POST /comps/search`). Until that is wired up, live tenants get `503 engine_unavailable`, never
a silent fallback to synthetic data.

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
  -d '{"item_type": "ring",
       "metal": {"type": "platinum", "purity": "950", "weight_grams": 6.4},
       "stones": [{"role": "center", "variety": "blue sapphire", "carat": 2.1,
                   "shape": "oval", "origin": "Sri Lanka"},
                  {"role": "side", "variety": "diamond", "carat": 0.6, "count": 14,
                   "growth": "natural"}],
       "condition": "excellent",
       "valuation_basis": "retail_replacement"}'
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
| `gsk_test_` | sandbox | Synthetic jewelry and stones; every response has `synthetic_data: true` |

Keys are stored as SHA-256 hashes and shown once at creation. Revoked keys and suspended tenants are
refused (`401 invalid_api_key`, `403 tenant_inactive`).

### Endpoints

| Method and path | Purpose |
| --- | --- |
| `POST /valuations` | Value one piece of jewelry or loose stone |
| `GET /valuations/{valuation_id}` | Retrieve a stored valuation exactly as returned (audit trail) |
| `POST /valuations/batch` | Upload a CSV; starts an async job |
| `GET /valuations/batch/{job_id}` | Job status and counts |
| `GET /valuations/batch/{job_id}/results` | Results CSV |
| `GET /models` | Model versions available for pinning, the default and your account's pin |
| `GET /usage` | Valuations per day for your account (`start_date`, `end_date`) |

### `POST /valuations` request

One request shape for every item. A **loose stone** has `item_type: "loose_stone"` and exactly one
stone (with `carat` and `shape`). A **piece** has any other `item_type` and needs `metal`, `stones`,
or both.

| Field | Required | Notes |
| --- | --- | --- |
| `item_type` | yes | `loose_stone`, `ring`, `earrings`, `necklace`, `pendant`, `bracelet`, `brooch`, `other` |
| `valuation_basis` | yes | `retail_replacement`, `fair_market`, `wholesale` or `auction_realized` |
| `stones[]` | loose stone: exactly 1 | One entry per stone or group of identical stones (fields below) |
| `metal` | pieces | `type` (`gold`, `platinum`, `silver`, `palladium`, `other`; `yellow gold` etc. accepted), `purity` (`18k`, `14k`, `950`, `925`; hallmarks such as `750` and `585` normalize), `color`, `weight_grams`. Not allowed on a loose stone. |
| `brand` | no | Maker or house |
| `signed` | no | `true` if the piece bears the maker's signature or mark |
| `era` | no | e.g. `art deco`, `victorian`, `retro`, `contemporary` |
| `condition` | no | `new`, `excellent`, `very_good`, `good`, `fair`, `poor` |
| `total_weight_grams` | no | Whole piece |
| `description` | no | Free text, stored for audit, not used for pricing |
| `model_version` | no | Pin a model version for this request (overrides the account pin) |
| `client_reference` | no | Your identifier, echoed back |

Each `stones[]` entry:

| Field | Required | Notes |
| --- | --- | --- |
| `variety` | yes | e.g. `diamond`, `blue sapphire`, `ruby`, `emerald`, `spinel`. Normalized (`Sapphire` → `blue_sapphire`). |
| `carat` | yes | Weight of the stone, or total weight of the group when `count` > 1 |
| `role` | no | `center` (default), `side` or `accent`; at most one `center` |
| `count` | no | Default 1 |
| `shape` | loose stone | e.g. `round`, `oval`, `cushion` |
| `growth` | no | `natural` or `lab_grown` (`lab-created`, `CVD`, `mined` accepted). **Omit when unknown**: a diamond then gets both scenarios. |
| `treatment` | no | `none`, `heated`, `oiled`, `diffused`, `filled`, `irradiated`, `other` (`unheated`, `no heat`, `minor oil` accepted). **Omit when unknown**: a colored stone then gets scenarios. |
| `origin` | no | Omit when unknown. Aliases accepted (`Myanmar` → `burma`, `Ceylon` → `sri_lanka`). |
| `color_grade`, `clarity`, `cut_grade` | no | e.g. `G`, `VS1`, `excellent` for diamonds; free text for colored stones |
| `lab`, `lab_report_number` | no, together | `GIA`, `IGI`, `HRD`, `AGL`, `SSEF`, `Gubelin` (or `Gübelin`), `GRS` |
| `dimensions_mm` | no | `{"length", "width", "depth"?}` |

Unknown fields are rejected (`422 invalid_request`) so that typos such as `orgin` cannot silently
widen a valuation.

### `POST /valuations` response (sandbox example, trimmed)

```jsonc
{
  "object": "valuation",
  "valuation_id": "val_01M87FMN3YG6FDPT0FDG02Y8VV",
  "created_at": "2026-11-24T15:04:11Z",
  "status": "priced",                       // or "insufficient_data"
  "item_type": "ring",
  "model_version": "comps-2026.11.0",
  "model_version_source": "default",        // "request" | "tenant_pin" | "default"
  "engine_version": "v2.4",
  "synthetic_data": true,                   // sandbox only
  "valuation_basis": "retail_replacement",
  "currency": "USD",
  "client_reference": "POLICY-88231/ITEM-2",
  "inputs": { "item_type": "ring", "metal": { "type": "platinum", "...": "..." }, "stones": [ "..." ] },
  "normalized_inputs": { "item_type": "ring", "stones": [ { "variety": "blue_sapphire", "role": "center", "treatment": null, "...": "..." }, "..." ] },
  "price": {
    "per_carat": null,                      // loose stones only
    "total": { "low": 2447, "mid": 4239, "high": 6581 }
  },
  "breakdown": {                            // pieces only: split of the mid value
    "raw":    { "share": 0.8, "value": 3391 },
    "make":   { "share": 0.2, "value": 848 },
    "market": { "share": 0.0, "value": 0 }
  },
  "confidence": {
    "score": 0.6,
    "level": "medium",
    "reasons": [
      { "code": "missing_treatment", "message": "Treatment was not provided, so comparables include treated and untreated stones. See scenarios for each case." },
      { "code": "wide_band", "message": "Comparable prices are widely dispersed for this item." }
    ]
  },
  "comps": [
    {
      "comp_id": "sbx_ring_0114",
      "source_type": "asking",              // "realized" = completed sale; "asking" = list price
      "source": "dealer:sandbox-dealer-2",
      "date": "2024-06-26",
      "currency": "USD",
      "carat": null,
      "price_total": 9448.0,
      "price_per_carat": null,
      "adjusted_price_total": null,         // engine adjustment toward the subject, if any
      "basis_factor": 1.0,
      "basis_price_total": 9448.0,
      "similarity": 0.8373,
      "qualified": true,
      "attributes": { "item_type": "ring", "metal_type": "platinum", "metal_purity": "950",
                      "center_variety": "blue_sapphire", "center_carat": 2.1, "treatment": "heated",
                      "origin": "sri_lanka", "side_carat_total": 0.71, "condition": "excellent", "...": "..." }
    }
  ],
  "comps_summary": {
    "considered": 46, "rejected": 0, "qualified": 39, "used": 25,
    "realized": 5, "asking": 20, "min_required": 5, "similarity_threshold": 0.55
  },
  "scenarios": [
    { "scenario_id": "treatment:none", "label": "If unheated", "assumptions": { "treatment": "none" },
      "status": "priced", "price": { "per_carat": null, "total": { "low": 2659, "mid": 5970, "high": 10525 } },
      "comps_used": 25, "confidence": { "...": "..." } },
    { "scenario_id": "treatment:heated", "label": "If heated", "assumptions": { "treatment": "heated" },
      "status": "priced", "price": { "per_carat": null, "total": { "low": 2287, "mid": 3204, "high": 4481 } },
      "comps_used": 25, "confidence": { "...": "..." } }
  ],
  "warnings": ["Sandbox: prices are computed from synthetic items and are not market values."],
  "batch_job_id": null
}
```

For a loose stone, `price.per_carat` holds the per-carat band, `total` = per carat × carat, there is no
`breakdown`, and each comp carries `carat`, `price_per_carat` and `basis_price_per_carat`.

### The 5-comp floor (hard rule)

If fewer than **5** comps qualify (similarity at or above the model version's threshold, a valid
`source_type`, USD, finite positive prices), the API returns **no price** and no breakdown:

```jsonc
{
  "status": "insufficient_data",
  "price": null,
  "breakdown": null,
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
"coverage" metric directly from `/usage`. A named maker with no comparables of its own, for example,
returns `insufficient_data` rather than being priced as unbranded metal and stones.

### Valuation basis

Each comp's price (or the engine's adjusted price) is multiplied by a factor that depends on its
source type and the requested basis. The band is the similarity- and source-weighted 25th, 50th and
75th percentile of those converted prices, per carat for loose stones and per item for pieces.
Realized sales weigh 1.0 and asking prices 0.7 (model version `comps-2026.11.0`).

| Comp source → basis | `retail_replacement` | `fair_market` | `wholesale` | `auction_realized` |
| --- | --- | --- | --- | --- |
| `asking` (list price) | 1.00 | 0.80 | 0.55 | 0.70 |
| `realized` (completed sale) | 1.40 | 1.00 | 0.75 | 1.00 |

> **These factors are placeholders.** They encode the right structure (asking prices overstate
> transaction prices; realized sales are the best evidence of fair market value) but must be
> calibrated by the pricing team on realized-sale data before any live pilot, possibly per category
> (loose diamonds, colored stones, signed pieces). They are versioned with the model version, so
> changing them means releasing a new version, not editing an old one.

### Raw / Make / Market breakdown

For pieces, the engine may return the shares of the subject's value that come from materials
(**raw**: metal and stones), craftsmanship (**make**) and brand or period premium (**market**). The
API applies those shares to the mid total in the requested basis. The breakdown is omitted for loose
stones, when the engine supplies no shares, and when there is no price. Shares are validated
(non-negative, summing to 1) at the adapter boundary.

### Confidence reason codes

The score starts at 1.0 and each applicable reason subtracts a penalty (level: `high` ≥ 0.75,
`medium` ≥ 0.5, else `low`; `none` when there is no price). Stone reasons apply to the principal
stone: the center stone, or the largest single stone.

| Code | When | Penalty |
| --- | --- | --- |
| `insufficient_comps` | Fewer than 5 qualifying comps (no price) | score = 0 |
| `few_comps` | 5–9 qualifying comps | 0.15 |
| `missing_growth` | A diamond without natural vs lab-grown | 0.45 |
| `missing_treatment` | A colored stone without treatment | 0.15 (0.30 for corundum and emerald) |
| `missing_origin` | Origin not provided for an origin-sensitive variety | 0.10 |
| `rare_origin` | Fewer than 3 used comps share the stated origin | 0.15 |
| `missing_metal` | A piece without metal | 0.15 |
| `missing_metal_weight` | Metal without weight | 0.10 |
| `unsigned_brand` | A maker is named but `signed` is not `true` | 0.10 |
| `missing_condition` | A piece without condition | 0.05 |
| `asking_only` | No realized sales among the comps used | 0.10 |
| `wide_band` | (high − low) / mid > 0.6 | 0.10 |
| `stale_comps` | Median comp older than 24 months | 0.10 |
| `low_similarity` | Mean similarity of comps used below 0.70 | 0.10 |
| `no_lab_report` | No lab report for a loose stone, or for a set stone of 1 ct or more | 0.05 |

### Scenario pricing

Scenarios price the item again under each plausible value of an unknown on the principal stone:

- **Diamond, growth unknown:** "If natural" and "If lab-grown" (often a 10x difference).
- **Colored stone, treatment unknown:** corundum "If unheated" and "If heated"; emerald `none` and
  `oiled`.
- **Colored stone, origin unknown,** for origin-sensitive varieties (blue sapphire, ruby, emerald,
  spinel, padparadscha, Paraíba tourmaline, alexandrite): that variety's main origins.

Each scenario has its own status, price, comp count and confidence, and obeys the 5-comp floor. Side
stones are left as given.

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
optional `valuation_basis` (default for rows without one) and optional `model_version`. One item per
row; extra columns are ignored; the header must include `variety`, `item_type` or `metal_type`.

| Group | Columns |
| --- | --- |
| Row | `row_id`, `item_type` (default `loose_stone`), `valuation_basis` |
| Center or loose stone | `variety`, `species`, `carat`, `shape`, `color_grade`, `clarity`, `cut_grade`, `treatment`, `origin`, `growth`, `lab`, `lab_report_number`, `length_mm`, `width_mm`, `depth_mm` |
| Side stones | `side_variety` (default `diamond`), `side_carat_total`, `side_count`, `side_growth` |
| Metal | `metal_type`, `metal_purity`, `metal_color`, `metal_weight_grams` |
| Piece | `brand`, `signed` (`true`/`false`/`yes`/`no`), `era`, `condition` |

The response is `202` with a `job_id`. Poll `GET /valuations/batch/{job_id}` until `status` is
`completed`, then download `results_url`. Results have one row per input row:
`row_number, row_id, item_type, status, valuation_id, currency, valuation_basis, per_carat_low,
per_carat_mid, per_carat_high, total_low, total_mid, total_high, breakdown_raw, breakdown_make,
breakdown_market, confidence_score, confidence_level, reason_codes, comps_used, realized_comps,
asking_comps, model_version, synthetic_data, error`.

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
realized-price sources (auction results, resale final prices) are added, across all jewelry
categories rather than gemstones alone.

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

- No real prices until the V2 engine adapter is connected (`ENTERPRISE_ENGINE_BACKEND=remote`), and
  the V2 engine must accept the item shape above (pieces as well as loose stones).
- Watches, pearls-specific grading (size in mm, luster, nacre) and photo inputs are not in v1.
- USD only; the engine adapter must normalize currency.
- Batch jobs run in-process after the response. `process_batch_job` is a plain function, ready to
  move onto the existing Celery workers; a job interrupted by a restart stays `running`.
- Audit retention: valuations are never deleted by the API (the spec asks for 7+ years); no
  retention job or export exists yet.
- No admin console, SSO, billing or webhooks (out of scope for v1).
