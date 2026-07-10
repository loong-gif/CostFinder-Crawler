"""Tests for Firecrawl monitor URL selection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.monitor_target_urls import normalize_monitor_url, pick_monitor_urls


def test_promo_path_preferred_over_services():
    urls = pick_monitor_urls(
        [
            "https://example.com/services",
            "https://example.com/specials",
        ],
        domain_name="example.com",
        max_urls=2,
    )
    assert urls[0] == "https://example.com/specials"
    assert len(urls) == 1


def test_excludes_about_contact_paths():
    urls = pick_monitor_urls(
        [
            "https://example.com/about",
            "https://example.com/contact",
            "https://example.com/pricing",
        ],
        domain_name="example.com",
        max_urls=2,
    )
    assert urls == ["https://example.com/pricing"]


def test_fallback_to_homepage_when_all_excluded():
    urls = pick_monitor_urls(
        ["https://example.com/about", "https://example.com/contact"],
        domain_name="example.com",
        max_urls=2,
    )
    assert urls == ["https://example.com"]


def test_dedupes_http_www_and_tracking_variants():
    urls = pick_monitor_urls(
        [
            "http://www.example.com/specials?utm_source=google",
            "https://example.com/specials",
        ],
        domain_name="example.com",
        max_urls=2,
    )
    assert urls == ["https://example.com/specials"]


def test_returns_up_to_max_urls_for_strong_promo_pages():
    urls = pick_monitor_urls(
        [
            "https://example.com/promotions",
            "https://example.com/pricing",
            "https://example.com/memberships",
        ],
        domain_name="example.com",
        max_urls=2,
    )
    assert len(urls) == 2
    assert "https://example.com/memberships" not in urls
    assert all("example.com" in u for u in urls)


def test_excludes_membership_paths_from_monitor_targets():
    urls = pick_monitor_urls(
        [
            "https://example.com/membership",
            "https://example.com/specials",
        ],
        domain_name="example.com",
        max_urls=2,
    )
    assert urls == ["https://example.com/specials"]


def test_normalize_monitor_url_forces_https_and_strips_www():
    assert normalize_monitor_url("http://www.example.com/specials/?utm_source=x") == (
        "https://example.com/specials/"
    )
