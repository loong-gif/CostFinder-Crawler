"""URL helpers for membership page routing."""
from __future__ import annotations

from urllib.parse import urlparse

MEMBERSHIP_PATH_FRAGMENTS = (
    "/membership",
    "/memberships",
    "/member",
    "/membership-plans",
)


def is_membership_page_url(url: str) -> bool:
    """True when subpage_url is a dedicated membership page path."""
    path = (urlparse((url or "").strip()).path or "/").lower().rstrip("/") or "/"
    for fragment in MEMBERSHIP_PATH_FRAGMENTS:
        if path == fragment or path.endswith(fragment):
            return True
    return False
