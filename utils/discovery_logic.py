"""Re-discovery rules: classify discovered places into inserts, line crossings and refreshes."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from config.discovery_filters import CONSECUTIVE_PASSES_TO_PROMOTE, passes_filter


def website_host(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    host = urlparse(raw if "://" in raw else f"https://{raw}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _name_key(name: Any, city: Any) -> str:
    base = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    return f"{base}|{str(city or '').strip().lower()}" if base else ""


def match_master(
    place: Mapping[str, Any],
    masters: Iterable[Mapping[str, Any]],
    place_hosts: Mapping[str, int],
) -> Tuple[Optional[Mapping[str, Any]], str]:
    """Match a discovered place to an existing business that has no ``google_place_id`` yet.

    Returns (row, reason). reason is "host" (unique host on both sides), "name_city", or
    "ambiguous" when a shared host cannot be resolved, in which case nothing may be inserted.
    """
    unlinked = [m for m in masters if not m.get("google_place_id")]
    host = website_host(place.get("website"))
    if host:
        same_host = [m for m in unlinked if website_host(m.get("website")) == host]
        if len(same_host) == 1 and place_hosts.get(host, 0) == 1:
            return same_host[0], "host"
    key = _name_key(place.get("name"), place.get("city"))
    if key:
        same_name = [m for m in unlinked if _name_key(m.get("name"), m.get("city")) == key]
        if len(same_name) == 1:
            return same_name[0], "name_city"
    if host and any(website_host(m.get("website")) == host for m in unlinked):
        return None, "ambiguous"
    return None, ""


def classify_places(
    places: List[Mapping[str, Any]],
    master_by_place_id: Mapping[str, Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    now: datetime,
    unlinked_masters: Iterable[Mapping[str, Any]] = (),
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Return (updated candidates, actions). Existing master rows are only refreshed, never demoted.

    ``unlinked_masters`` are existing businesses without a ``google_place_id``; a discovered place
    that matches one is a ``link`` (backfill the id), never an insert.
    """
    updated: Dict[str, Dict[str, Any]] = {key: dict(value) for key, value in candidates.items()}
    actions: Dict[str, List[Dict[str, Any]]] = {
        "insert": [], "promote": [], "refresh": [], "flag_below": [], "link": [], "ambiguous": [],
    }
    stamp = now.isoformat()
    unlinked = [m for m in unlinked_masters if not m.get("google_place_id")]
    place_hosts = Counter(
        website_host(p.get("website")) for p in places
        if website_host(p.get("website")) and str(p.get("google_place_id") or "").strip() not in master_by_place_id
    )
    claimed: set = set()

    for place in places:
        place_id = str(place.get("google_place_id") or "").strip()
        if not place_id:
            continue
        passed = passes_filter(place)
        prior = updated.get(place_id)
        entry = dict(prior or {"first_seen_at": stamp, "consecutive_passes": 0, "promoted_business_id": None})
        entry.update(
            city=place.get("city"),
            name=place.get("name"),
            review_count=place.get("review_count"),
            score=place.get("score"),
            category=place.get("category"),
            website=place.get("website"),
            passed_filter=passed,
            last_seen_at=stamp,
            consecutive_passes=(int(entry.get("consecutive_passes") or 0) + 1) if passed else 0,
        )
        updated[place_id] = entry

        master = master_by_place_id.get(place_id)
        if master is None and unlinked:
            row, reason = match_master(place, [m for m in unlinked if m["business_id"] not in claimed], place_hosts)
            if row is not None:
                claimed.add(row["business_id"])
                actions["link"].append({"business_id": row["business_id"], "google_place_id": place_id,
                                        "matched_by": reason,
                                        "fields": {k: place.get(k) for k in ("review_count", "score")
                                                   if place.get(k) is not None}})
                entry["promoted_business_id"] = row["business_id"]
                continue
            if reason == "ambiguous":
                actions["ambiguous"].append({"google_place_id": place_id, "name": place.get("name"),
                                             "website": place.get("website")})
                continue
        if master is not None:
            entry["promoted_business_id"] = master.get("business_id")
            changed = {
                key: place.get(key)
                for key in ("review_count", "score")
                if place.get(key) is not None and place.get(key) != master.get(key)
            }
            if changed:
                actions["refresh"].append({"business_id": master["business_id"], "fields": changed})
            if not passed:
                actions["flag_below"].append({"business_id": master["business_id"], "google_place_id": place_id})
            continue

        if entry.get("promoted_business_id") is not None or not passed:
            continue
        # A place first seen above the line is inserted at once; a below-line place that rose
        # above it must hold for several observations so it cannot flap in and out.
        first_sighting = prior is None
        if first_sighting:
            actions["insert"].append({"google_place_id": place_id, **{k: place.get(k) for k in (
                "name", "address", "city", "review_count", "score", "category", "website")}})
        elif entry["consecutive_passes"] >= CONSECUTIVE_PASSES_TO_PROMOTE:
            actions["promote"].append({"google_place_id": place_id, **{k: place.get(k) for k in (
                "name", "address", "city", "review_count", "score", "category", "website")}})
    return updated, actions
