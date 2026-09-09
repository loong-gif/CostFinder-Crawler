"""Runtime configuration shared by active CostFinder commands."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

OUTPUT_DIR = BASE_DIR / "output" / "results"
OUTPUT_ENCODING = "utf-8-sig"

LOG_DIR = BASE_DIR / "output" / "logs"
LOG_LEVEL = "INFO"
LOG_ROTATION = "500 MB"
LOG_RETENTION = "30 days"

FIRECRAWL_CRAWL_MAX_PAGES = int(os.getenv("FIRECRAWL_CRAWL_MAX_PAGES", "50"))
FIRECRAWL_CRAWL_TIMEOUT_SECS = int(
    os.getenv("FIRECRAWL_CRAWL_TIMEOUT_SECS", "1800")
)
CLINIC_SERVICES_SEARCH_LIMIT = int(
    os.getenv("CLINIC_SERVICES_SEARCH_LIMIT", "5")
)
CLINIC_SERVICES_SEARCH_TIMEOUT = int(
    os.getenv("CLINIC_SERVICES_SEARCH_TIMEOUT", "120")
)
CLINIC_SERVICES_FALLBACK_CRAWL_PAGES = int(
    os.getenv("CLINIC_SERVICES_FALLBACK_CRAWL_PAGES", "15")
)


def ensure_directories() -> None:
    """Create directories used by active reports and logs."""
    for directory in (OUTPUT_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


ensure_directories()
