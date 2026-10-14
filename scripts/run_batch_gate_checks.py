#!/usr/bin/env python3
"""Run the batch completion checks (A-E) on linked production; exit 1 on any undocumented row."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import run_batch_gate
from utils.monitor_price_store import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "output/data_quality_state")
    args = parser.parse_args()
    # {"C_blanket_service_link": {"<offer_item_id>": "why it stays"}, ...}
    retained = read_json(args.state_dir / "data_quality/gate_retained.json", {})
    gate = run_batch_gate(ROOT, retained)
    print(json.dumps(gate, ensure_ascii=False, indent=1, default=str))
    sys.exit(0 if gate["passed"] else 1)


if __name__ == "__main__":
    main()
