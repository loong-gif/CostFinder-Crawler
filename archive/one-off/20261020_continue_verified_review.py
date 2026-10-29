import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.monitor_image_evaluation import validate_truth
from utils.monitor_price_store import atomic_json, now

DIRECTORY = ROOT / "output/monitor_price_sync/image_accuracy_20261012"
OUT = ROOT / "output/monitor_price_sync/remaining_sync_20261020"


def main():
    manifest = json.loads((DIRECTORY / "sample_manifest.json").read_text())
    truth = json.loads((DIRECTORY / "truth_draft.json").read_text())
    rows = {row["sample_id"]: row for row in manifest["samples"]}
    reviewed = {}
    decisions = {}
    for file in sorted(OUT.glob("owner_review*.json")):
        batch = json.loads(file.read_text())
        for sid, decision in batch["samples"].items():
            assert sid not in decisions, "重复核验责任: " + sid
            decisions[sid] = {**decision, "reviewed_at": batch["reviewed_at"]}
    assert decisions, "没有核验结论"
    for sid, decision in decisions.items():
        row = rows[sid]
        shots = []
        names = list(decision.get("screenshot_paths", row["screenshot_paths"]))
        if sid.startswith("VR133_"):
            names.append(str(ROOT / "output/monitor_price_sync/visual_review_20261010/luna_1/VR133/refined_00_y0.png"))
        for name in dict.fromkeys(names):
            path = Path(name)
            with Image.open(path) as image:
                width, height = image.size
            shots.append({"path": str(path), "region": {"x": 0, "y": 0, "width": width, "height": height},
                          "capture_session_time": row["observed_at"], "individual_capture_time": None,
                          "capture_time_note": "沿用清单记录的历史抓取时间，不代表本轮重新抓取。"})
        reviewed[sid] = {key: row[key] for key in ("sample_id", "merchant", "split", "url")}
        reviewed[sid].update(status=decision["status"], owner_verified_at=decision["reviewed_at"],
                             screenshots=shots, facts=decision["facts"], notes=decision.get("notes", []),
                             adjudication=decision["adjudication"])
    subset = {"samples": [rows[sid] for sid in reviewed]}
    validation = validate_truth(subset, {"samples": reviewed})
    assert validation["valid"], validation
    prior = OUT / "truth_before_remaining.json"
    if not prior.exists():
        atomic_json(prior, truth)
        atomic_json(OUT / "manifest_before_remaining.json", manifest)
    base = json.loads(prior.read_text())["samples"]
    assert all(truth["samples"][sid] == sample for sid, sample in base.items()), "此前核验内容发生变化"
    assert not set(reviewed).intersection(base), "本轮不得覆盖此前已经完成的核验"
    for sid in set(truth["samples"]) - set(base):
        assert sid in reviewed, "本轮清单不能删除已保存结论"
    truth["samples"].update(reviewed)
    truth.update(reviewed_sample_count=len(truth["samples"]), updated_at=now(),
                 freeze_status="pending_remaining_owner_review")
    atomic_json(DIRECTORY / "truth_draft.json", truth)
    for sid, sample in reviewed.items():
        rows[sid].update(owner_review_status="verified_" + sample["status"],
                         truth_reference="truth_draft.json#" + sid,
                         screenshot_paths=[shot["path"] for shot in sample["screenshots"]])
    manifest.update(reviewed_sample_count=len(truth["samples"]),
                    freeze_status="pending_remaining_owner_review", updated_at=now())
    atomic_json(DIRECTORY / "sample_manifest.json", manifest)
    pending = [sid for sid in rows if sid not in truth["samples"]]
    receipt = {"checked_at": now(), "total": len(rows), "previously_reviewed": len(base),
               "newly_reviewed": len(reviewed), "reviewed": len(truth["samples"]),
               "remaining": len(pending), "pending_ids": pending, "validation": validation,
               "database_writes": 0, "schema_changes": 0}
    atomic_json(OUT / "progress.json", receipt)
    print(json.dumps({key: value for key, value in receipt.items() if key != "pending_ids"}))


if __name__ == "__main__":
    main()
