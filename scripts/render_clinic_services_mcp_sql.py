#!/usr/bin/env python3
"""Render an extraction audit's MCP operations as a transaction SQL script."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.clinic_services_mcp_sql import render_mcp_operations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    operations = [
        write["mcp_operation"]
        for result in audit.get("results", [])
        for write in result.get("writes", [])
        if write.get("mcp_operation")
    ][:args.limit]
    print(render_mcp_operations(operations))


if __name__ == "__main__":
    main()
