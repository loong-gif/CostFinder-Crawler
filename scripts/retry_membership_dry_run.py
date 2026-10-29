import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.repair_clinic_memberships_full_table import (  # noqa: E402
    DEFAULT_FIRECRAWL_URL,
    FirecrawlHTTP,
    build_dry_run,
    extract_many,
    scrape_many,
    source_key,
    write_json,
)
from utils.offer_extraction_llm import build_client_from_env  # noqa: E402

parser = argparse.ArgumentParser(description="Retry the failed scrapes and extractions of a membership dry run, then rebuild its plan")
parser.add_argument("--dir", type=Path, required=True, help="artifact directory of the original dry run")
parser.add_argument("--out", type=Path, required=True, help="new artifact directory")
parser.add_argument("--workers", type=int, default=2)
args = parser.parse_args()

load_dotenv(PROJECT_ROOT / ".env")
read = lambda name: json.loads((args.dir / name).read_text())  # noqa: E731
snapshot = read("snapshot.json")
main_pages = read("firecrawl_main_scrapes.json")
main_extractions = read("main_extractions.json")
fallback_pages = read("firecrawl_fallback_scrapes.json")
fallback_extractions = read("fallback_extractions.json")

fc = FirecrawlHTTP(os.getenv("COSTFINDER_FIRECRAWL_URL", DEFAULT_FIRECRAWL_URL), os.environ["FIRECRAWL_API_KEY"], request_interval=1.0)
llm = build_client_from_env()
assert llm is not None, "LLM_API_URL/LLM_API_KEY/LLM_MODEL are required"


def retry_pages(pages, fallback):
    failed = [p["requested_url"] for p in pages if p.get("error") or not p.get("markdown")]
    if not failed:
        return pages, 0
    fresh = {source_key(p["requested_url"]): p for p in scrape_many(fc, failed, fallback=fallback, max_workers=args.workers)}
    return [fresh.get(source_key(p["requested_url"]), p) if p["requested_url"] in failed else p for p in pages], len(failed)


def retry_extractions(pages, extractions):
    failed = {(source_key(e["requested_url"]), bool(e.get("fallback"))) for e in extractions if e.get("error")}
    retry = [p for p in pages if (source_key(p["requested_url"]), bool(p.get("fallback"))) in failed and p.get("markdown")]
    fresh = {(source_key(e["requested_url"]), bool(e.get("fallback"))): e for e in extract_many(llm, retry, max_workers=args.workers, min_interval=2.0)}
    merged = [fresh.get((source_key(e["requested_url"]), bool(e.get("fallback"))), e) for e in extractions]
    return merged, len(retry)


main_pages, rescraped_main = retry_pages(main_pages, False)
fallback_pages, rescraped_fallback = retry_pages(fallback_pages, True)
main_extractions, reextracted_main = retry_extractions(main_pages, main_extractions)
fallback_extractions, reextracted_fallback = retry_extractions(fallback_pages, fallback_extractions)

args.out.mkdir(parents=True, exist_ok=True)
write_json(args.out / "snapshot.json", snapshot)
write_json(args.out / "firecrawl_main_scrapes.json", main_pages)
write_json(args.out / "main_extractions.json", main_extractions)
write_json(args.out / "firecrawl_fallback_scrapes.json", fallback_pages)
write_json(args.out / "fallback_extractions.json", fallback_extractions)
dry_run = build_dry_run(snapshot, main_pages + fallback_pages, main_extractions + fallback_extractions, artifact_dir=args.out)
write_json(args.out / "dry_run.json", dry_run)
still_failed = sum(1 for e in main_extractions + fallback_extractions if e.get("error"))
print(json.dumps({"rescraped": [rescraped_main, rescraped_fallback], "reextracted": [reextracted_main, reextracted_fallback],
                  "still_failed_extractions": still_failed, "action_counts": dry_run["action_counts"]}, ensure_ascii=False))
