import os
import sys
import time
import json
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from firecrawl import Firecrawl

def _obj_to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {"id": getattr(obj, "id", "?"), "name": getattr(obj, "name", "?")}

def get_checks_with_retry(fc, monitor_id, retries=5, delay=2):
    for attempt in range(retries):
        try:
            checks_result = fc.list_monitor_checks(monitor_id)
            return checks_result.data if hasattr(checks_result, "data") else checks_result
        except Exception as e:
            err_msg = str(e)
            if "rate limit" in err_msg.lower() or "too many requests" in err_msg.lower() or "429" in err_msg:
                # Try to parse retry-after
                retry_match = re.search(r"retry after (\d+)s", err_msg)
                wait = int(retry_match.group(1)) + 2 if retry_match else delay * (2 ** attempt)
                print(f"      [Rate Limit] Monitor {monitor_id}. Waiting {wait}s...", flush=True)
                time.sleep(wait)
            else:
                if attempt == retries - 1:
                    raise e
                time.sleep(delay)
    raise RuntimeError(f"Failed to fetch checks for {monitor_id} after {retries} retries due to rate limits.")

def analyze_single_monitor(fc, m):
    md = _obj_to_dict(m)
    mid = md.get("id", "?")
    name = md.get("name", "?")
    status = md.get("status", "?")
    next_run = md.get("next_run_at") or md.get("nextRunAt") or "?"

    try:
        checks = get_checks_with_retry(fc, mid)
        checks_list = []
        monitor_has_changes = False
        changes_list = []

        if isinstance(checks, list) and len(checks) > 0:
            for c in checks:
                cd = _obj_to_dict(c)
                cid = cd.get("id", "?")
                c_status = cd.get("status", "?")
                created_at = cd.get("created_at") or cd.get("createdAt") or "?"
                summary = cd.get("summary", {})

                # Check if there are changed pages
                changed = summary.get("changed", 0) if isinstance(summary, dict) else 0
                new = summary.get("new", 0) if isinstance(summary, dict) else 0

                checks_list.append({
                    "check_id": cid,
                    "status": c_status,
                    "created_at": str(created_at),
                    "summary": summary
                })

                if changed > 0 or new > 0:
                    monitor_has_changes = True
                    changes_list.append({
                        "check_id": cid,
                        "created_at": str(created_at),
                        "summary": summary
                    })

        return {
            "success": True,
            "monitor_id": mid,
            "name": name,
            "status": status,
            "next_run_at": str(next_run),
            "checks": checks_list,
            "has_changes": monitor_has_changes,
            "changes": changes_list
        }
    except Exception as e:
        return {
            "success": False,
            "monitor_id": mid,
            "name": name,
            "status": status,
            "next_run_at": str(next_run),
            "error": str(e)
        }

def main():
    load_dotenv()
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("Error: FIRECRAWL_API_KEY not found in .env file.", flush=True)
        sys.exit(1)

    fc = Firecrawl(api_key=api_key)

    print("Fetching all monitors from Firecrawl...", flush=True)
    monitors = []
    limit = 100
    offset = 0
    while True:
        try:
            print(f"  Fetching monitors with limit={limit}, offset={offset}...", flush=True)
            result = fc.list_monitors(limit=limit, offset=offset)
            batch = result.data if hasattr(result, "data") else result
            if not isinstance(batch, list) or len(batch) == 0:
                break
            monitors.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        except Exception as e:
            print(f"Error listing monitors: {e}", flush=True)
            sys.exit(1)

    total_monitors = len(monitors)
    print(f"Found {total_monitors} monitors total.", flush=True)

    print("\nAnalyzing monitor checks in parallel...", flush=True)
    report_data = []
    monitors_with_changes = []
    monitors_with_checks = 0
    total_checks_count = 0
    completed_checks_count = 0
    failed_checks_count = 0

    # Use ThreadPoolExecutor to run tasks in parallel
    max_workers = 15
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(analyze_single_monitor, fc, m): m for m in monitors}
        
        for idx, future in enumerate(as_completed(futures), 1):
            m = futures[future]
            res = future.result()
            
            name = res["name"]
            mid = res["monitor_id"]
            
            if res["success"]:
                checks_list = res["checks"]
                print(f"  [{idx}/{total_monitors}] Analyzed: {name} ({len(checks_list)} checks)", flush=True)
                
                if len(checks_list) > 0:
                    monitors_with_checks += 1
                    total_checks_count += len(checks_list)
                    for c in checks_list:
                        if c["status"] == "completed":
                            completed_checks_count += 1
                        elif c["status"] == "failed":
                            failed_checks_count += 1
                
                if res["has_changes"]:
                    monitors_with_changes.append({
                        "monitor_id": mid,
                        "name": name,
                        "changes": res["changes"]
                    })
                
                report_data.append({
                    "monitor_id": mid,
                    "name": name,
                    "status": res["status"],
                    "next_run_at": res["next_run_at"],
                    "checks_count": len(checks_list),
                    "checks": checks_list
                })
            else:
                print(f"  [{idx}/{total_monitors}] Failed to analyze: {name} - {res['error']}", flush=True)
                report_data.append({
                    "monitor_id": mid,
                    "name": name,
                    "status": res["status"],
                    "next_run_at": res["next_run_at"],
                    "error": res["error"]
                })

    # Save structured report
    output_dir = os.path.join("output", "monitor_results")
    os.makedirs(output_dir, exist_ok=True)
    report_file = os.path.join(output_dir, "monitor_health_report.json")

    summary_stats = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_monitors": total_monitors,
        "monitors_with_checks": monitors_with_checks,
        "total_checks_run": total_checks_count,
        "completed_checks": completed_checks_count,
        "failed_checks": failed_checks_count,
        "monitors_with_detected_changes": len(monitors_with_changes)
    }

    final_report = {
        "summary": summary_stats,
        "monitors": report_data,
        "changes_detected": monitors_with_changes
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(final_report, f, ensure_ascii=False, indent=2)

    print("\n" + "="*50, flush=True)
    print("ANALYSIS SUMMARY", flush=True)
    print("="*50, flush=True)
    print(f"Report Generated: {summary_stats['timestamp']}", flush=True)
    print(f"Total Monitors: {total_monitors}", flush=True)
    print(f"Monitors with checks: {monitors_with_checks}", flush=True)
    print(f"Total Checks Run: {total_checks_count}", flush=True)
    print(f"  - Completed: {completed_checks_count}", flush=True)
    print(f"  - Failed: {failed_checks_count}", flush=True)
    print(f"Monitors with changes detected: {len(monitors_with_changes)}", flush=True)
    print(f"Report written to: {report_file}", flush=True)
    print("="*50, flush=True)

    if monitors_with_changes:
        print("\nMonitors with detected changes:", flush=True)
        for m in monitors_with_changes:
            print(f"  - {m['name']} ({m['monitor_id']}):", flush=True)
            for ch in m['changes']:
                print(f"    Check {ch['check_id']} at {ch['created_at']}: {ch['summary']}", flush=True)

if __name__ == "__main__":
    main()
