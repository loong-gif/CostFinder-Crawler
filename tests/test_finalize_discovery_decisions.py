from scripts.finalize_discovery_decisions import apply_render_verdicts

URL = "https://spa.example/specials"


def test_render_verdict_only_settles_pages_waiting_for_it():
    decisions = [
        {"url": URL, "business_id": 1, "status": "needs_review", "decision": "add_with_review"},
        # 同一页面在另一家分店的记录被审查跳过（重复证据）：渲染可见也不重新加入
        {"url": URL, "business_id": 2, "status": "needs_review", "decision": "skip", "reasons": ["duplicate_evidence"]},
        {"url": "https://spa.example/hidden", "business_id": 1, "status": "needs_review", "decision": "add_with_review"},
        {"url": "https://spa.example/offers", "business_id": 1, "status": "valid", "decision": "add_with_review"},
    ]
    deep = {URL: {"verdict": "verified"}, "https://spa.example/hidden": {"verdict": "not_visible"}}
    apply_render_verdicts(decisions, deep)
    assert [r["decision"] for r in decisions] == ["add", "skip", "skip", "add"]
    assert decisions[1]["reasons"] == ["duplicate_evidence"]
