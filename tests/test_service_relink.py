import json
import shutil
from datetime import datetime, timezone

from scripts import link_services
from utils import offer_extraction_llm, service_mapping_confirmations
from utils.clinic_service_taxonomy import SERVICE_NAME_ENUM_VALUES
from utils.data_quality_audit import audit_snapshot
from utils.service_linking import render_apply

AT = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
URL = "https://clinic.example/specials"
SERVICES = {12: "Neurotoxin", 13: "Facial", 14: "Chemical Peel", 15: "Botox"}


class FakeClient:
    """代替 LLM 客户端：按 item 名称回答，记录被问到的 item。"""
    answers = {"Bunny Lines": "Neurotoxin", "Glow Ritual": "Chemical Peel", "Mystery Day": "NONE"}

    def __init__(self):
        self.asked = []

    def create_json_response(self, messages, *, json_schema=None):
        batch = json.loads(messages[-1]["content"])
        self.asked.extend(entry["item_name"] for entry in batch)
        return {"results": [{"index": e["index"], "service_name": self.answers[e["item_name"]]} for e in batch]}


def graph():
    """quality 快照：4 个 offer，各 1 个已关联服务的 item。"""
    rows = [(1, "Bunny Lines $99 (regular $135)", "Bunny Lines", 12, False),
            (2, "Glow Ritual $150", "Glow Ritual", 13, False),
            (3, "Mystery Day $50", "Mystery Day", 13, False),
            (4, "Iconique Lip Reset $1,200", "Botox", 15, True)]
    offers = [{"id": i, "business_id": 7, "promotion_id": 1, "membership_plan_id": None, "is_active": True,
               "price_model": "total", "is_package": package, "is_membership_required": False,
               "is_new_customer_required": False, "conditions": [], "discount_price": None, "regular_price": None,
               "offer_raw_text": text} for i, text, _, _, package in rows]
    items = [{"offer_item_id": 100 + i, "offer_id": i, "item_name": name, "service_id": sid,
              "delivered_units": None, "unit_price": None} for i, _, name, sid, _ in rows]
    services = [{"service_id": sid, "business_id": 7, "service_name": name, "service_area": None,
                 "unit_type": None, "source_url": URL} for sid, name in SERVICES.items()]
    return {"promotions": [{"promotion_id": 1, "business_id": 7, "source_url": URL, "is_active": True,
                            "campaign_end_date": None, "promotion_content": []}],
            "memberships": [], "services": services, "offers": offers, "items": items}


def link_snapshot(g):
    tables = {"items": g["items"], "offers": g["offers"], "services": g["services"],
              "promotions": g["promotions"], "memberships": [],
              "enums": [{"enumlabel": name, "enumsortorder": i} for i, name in enumerate(SERVICE_NAME_ENUM_VALUES)]}
    return {name: {"count": len(rows), "rows": rows} for name, rows in tables.items()}


def run_link_services_plan(tmp_path, monkeypatch, g, report):
    """按调用方方式运行 link-services plan：快照文件、quality 状态目录、替换后的 LLM 客户端。
    返回计划、LLM 客户端与写入后的确认文件。"""
    confirmations = tmp_path / "service_mapping_confirmations.json"
    shutil.copy(service_mapping_confirmations.CONFIRMATIONS_PATH, confirmations)
    monkeypatch.setattr(service_mapping_confirmations, "CONFIRMATIONS_PATH", confirmations)
    client = FakeClient()
    monkeypatch.setattr(link_services, "build_client_from_env", lambda: client)
    state = tmp_path / "state"
    (state / "data_quality").mkdir(parents=True)
    (state / "data_quality" / "report.json").write_text(json.dumps(report))
    snapshot = tmp_path / "run" / "snapshot.json"
    snapshot.parent.mkdir()
    snapshot.write_text(json.dumps(link_snapshot(g), default=str))
    assert link_services.main(["plan", "--snapshot", str(snapshot), "--state-dir", str(state)]) == 0
    return json.loads((snapshot.parent / "plan.json").read_text()), client, json.loads(confirmations.read_text())


def test_quality_relink_items_are_redecided_by_link_services(tmp_path, monkeypatch):
    g = graph()
    report = audit_snapshot(g, at=AT)
    relinked = sorted(i["id"] for i in report["issues"] if i["status"] == "relink")
    assert relinked == [101, 102, 103]
    dictionary = offer_extraction_llm.SERVICE_NAME_DICT_PATH.read_text()
    plan, client, confirmations = run_link_services_plan(tmp_path, monkeypatch, g, report)
    assert sorted(client.asked) == ["Bunny Lines", "Glow Ritual", "Mystery Day"]
    outcome = {e["offer_item_id"]: (e["action"], e["service_id"], e["current_service_id"]) for e in plan["items"]}
    # 判定一致保留，判出另一服务改映射，判不出置 NULL
    assert outcome == {101: ("keep", 12, 12), 102: ("link", 14, 13), 103: ("unlink", None, 13)}
    assert plan["counts"] == {"link": 1, "create_and_link": 0, "unresolved": 0, "keep": 1, "unlink": 1}
    # 确认按"offer 原文 + item 名称"记录，不进入全局别名词典
    confirmed = {"offer_raw_text": "Bunny Lines $99 (regular $135)", "item_name": "Bunny Lines",
                 "service_name": "Neurotoxin"}
    assert plan["confirmed_mappings"] == [{**confirmed, "offer_item_ids": [101]}]
    assert confirmed in confirmations["confirmations"]
    assert offer_extraction_llm.SERVICE_NAME_DICT_PATH.read_text() == dictionary
    again = audit_snapshot(g, at=AT)
    assert sorted(i["id"] for i in again["issues"] if i["status"] == "relink") == [102, 103]
    # 原文变化后确认不再适用，重新交给 link-services
    g["offers"][0]["offer_raw_text"] = "Bunny Lines $89 (regular $135)"
    assert 101 in [i["id"] for i in audit_snapshot(g, at=AT)["issues"] if i["status"] == "relink"]


def test_relink_apply_sql_checks_the_current_mapping(tmp_path, monkeypatch):
    g = graph()
    plan, _, _ = run_link_services_plan(tmp_path, monkeypatch, g, audit_snapshot(g, at=AT))
    text = render_apply(plan)
    link = next(line for line in text.splitlines() if "set service_id = v_ref" in line)
    assert "i.service_id is not distinct from r.current_id" in link
    assert '[[102, "14", 13]]' in text
    unlink = next(line for line in text.splitlines() if "set service_id = null" in line)
    assert "i.service_id = r.current_id" in unlink and "[[103, 13]]" in text
    # 保留的映射不写库；blanket 检查只覆盖本批新关联的 item
    assert "101" not in text and "any('{102}'" in text


def test_blanket_item_reported_for_relink_stays_unlinked(tmp_path, monkeypatch):
    g = graph()
    g["offers"][2]["offer_raw_text"] = "Mystery Day: 20% off all services"
    report = {"issues": [{"rule": "service_scope_evidence", "table": "promo_offer_items", "id": 103,
                          "status": "relink"}]}
    plan, client, _ = run_link_services_plan(tmp_path, monkeypatch, g, report)
    (entry,) = plan["items"]
    assert (entry["action"], entry["reason"], client.asked) == ("unlink", "blanket_offer", [])
