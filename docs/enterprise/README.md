# GemstoneAI Enterprise

**The pricing and verification layer for jewelry, delivered as infrastructure.**

GemstoneAI Enterprise sells five products to companies that price, accept, list or insure jewelry at
volume: auction houses, luxury resale platforms, marketplaces and insurers. Coverage spans finished
fine jewelry (including signed and period pieces), loose diamonds and gemstones. Every product builds
on the GemstoneAI scan.

| Product | What it does | First buyer | Status |
| --- | --- | --- | --- |
| **Scan API** | Today's GemstoneAI scan result for business systems: identification, price and comparables in one call | Insurers, appraisal platforms, marketplaces | **API v1 built** (this document) |
| **Intake** | Routes consigned jewelry to list now, specialist review, lab or decline | Luxury resale | Planned; scope `intake` reserved |
| **Estimate** | Pre-sale estimates and reserves for jewelry lots, documented with realized comps | Auction houses | Planned; scope `estimate` reserved |
| **Verify** | Risk-scores listings and claims whose price, materials, stones or photos don't add up | Marketplaces, insurers | Planned; scope `verify` reserved |
| **Index** | Licenses the GemstoneAI Price Index (GPI) and price data for jewelry, diamonds and gemstones | Auction houses, insurers, lenders | Planned; scope `index` reserved |

The product spec (the GemstoneAI Enterprise Claude Doc) also describes valuation bases, scenario
pricing and a Raw/Make/Market breakdown. Scan API v1 deliberately leaves those out and returns
today's scan result as it is.

---

## How it works

The Scan API does not price anything itself. It runs the existing GemstoneAI scan and adds what
businesses need around it.

```
 Customer system ──HTTPS──▶  /enterprise/v1  (separate FastAPI app, own OpenAPI)
                                   │
                    API key → tenant (scoped), Idempotency-Key handling
                                   │
                    ScanService (enterprise/scan.py)
                      1. analyze: photos (+ known details)  ─┐
                      2. wait, or answer "processing"        ├─▶ GemstoneAI scan (backend)
                      3. comparables for the analysis       ─┘    RemoteScanEngine, or the
                      4. 5-comparable rule, store, meter           SandboxScanEngine for gsk_test_ keys
                                   │
   Postgres ent_* tables: tenants, api keys, scans (audit), idempotency, usage, batch jobs
```

What the enterprise layer adds to the scan:

1. **One call, one result:** identification, price context and comparables together.
2. **Business plumbing:** per-customer API keys with product scopes, every result stored and
   retrievable by ID, safe retries, daily usage counts, CSV batch.
3. **A version stamp** on every result, with an optional pin per request or per account.
4. **One rule:** no price with fewer than 5 comparables (the reason is given and the comparables
   found are still returned).

**Isolation.** The enterprise API is its own FastAPI app (`enterprise/app.py`) with its own routes,
auth, errors and OpenAPI document, and its own `ent_` tables migrated by a separate Alembic history
(`ent_alembic_version`). To serve it from the main backend:
`backend_app.mount("/enterprise/v1", create_enterprise_app())`. Links into the consumer app are not
carried into enterprise results, and enterprise scans should not appear in or count against any
consumer account (a backend-side requirement).

**The backend connection.** `enterprise/engine/remote.py` documents the three endpoints the adapter
calls: `POST /analyses`, `GET /analyses/{id}` and `GET /analyses/{id}/comparables`. Their field
names mirror what today's scan returns but are a proposal: check them against a real scan result and
adjust the two mapping functions in that file. Until it is connected, live tenants get
`503 scan_unavailable`, never a silent fallback to example data.

---

## Quickstart (local)

