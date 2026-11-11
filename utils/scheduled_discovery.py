from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path

from utils.offer_stage import snapshot_tables as offer_tables
from utils.page_extraction_snapshot import load_decision_pages, snapshot_tables_for_pages
from utils.pipeline_db import fresh_snapshot
from utils.pipeline_run import PROJECT_ROOT
from utils.scheduled_discovery_registration import run_registration
from utils.service_linking import snapshot_tables as service_tables

ROOT = PROJECT_ROOT / "state" / "scheduled_discovery"
PENDING = PROJECT_ROOT / "state" / "pending_sql"
COMMAND = PROJECT_ROOT / "scripts" / "promo_pipeline.py"
NEXT = {"pages": "offers", "offers": "link", "link": "activate", "activate": "finish"}
MAX_CITIES_PER_DAY = 5


def pending_status(directory: Path, name: str) -> str:
    paths = {key: directory / folder / name for key, folder in (("applied", "applied"), ("rejected", "rejected"))}
    paths["waiting"] = directory / name
    found = [key for key, path in paths.items() if path.is_file()]
    if len(found) > 1:
        raise RuntimeError(f"duplicate pending SQL state: {name}: {found}")
    return found[0] if found else "missing"


def save(batch: Path, state: dict) -> None:
    path = batch / "state.json"
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def command(*args: str, raw: Path | None = None) -> None:
    env = os.environ.copy()
    if raw is not None:
        env["COSTFINDER_RAW_DIR"] = str(raw)
    subprocess.run([sys.executable, str(COMMAND), *map(str, args)], cwd=PROJECT_ROOT, env=env, check=True)


def ids_args(ids: list[int]) -> list[str]:
    return [value for business_id in ids for value in ("--business-id", str(business_id))]


def prepare_sql(batch: Path, state: dict) -> None:
    stage = state["stage"]
    ids = state["business_ids"]
    attempt = state.get("attempt", 0)
    cycle = state.get("cycle", 0)
    work = batch / "plans" / str(cycle) / stage / str(attempt)
    work.mkdir(parents=True, exist_ok=True)
    decisions = batch / "pages" / "decisions.json"
    if stage == "pages":
        pages = load_decision_pages(json.loads(decisions.read_text()), business_ids=set(ids))
        tables = snapshot_tables_for_pages(pages)
        arguments = ["extract-pages", "plan", "--decisions", str(decisions), "--raw-dir", str(batch / "raw")]
    elif stage in ("offers", "activate"):
        tables = offer_tables(business_ids=ids)
        arguments = ["extract-offers", "plan", "--state-dir", str(batch)]
    else:
        tables = service_tables(ids)
        arguments = ["link-services", "plan", "--state-dir", str(batch)]
    snapshot = fresh_snapshot(work, tables)
    command(*arguments, *ids_args(ids), "--snapshot", str(snapshot))
    plan = work / "plan.json"
    command(arguments[0], "apply-sql", "--plan", str(plan))
    name = f"{batch.name}_{stage}_{cycle}_{attempt}.sql"
    state.update(stage=f"{stage}_wait", pending=name, sql=str(work / "apply.sql"))
    if stage == "pages":
        state["page_plan"] = str(plan)
    save(batch, state)


