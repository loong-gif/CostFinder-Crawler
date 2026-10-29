import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.render_review import build_deep_items, shard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue every page without a verified render verdict for the deep review")
    parser.add_argument("--decisions", default="state/discovery/monitor_decisions_final.json")
    parser.add_argument("--out-dir", default="state/discovery/render_review/deep")
    parser.add_argument("--shards", type=int, default=4)
    args = parser.parse_args()

    decisions = json.loads(Path(args.decisions).read_text())["decisions"]
    items = build_deep_items(decisions)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, part in enumerate(shard(items, args.shards), start=1):
        path = out_dir / f"shard{index}.json"
        path.write_text(json.dumps({"name": f"deep{index}", "items": part}, ensure_ascii=False, indent=1))
        print(path, len(part))
    print("items", len(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