```bash
pip install -r requirements.txt
export ENTERPRISE_DATABASE_URL=sqlite:///./data/enterprise.db
python -m enterprise.cli init-db                      # local SQLite only; use Alembic elsewhere

python -m enterprise.cli create-tenant --name "Demo Insurer" --environment sandbox
python -m enterprise.cli create-key --tenant ten_01M8... --scopes scan
# {"key_id": "key_...", "api_key": "gsk_test_..."}   <- shown once

uvicorn --factory enterprise.app:create_app --reload
# Interactive docs: http://localhost:8000/enterprise/v1/docs

curl -s localhost:8000/enterprise/v1/scans \
  -H "Authorization: Bearer gsk_test_..." \
  -H "Idempotency-Key: demo-1" \
  -H "Content-Type: application/json" \
  -d '{"photos": [{"url": "https://photos.example.com/ring-front.jpg"},
                  {"url": "https://photos.example.com/ring-inside-band.jpg"}],
       "details": {"item_type": "ring", "metal": {"type": "platinum", "weight_grams": 6.4}}}'
```

Run the tests with `pytest tests/enterprise`. Set `ENTERPRISE_TEST_POSTGRES_URL` to a disposable
Postgres database to also run the integration test on Postgres.

---

## Scan API v1 contract

Base URL: `https://api.gemstoneaiapp.com/enterprise/v1`. The machine-readable contract is
[`openapi.json`](openapi.json) (regenerate with `python -m enterprise.cli export-openapi`; a test
fails if it drifts from the code).

### Authentication

Send `Authorization: Bearer <key>` (or `X-API-Key: <key>`). Keys belong to one account and carry
per-product scopes; Scan API endpoints need the `scan` scope. `gsk_live_` keys use the real scan.
`gsk_test_` keys return example results, and every such response has `synthetic_data: true`. Keys
are stored as SHA-256 hashes and shown once at creation.

### Endpoints

| Method and path | Purpose |
| --- | --- |
| `POST /scans` | Scan one piece of jewelry or loose stone |
| `GET /scans/{scan_id}` | Retrieve a scan as it was returned (audit), or the result of one still running |
| `POST /scans/batch` | Upload a CSV; starts an async job |
| `GET /scans/batch/{job_id}` | Job status and counts |
| `GET /scans/batch/{job_id}/results` | Results CSV |
| `GET /usage` | Scans per day for your account (`start_date`, `end_date`) |

### `POST /scans` request

