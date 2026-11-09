import argparse
import collections
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def apply_render_verdicts(decisions: list, deep: dict) -> None:
    """渲染核验结论写回等待核验的页面；有效页面直接加入；之后不应再有等待核验的页面。"""
    for record in decisions:
        verdict = deep.get(record["url"])
        # 渲染结论只决定等待核验的页面；审查已跳过的（重复证据、单店页面上限等）不因渲染可见而重新加入
        if verdict and record["decision"] == "add_with_review" and record.get("status") == "needs_review":
            record["render_review_deep"] = {k: verdict.get(k) for k in ("verdict", "reason", "source", "controls", "clicked", "images_read")}
            if verdict["verdict"] == "verified":
                record["decision"] = "add"
                record.setdefault("reasons", []).append("render_verified_after_expanding")
            else:
                record["decision"] = "skip"
                record.setdefault("reasons", []).append("not_visible_after_expanding_or_unreachable")
        elif record["decision"] == "add_with_review" and record.get("status") == "valid":
            record["decision"] = "add"
            record.setdefault("reasons", []).append("valid_page_concrete_offer")
    remaining = [r["url"] for r in decisions if r["decision"] == "add_with_review"]
    assert not remaining, remaining


def main() -> int:
    parser = argparse.ArgumentParser(description="Final add/skip decisions once the deep render review has run")
    parser.add_argument("--decisions", type=Path, default=PROJECT_ROOT / "state/discovery/monitor_decisions_final.json")
    parser.add_argument("--deep", type=Path, default=PROJECT_ROOT / "state/discovery/render_review/deep/verdicts.json")
    args = parser.parse_args()

    payload = json.loads(args.decisions.read_text())
    deep = json.loads(args.deep.read_text())
    apply_render_verdicts(payload["decisions"], deep)

    counts = collections.Counter(r["decision"] for r in payload["decisions"])
    payload["summary"] = dict(counts)
    args.decisions.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    urls = {r["url"] for r in payload["decisions"] if r["decision"] == "add"}
    businesses = {r["business_id"] for r in payload["decisions"] if r["decision"] == "add"}
    print("decisions", dict(counts), "urls", len(urls), "businesses", len(businesses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