def queue_sql(directory: Path, state: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / state["pending"]
    temporary = target.with_suffix(".new")
    shutil.copyfile(state["sql"], temporary)
    temporary.replace(target)
    for folder in ("applied", "rejected"):
        completed = directory / folder / target.name
        if completed.exists():
            if completed.read_bytes() != target.read_bytes():
                raise RuntimeError(f"conflicting pending SQL: {target.name}")
            target.unlink()


def settle_sql(batch: Path, state: dict, directory: Path) -> bool:
    phase = state["stage"].removesuffix("_wait")
    status = pending_status(directory, state["pending"])
    if status == "missing":
        queue_sql(directory, state)
        return False
    if status == "waiting":
        return False
    if status == "rejected":
        state.update(stage=phase, attempt=state["attempt"] + 1)
    else:
        state.update(stage=NEXT[phase], attempt=0)
    save(batch, state)
    return True


def run_batch(batch: Path) -> str:
    state_file = batch / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"stage": "registration"}
    if not state_file.exists():
        save(batch, state)
    while True:
        stage = state["stage"]
        if stage == "registration":
            result = run_registration(batch)
            if result["status"] != "ready":
                if result["status"] == "idle":
                    state["stage"] = "idle"
                    save(batch, state)
                return result["status"]
            state.update(stage="discover", business_ids=result["business_ids"], attempt=0)
            save(batch, state)
        elif stage == "discover":
            if not state["business_ids"]:
                state["stage"] = "done"
            else:
                command("discover-pages", "--batch-dir", batch / "pages", "--limit", str(len(state["business_ids"])),
                        *ids_args(state["business_ids"]))
                state["stage"] = "review"
            save(batch, state)
        elif stage in ("review", "render", "crawl"):
            pages = batch / "pages"
            if stage == "review":
                command("review-pages", "--batch-dir", pages)
            elif stage == "render":
                command("render-pages", "--batch-dir", pages)
            else:
                records = json.loads((pages / "decisions.json").read_text())["decisions"]
                if any(record["decision"] == "add" for record in records):
                    command("crawl", "--decisions", pages / "decisions.json", raw=batch / "raw")
                else:
                    state.pop("page_plan", None)
                    state["stage"] = "finish"
                    save(batch, state)
                    continue
            state["stage"] = {"review": "render", "render": "crawl", "crawl": "pages"}[stage]
            save(batch, state)
        elif stage in NEXT:
            prepare_sql(batch, state)
        elif stage.endswith("_wait"):
            if not settle_sql(batch, state, PENDING):
                return "waiting"
        elif stage == "finish":
            decisions = json.loads((batch / "pages" / "decisions.json").read_text())["decisions"]
            discovered = json.loads((batch / "pages" / "discovered.json").read_text())
            summary = discovered["summary"]
            plan_path = Path(state["page_plan"]) if state.get("page_plan") else None
            entries = json.loads(plan_path.read_text())["entries"] if plan_path and plan_path.exists() else []
            if summary.get("unfinished") or summary.get("scrape_failed"):
                state["stage"] = "discover"
            elif any(d["decision"] == "add_with_review" for d in decisions):
                state["stage"] = "render"
            elif any(entry.get("reason") == "no_scrape" for entry in entries):
                state["stage"] = "crawl"
            else:
                state["stage"] = "done"
            if state["stage"] != "done":
                state["cycle"] = state.get("cycle", 0) + 1
            save(batch, state)
            if state["stage"] != "done":
                return "review"
        elif stage in ("done", "idle"):
            return stage
        else:
            raise ValueError(f"unknown discovery stage: {stage}")


def city_check_due(batches: list[Path], now: datetime) -> bool:
    if now.time() < time(4, 30):
        return False
    today = [batch for batch in batches if datetime.strptime(batch.name, "%Y%m%dT%H%M%S%fZ")
             .replace(tzinfo=timezone.utc).astimezone(now.tzinfo).date() == now.date()]
    if len(today) >= MAX_CITIES_PER_DAY:
        return False
    return not any((batch / "state.json").exists() and json.loads((batch / "state.json").read_text())["stage"] == "idle"
                   for batch in today)


def run() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        batches = sorted(path for path in ROOT.iterdir() if path.is_dir())
        registration_waiting = False
        for batch in batches:
            outcome = run_batch(batch)
            print(f"{batch}: {outcome}", flush=True)
            if outcome == "waiting" and json.loads((batch / "state.json").read_text())["stage"] == "registration":
                registration_waiting = True
        if registration_waiting or not city_check_due(batches, datetime.now().astimezone()):
            return
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        batch = ROOT / name
        batch.mkdir()
        outcome = run_batch(batch)
        print(f"{batch}: {outcome}", flush=True)