| Field | Required | Notes |
| --- | --- | --- |
| `photos[]` | yes, 1-5 | Each `{"url": "https://..."}` or `{"data": "<base64>", "media_type": "image/jpeg"}` (png, webp and heic too; up to 8 MB). Front, side, and any stamps inside the band or on the clasp. |
| `details` | no | What you already know, passed to the scan as hints: `item_type`, `metal` (`type`, `purity`, `color`, `weight_grams`), `brand`, `signed`, `era`, `condition`, `lab`, `lab_report_number`, `notes` |
| `model_version` | no | Pin a scan model version (overrides your account's pin) |
| `client_reference` | no | Your identifier, echoed back |

Unknown fields are rejected (`422 invalid_request`). Image bytes are sent to the scan and never
stored; the scan record keeps each photo's sha256, media type, size and URL.

### `POST /scans` response (sandbox example)

```jsonc
{
  "object": "scan",
  "scan_id": "scn_01M8CHV49ESFQ5336GAQVF9YJQ",
  "created_at": "2026-11-26T18:28:17Z",
  "status": "completed",                    // or "processing" (HTTP 202)
  "product": "scan",
  "model_version": "sandbox-2026.11",
  "synthetic_data": true,                   // sandbox only
  "client_reference": "POLICY-88231/ITEM-2",
  "inputs": { "photos": [ { "sha256": "...", "url": "https://photos.example.com/ring.jpg", "...": "..." } ],
              "details": { "item_type": "ring", "metal": { "type": "platinum", "weight_grams": 6.4 } }, "...": "..." },
  "identification": {
    "status": "identified",                 // identified | unidentified | processing
    "summary": "Platinum ring with an oval blue sapphire of about 2.1 ct and diamond side stones",
    "confidence": 0.86,
    "observations": [
      "Oval center stone, saturated medium-dark blue, about 9 x 7 mm",
      "Fourteen small round side stones set along the shoulders",
      "White metal; a PT950 stamp is visible inside the band"
    ],
    "alternatives": [
      { "label": "Center stone is a blue spinel", "probability": 0.08 },
      { "label": "Center stone is a lab-grown sapphire", "probability": 0.06 }
    ],
    "open_questions": ["Is the sapphire heated?", "Where is the sapphire from?"],
    "next_evidence": ["A lab report for the center stone (GIA, SSEF, Gubelin, AGL or GRS) would settle treatment and origin"]
  },
  "price_context": {
    "status": "provided",                   // provided | withheld | processing
    "currency": "USD",
    "low": 2400,
    "high": 6600,
    "basis": "retail replacement",          // what the figure represents, as the scan states it
    "explanation": "Most comparable rings with heated sapphires of this size list between $2,400 and $6,600; unheated stones sit at the top of that range and above.",
    "withheld_reason": null
  },
  "comparables": {
    "status": "found",                      // found | none_found | processing
    "total": 8, "realized": 3, "asking": 5,
    "items": [
      { "source": "sandbox-dealer-1", "title": "Platinum sapphire ring, 2.10 ct, diamond shoulders",
        "url": null, "photo_url": null, "price": 9448.0, "currency": "USD",
        "price_type": "asking",             // "realized" = completed sale; "asking" = list price
        "observed_on": "2024-06-26",
        "differences": ["Heated sapphire, Sri Lanka", "Fewer side stones"] }
    ]
  },
  "warnings": ["Sandbox: this is an example scan result; it does not reflect the photos and its prices are not market values."],
  "batch_job_id": null
}
```

### When there is no price

`price_context.status` is `withheld` and `withheld_reason` says why:

| Case | What you get |
| --- | --- |
| Fewer than 5 comparables | The comparables that were found; reason "Fewer than 5 comparables were found, so no price is given." |
| The scan withholds the price itself | The scan's own reason, and its comparables |
| The item could not be identified | `identification.status: unidentified`, `next_evidence` saying what would help, no comparables |

The 5-comparable rule is enforced in the service and again in the response schema. Withheld scans
are still stored and counted (`scans_total` but not `scans_priced`), which gives the pilot coverage
metric from `/usage`.

### Scans that take a while

If the scan is still running after a short wait (8 seconds by default), `POST /scans` answers
`202` with `status: processing`. `GET /scans/{scan_id}` returns the finished result once it is
ready. The scan is counted once, when it completes.

### Versions

Every completed result carries `model_version`, the scan model that produced it. Pin one with
`model_version` on the request, or for the whole account
(`python -m enterprise.cli pin-model --tenant ... --version ...`). The pin is passed to the scan; an
unknown version returns `422 unknown_model_version` with the available versions.

### Idempotency

Send `Idempotency-Key: <unique string>` (1–255 characters) on `POST /scans` and
`POST /scans/batch`. Within 24 hours, the same key and body returns the original response with
`Idempotent-Replayed: true`; nothing is re-run or re-counted. The same key with a different body
returns `422 idempotency_key_reused`. Errors are not stored, so a corrected request can reuse its
key. Keys are scoped per account.

### Batch CSV

`POST /scans/batch` takes multipart form data: `file` (UTF-8 CSV, ≤ 5 MB, ≤ 5,000 rows) and
optional `model_version`. One item per row; extra columns are ignored.

| Column | Required | Notes |
| --- | --- | --- |
| `photo_urls` | yes | 1-5 https links separated by `\|` |
| `row_id` | no | Echoed as `client_reference` |
| `item_type`, `metal_type`, `metal_purity`, `metal_weight_grams`, `brand`, `signed`, `era`, `condition`, `lab`, `lab_report_number`, `notes` | no | Passed to the scan as details |

The response is `202` with a `job_id`. Poll `GET /scans/batch/{job_id}` until `status` is
`completed`, then download `results_url`. Results have one row per input row: `row_number, row_id,
status, scan_id, identified_as, identification_confidence, price_status, price_low, price_high,
currency, price_basis, withheld_reason, comparables, realized_comparables, asking_comparables,
model_version, synthetic_data, error`. Rows that fail validation get `status = error` with the
reason and do not fail the job. Text cells starting with `=`, `+`, `-` or `@` are prefixed with `'`
to prevent spreadsheet formula injection.

### Errors

Every error has the same shape: `{"error": {"code": "...", "message": "...", "details": ...}}`.

| Status | Codes |
| --- | --- |
| 401 | `missing_api_key`, `invalid_api_key` |
| 403 | `insufficient_scope`, `tenant_inactive` |
| 404 | `scan_not_found`, `batch_job_not_found` (also for other accounts' ids) |
| 409 | `batch_not_completed`, `idempotency_conflict` |
| 413 | `file_too_large` |
| 422 | `invalid_request`, `unknown_model_version`, `idempotency_key_reused`, `invalid_idempotency_key`, `invalid_csv`, `too_many_rows`, `invalid_date_range` |
| 503 | `scan_unavailable` |

---

## Data: realized vs asking

Auction and resale customers judge everything by realized prices, so every comparable says what kind
of price it is (`price_type`: `realized` or `asking`). Comparables from the scan without that tag are
dropped, never shown as evidence.

In this repo, the scraped listings gained `gemstone_items.price_source_type`: dealer sites default to
`asking`, auction houses are `realized`. `storage/migrations/001_add_price_source_type.sql` backfills
existing dealer rows to `asking` and leaves existing auction rows NULL (unverified). The backend's
own listings, which feed the scan's comparables, need the same tag. Today they come from dealers, so
expect most comparables to be asking prices until realized-price sources are added.

---

## Operations

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENTERPRISE_DATABASE_URL` | `sqlite:///./data/enterprise.db` | Postgres in production (`postgresql://...` uses psycopg2) |
| `ENTERPRISE_SCAN_BACKEND` | `none` | `none` (live scans → 503), `remote` (the backend's scan), `sandbox` (example results, dev only) |
| `ENTERPRISE_SCAN_URL`, `ENTERPRISE_SCAN_API_KEY` | | The backend scan endpoint and credential |
| `ENTERPRISE_SCAN_TIMEOUT_SECONDS` | `20.0` | Per-call timeout; also how long a batch row waits for a running scan |
| `ENTERPRISE_SCAN_WAIT_SECONDS`, `ENTERPRISE_SCAN_POLL_INTERVAL_SECONDS` | `8.0`, `1.0` | How long `POST /scans` waits before answering `202 processing` |
| `ENTERPRISE_IDEMPOTENCY_TTL_HOURS` | `24` | |
| `ENTERPRISE_BATCH_MAX_ROWS`, `ENTERPRISE_BATCH_MAX_BYTES` | `5000`, `5000000` | |

**Migrations:** `ENTERPRISE_DATABASE_URL=... alembic -c enterprise/alembic.ini upgrade head`.
Verified against SQLite and Postgres 16. Do not run against production without review.

**Operator CLI** (`python -m enterprise.cli --help`): `create-tenant`, `create-key`, `revoke-key`,
`pin-model`, `export-openapi`, `init-db` (SQLite only).

## Known limitations (v1)

- No real results until the backend adapter is connected (`ENTERPRISE_SCAN_BACKEND=remote`) and its
  field mapping is checked against a real scan.
- Photos are required, as in today's scan. Details-only requests (for example from an auction
  catalogue) would need the backend to support them.
- Photos are not retained (only sha256, size, type and URL); keeping images for audit needs object
  storage and retention terms.
- Batch jobs run in-process after the response. `process_batch_job` is a plain function, ready to
  move onto the existing Celery workers; a job interrupted by a restart stays `running`.
- No admin console, SSO, billing or webhooks (out of scope for v1).
