import json
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent / "outputs/candidates_20261020"
groups = defaultdict(list)
for path in (OUT / "requests").glob("*/scrape_*.json"):
    capture = json.loads(path.read_text())
    markdown = (capture.get("response", {}).get("data") or {}).get("markdown")
    if markdown and capture["response"].get("success", True):
        groups[(path.parent.name, capture["request"]["url"], markdown)].append(str(path.relative_to(OUT)))
duplicates = [paths for paths in groups.values() if len(paths) > 1]
print(json.dumps({"unique_source_captures": len(groups), "identical_extra_captures": sum(len(paths)-1 for paths in duplicates), "duplicate_groups": len(duplicates)}, ensure_ascii=False))
