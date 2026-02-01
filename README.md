# Gemstone Dataset Pipeline

A production-grade, distributed pipeline for creating validated gemstone identification datasets. This system collects, validates, and processes gemstone listings to produce training-ready datasets for computer vision models.

## Overview

This pipeline:
- Crawls configured gemstone dealer and auction house websites
- Extracts gemstone attributes (stone type, shape, carat, lab reports)
- Validates data quality with tiered confidence scoring
- Deduplicates using perceptual image hashing
- Produces clean training manifests

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       Gemstone Pipeline                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │ Discovery│───▶│  Fetch   │───▶│  Parse   │───▶│ Validate │  │
│  │  Task    │    │  Task    │    │  Task    │    │  Task    │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│       │               │               │               │         │
│       ▼               ▼               ▼               ▼         │
│  ┌────────────────────────────────────────────────────────┐    │
│  │                    Celery + Redis                       │    │
│  └────────────────────────────────────────────────────────┘    │
│       │               │               │               │         │
│       ▼               ▼               ▼               ▼         │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │ Database │    │  Storage │    │  Dedupe  │    │ Manifest │  │
│  │(Postgres)│    │(Local/S3)│    │ (phash)  │    │ Builder  │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Clone and Setup

```bash
cd gemstone

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
```

### 2. Start Infrastructure

```bash
# Using Docker Compose (recommended)
docker compose up -d

# Or start services manually:
# - Redis on localhost:6379
# - PostgreSQL on localhost:5432
```

### 3. Run Pipeline

```bash
# Check compliance for a domain
python -m scripts.check_compliance --domain leibish.com

# Submit a discovery job
python -m scripts.submit_discovery --site leibish --limit 1000

# Run workers (if not using Docker)
python -m scripts.run_workers

# Export validated items
python -m scripts.export_validated --output data/validated_items.jsonl

# Build training manifest
python -m scripts.build_manifest \
    --input data/validated_items.jsonl \
    --output data/train_manifest.csv

# View statistics
python -m scripts.report_stats --input data/validated_items.jsonl
```

## Project Structure

```
gemstone/
├── config/                  # Configuration
│   ├── allowed_sites.py     # Site allowlist and configs
│   └── settings.py          # Application settings
├── crawler/                 # URL discovery and fetching
│   ├── fetcher.py           # HTTP fetcher with retries
│   ├── rate_limiter.py      # Per-domain rate limiting
│   ├── robots.py            # Robots.txt handling
│   └── tos.py               # ToS compliance
├── scrapers/                # Site-specific parsers
│   ├── adapters/            # Site adapters
│   │   ├── leibish.py
│   │   ├── jamesallen.py
│   │   ├── bluenile.py
│   │   ├── sothebys.py
│   │   ├── christies.py
│   │   ├── bonhams.py
│   │   └── generic.py
│   ├── base.py              # Base scraper class
│   ├── models.py            # Pydantic data models
│   └── registry.py          # Scraper registry
├── validators/              # Data validation
│   ├── lab_reports.py       # Lab report validation
│   ├── sanity.py            # Sanity checks
│   ├── schema.py            # Schema validation
│   └── tier.py              # Tier assignment
├── dedupe/                  # Deduplication
│   ├── deduplicator.py      # Main deduplicator
│   ├── phash.py             # Perceptual hashing
│   └── url_canonical.py     # URL canonicalization
├── storage/                 # Data storage
│   ├── base.py              # Storage interface
│   ├── database.py          # PostgreSQL metadata
│   ├── factory.py           # Storage factory
│   ├── local.py             # Local filesystem
│   └── s3.py                # S3/MinIO storage
├── orchestrator/            # Distributed execution
│   ├── celery_app.py        # Celery configuration
│   └── tasks.py             # Celery tasks
├── scripts/                 # CLI entry points
│   ├── build_manifest.py
│   ├── check_compliance.py
│   ├── export_validated.py
│   ├── report_stats.py
│   ├── run_workers.py
│   └── submit_discovery.py
├── infra/                   # Infrastructure
│   └── docker-compose.yml   # Full deployment
├── data/                    # Output data
│   ├── raw/                 # Raw HTML snapshots
│   ├── images/              # Downloaded images
│   ├── validated/           # Validated items
│   └── manifests/           # Training manifests
├── docker-compose.yml       # Simple local setup
├── Dockerfile
├── requirements.txt
└── README.md
```

## CLI Commands

### Check Compliance
```bash
python -m scripts.check_compliance --domain <domain>
```
Verifies a domain is in the allowlist and checks robots.txt.

### Submit Discovery
```bash
python -m scripts.submit_discovery \
    --site <site_name> \
    --seed_url <url> \        # Optional, uses default
    --limit <max_urls> \      # Default: 1000
    --depth <max_depth> \     # Default: 3
    --async                   # Submit as Celery task
```
Starts URL discovery for a site.

### Run Workers
```bash
python -m scripts.run_workers \
    --queues <queue_list> \   # Default: all queues
    --concurrency <n> \       # Default: 4
    --loglevel <level>        # Default: INFO
```
Starts Celery workers locally.

