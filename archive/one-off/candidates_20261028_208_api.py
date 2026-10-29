import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path("/home/loong/costfinder_monitor")
sys.path.insert(0, str(ROOT))
from scripts.discover_new_promo_pages import Firecrawl

load_dotenv(ROOT / ".env")
request = json.load(sys.stdin)
assert request["path"] in {"/v1/map", "/v1/search", "/v1/scrape"}
client = Firecrawl("http://127.0.0.1:3002", os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), timeout=120)
print(json.dumps(client._post(request["path"], request["body"]), ensure_ascii=False))
