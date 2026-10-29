import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.extract_clinic_promotions_from_scrape_raw import run  # noqa: E402
from utils.offer_extraction_llm import build_client_from_env  # noqa: E402
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key  # noqa: E402


class CachedLLM:
    """Persists each extraction response so the dry run and the apply run read identical claims."""

    def __init__(self, llm, path: Path) -> None:
        self.llm = llm
        self.path = path
        self.cache = json.loads(path.read_text()) if path.exists() else {}

    def create_json_response(self, messages, json_schema=None, **kwargs):
        key = hashlib.sha256(json.dumps([messages, json_schema], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if key not in self.cache:
            self.cache[key] = self.llm.create_json_response(messages, json_schema=json_schema, **kwargs)
            self.path.write_text(json.dumps(self.cache, ensure_ascii=False))
        return self.cache[key]


parser = argparse.ArgumentParser(description="clinic_promotions extraction for the discovered pages with the configured LLM")
parser.add_argument("--batch_date", required=True)
parser.add_argument("--limit", type=int)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--llm-cache", type=Path, required=True)
parser.add_argument("--apply", action="store_true", help="write accepted rows; requires --visibility-manifest")
parser.add_argument("--visibility-manifest", type=Path)
args = parser.parse_args()

load_dotenv(PROJECT_ROOT / ".env")
assert os.getenv("COSTFINDER_RAW_DIR"), "set COSTFINDER_RAW_DIR to the isolated raw batch"
if args.apply and args.visibility_manifest is None:
    raise RuntimeError("--visibility-manifest is required with --apply")
llm = build_client_from_env()
assert llm is not None, "LLM_API_KEY / LLM_MODEL are not configured"
visibility = {}
if args.visibility_manifest is not None:
    payload = json.loads(args.visibility_manifest.read_text())
    visibility = payload.get("results", payload)
client = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_secret_key())
audit = run(client=client, llm=CachedLLM(llm, args.llm_cache), batch_date=args.batch_date, limit=args.limit,
            apply=args.apply, rendered_visibility=visibility)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in audit.items() if k != "results"}, ensure_ascii=False))