### Export Validated
```bash
python -m scripts.export_validated \
    --output <path.jsonl> \
    --tier <0|1|2> \          # Optional: filter by tier
    --include-rejected \      # Include rejected items
    --include-duplicates      # Include duplicates
```
Exports validated items to JSONL.

### Build Manifest
```bash
python -m scripts.build_manifest \
    --input <validated.jsonl> \
    --output <manifest.csv> \
    --min-tier <0> \          # Default: 0
    --max-tier <2>            # Default: 2
```
Creates training manifest CSV.

### Report Stats
```bash
python -m scripts.report_stats \
    --input <validated.jsonl> \  # Optional, uses DB
    --detailed                   # Show detailed stats
```
Shows pipeline statistics.

## Data Model

### GemstoneItem

```python
{
    "item_id": "uuid",
    "source_site": "leibish.com",
    "source_url": "https://...",
    "discovered_at_utc": "2024-01-01T00:00:00Z",
    "scraped_at_utc": "2024-01-01T00:00:00Z",
    "title": "1.50ct Fancy Yellow Diamond",
    "description_text": "...",
    "image_urls": ["https://..."],
    "downloaded_images": [...],
    "stone_type_claimed": "diamond",
    "shape": "cushion",
    "color_family": "yellow",
    "carat": 1.50,
    "lab_name": "GIA",
    "lab_report_id": "1234567890",
    "has_lab_report": true,
    "extracted_attributes": {...},
    "validation": {
        "tier": 0,
        "confidence": 0.95,
        "keep": true,
        "reject_reasons": []
    }
}
```

### Validation Tiers

| Tier | Description | Usage |
|------|-------------|-------|
| 0 | Lab report verified, all sanity checks pass | Primary training data |
| 1 | Trusted institutional source, strong attributes | Auxiliary training |
| 2 | Weak labels, no lab report | Pretraining only |
| 3 | Rejected | Not used |

### Training Manifest (CSV)

```csv
stone_id,image_path,label,tier,confidence,source_url
item1_000,images/leibish.com/item1/000.jpg,diamond,0,0.95,https://...
item1_001,images/leibish.com/item1/001.jpg,diamond,0,0.95,https://...
```

## Configuration

### Environment Variables

```bash
# Application
APP_ENV=development
DEBUG=false
LOG_LEVEL=INFO

# Redis
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0

# PostgreSQL
DATABASE_URL=postgresql://gemstone:gemstone@localhost:5432/gemstone

# Storage
STORAGE_BACKEND=local  # or 's3'
LOCAL_STORAGE_ROOT=./data

# S3/MinIO (if STORAGE_BACKEND=s3)
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=gemstone-data

# Crawler
DEFAULT_CRAWL_DELAY=2.0
MAX_CONCURRENT_PER_DOMAIN=2
REQUEST_TIMEOUT=30

# Validation
MIN_IMAGE_COUNT=2
MIN_IMAGE_RESOLUTION=600
MIN_CARAT=0.05
MAX_CARAT=50.0
PHASH_DISTANCE_THRESHOLD=8
```

## Adding a New Site Adapter

1. **Add to allowlist** in `config/allowed_sites.py`:
```python
ALLOWED_SITES["newsite.com"] = SiteConfig(
    domain="newsite.com",
    purposes=("diamond_identification",),
    expected_validation_tier=0,
    trust_lab_reports=True,
    # ...
)
```

2. **Create adapter** in `scrapers/adapters/newsite.py`:
```python
from scrapers.base import BaseScraper
from scrapers.registry import register_scraper

@register_scraper("newsite.com")
class NewSiteScraper(BaseScraper):
    def extract_item(self, html, url, compliance):
        # Implement extraction logic
        ...

    def discover_urls(self, html, base_url, depth):
        # Implement URL discovery
        ...

    def is_listing_page(self, url, html=None):
        # Implement page detection
        ...
```

3. **Register adapter** in `scrapers/adapters/__init__.py`:
```python
from scrapers.adapters.newsite import NewSiteScraper
```

4. **Test the adapter**:
```bash
python -m scripts.check_compliance --domain newsite.com
python -m scripts.submit_discovery --site newsite --limit 10
```

## Deployment

### Local Development
```bash
docker compose up -d
```

### Production (Multi-VM)

1. Deploy Redis and PostgreSQL
2. Build and push Docker image
3. Run workers on multiple VMs:
```bash
# Worker VM 1 - Discovery
docker run -e CELERY_BROKER_URL=redis://... gemstone-pipeline \
    celery -A orchestrator.celery_app worker --queues discovery

# Worker VM 2 - Fetch
docker run -e CELERY_BROKER_URL=redis://... gemstone-pipeline \
    celery -A orchestrator.celery_app worker --queues fetch

# Worker VM 3 - Parse/Validate
docker run -e CELERY_BROKER_URL=redis://... gemstone-pipeline \
    celery -A orchestrator.celery_app worker --queues parse,validate
```

### Kubernetes

See `infra/k8s/` for Kubernetes manifests (if needed).

## Monitoring

### Flower (Celery Monitor)
```bash
# Included in docker-compose
http://localhost:5555
```

### Logs
```bash
# View worker logs
docker compose logs -f worker

# View all logs
docker compose logs -f
```

### Stats
```bash
python -m scripts.report_stats --detailed
```

## License

MIT License
