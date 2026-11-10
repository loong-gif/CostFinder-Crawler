"""监控事件入队：到期活动核验、数据质量复查、失败页面的小时重试选择，以及已核验批次的回读确认。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_store import atomic_json, digest, now, read_json
from utils.monitor_price_writer_plan import normalize_source_url


def fresh_evidence(root: Path, url: str, after: str) -> Path:
    """同一来源晚于 after、24 小时内的最新抓取证据；没有即该事件仍需新抓取。"""
    threshold = datetime.fromisoformat(after.replace("Z", "+00:00"))
    newest = None
    for path in (root / "evidence").glob("*.json"):
        artifact = read_json(path)
        if normalize_source_url(artifact["url"]) != normalize_source_url(url):
            continue
        captured = datetime.fromisoformat(artifact["captured_at"].replace("Z", "+00:00"))
        if captured > threshold and datetime.now(timezone.utc) - captured <= timedelta(hours=24):
            if newest is None or captured > newest[0]:
                newest = (captured, path)
    if newest is None:
        raise RuntimeError("fresh_scrape_required")
    return newest[1]


def verified_batch_results(root: Path, snapshot: dict) -> list[dict]:
    """Acknowledge applied fields against today's DB without replaying old DML.

    Receipts cover specific records. Unresolved offers on the same page still
    require review; a prior partial batch does not prove page completeness.
    """
    results = []
    for receipt in read_json(root / "verified_batch_receipts.json", []):
        matches, differences = [], []
        for expected in receipt["records"]:
            actual = next((row for row in snapshot.get(expected["group"], [])
                           if row[expected["pk"]] == expected["id"]), None)
            changed = [field for field, value in expected["values"].items()
                       if actual is None or actual.get(field) != value]
            ref = {"group":expected["group"], "id":expected["id"]}
            if changed:
                differences.append({**ref, "fields":changed})
            else:
                matches.append(ref)
        results.append({"batch_id":receipt["batch_id"],"url":receipt["url"],
                        "verified_at":receipt["verified_at"],"database_read_at":now(),
                        "status":"already_synchronized" if not differences else "changed_since_verification",
                        "confirmed_records":matches,"changed_records":differences,
                        "evidence_paths":receipt.get("evidence_paths",[])})
    return results


def retry_failed_scrape_urls(root: Path, config: dict) -> set[str]:
    """Select failed pages below the per-source hourly retry limit."""
    configured = {url for monitor in config["monitors"].values()
                  for group in monitor.get("targets", []) for url in group.get("urls", [])}
    limits = RetryLimits(root)
    limits.refresh_scrapes(configured)
    needed = {value["url"] for value in limits.state["scrape"].values()
              if not limits.paused("scrape", value["url"])}
    for path in (root / "events").glob("*.json"):
        event = read_json(path)
        if (event.get("status") == "retryable" and event.get("retry_reason") == "fresh_scrape_required"
                and event.get("url") in configured
                and not limits.paused("scrape", event["url"])
                and not limits.paused("processing", event["url"])):
            needed.add(event["url"])
    return needed


def expired_active_promotion_urls(snapshot: dict) -> set[str]:
    """Source URLs for promotions that are still active but past campaign_end_date."""
    today = date.today()
    urls: set[str] = set()
    for promotion in snapshot.get("promotions", []):
        if promotion.get("is_active") is not True or not promotion.get("source_url"):
            continue
        end_date = str(promotion.get("campaign_end_date") or "")[:10]
        try:
            if end_date and date.fromisoformat(end_date) < today:
                urls.add(promotion["source_url"])
        except ValueError:
            continue
    return urls


def enqueue_expired_campaign_checks(root: Path, snapshot: dict, config: dict) -> int:
    """Queue one fresh evidence check for each active, dated campaign that expired."""
    today = datetime.now().astimezone().date()
    newest: dict[str, tuple[datetime, Path, str]] = {}
    for path in (root / "evidence").glob("*.json"):
        try:
            artifact = read_json(path)
            captured = datetime.fromisoformat(str(artifact["captured_at"]).replace("Z", "+00:00"))
            if captured.astimezone().date() != today:
                continue
            url = artifact["url"]
            key = normalize_source_url(url)
            if key not in newest or captured > newest[key][0]:
                newest[key] = (captured, path, url)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    events_dir = root / "events"
    existing_ids = {path.stem for path in events_dir.glob("*.json")}
    queued = 0
    for promotion in snapshot.get("promotions", []):
        if promotion.get("is_active") is not True:
            continue
        end_date = str(promotion.get("campaign_end_date") or "")[:10]
        try:
            if not end_date or date.fromisoformat(end_date) >= today:
                continue
        except ValueError:
            continue
        url = promotion.get("source_url")
        try:
            key = normalize_source_url(url)
        except ValueError:
            continue
        if key not in newest:
            continue
        event_id = digest(["campaign-expiry", promotion["promotion_id"], end_date, newest[key][1].stem])
        if event_id in existing_ids:
            continue
        artifact_path = newest[key][1]
        event = {"event_id":event_id,"url":url,"promotion_id":promotion["promotion_id"],
                 "artifact_id":artifact_path.stem,"artifact_path":str(artifact_path),
                 "status":"pending","change_type":"campaign_expiry_check",
                 "created_at":now(),"expired_campaign_end_date":end_date}
        atomic_json(events_dir / (event_id+".json"),event)
        queued += 1
    return queued


def data_quality_recheck_urls(root: Path) -> set[str]:
    queue = read_json(root / "data_quality" / "recheck_queue.json", {})
    entries = queue.get("entries", []) if isinstance(queue, dict) else queue
    return {row["source_url"] for row in entries if row.get("source_kind") == "promotion"
            and row.get("status") in {"pending", "review"} and row.get("source_url")}


def enqueue_data_quality_rechecks(root: Path) -> int:
    """The shared queue owns status; Monitor adds durable evidence events only."""
    queue = read_json(root / "data_quality" / "recheck_queue.json", {})
    entries = queue.get("entries", []) if isinstance(queue, dict) else queue
    newest = {}
    for path in (root / "evidence").glob("*.json"):
        artifact = read_json(path)
        try:
            captured = datetime.fromisoformat(artifact["captured_at"].replace("Z", "+00:00"))
            if captured.astimezone().date() != datetime.now().astimezone().date():
                continue
            canonical = normalize_source_url(artifact["url"])
            if canonical not in newest or captured > newest[canonical][0]:
                newest[canonical] = (captured, path)
        except (KeyError, TypeError, ValueError):
            continue
    queued = 0
    for row in entries:
        if row.get("source_kind") != "promotion" or row.get("status") not in {"pending", "review"}:
            continue
        key = normalize_source_url(row["source_url"])
        if key not in newest:
            continue
        artifact_path = newest[key][1]
        event_id = digest(["data-quality-recheck", key, row.get("requested_at"), artifact_path.stem])
        event_path = root / "events" / (event_id + ".json")
        if event_path.exists():
            continue
        atomic_json(event_path, {"event_id":event_id,"url":row["source_url"],"status":"pending",
                    "change_type":"data_quality_recheck","created_at":now(),
                    "artifact_id":artifact_path.stem,"artifact_path":str(artifact_path),
                    "quality_reasons":row.get("reasons", []),"quality_requested_at":row.get("requested_at")})
        queued += 1
    return queued
