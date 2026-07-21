from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _legacy_state(value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"consecutive_failures": value}
    return value if isinstance(value, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--issues", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    previous = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    state: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for item in report:
        key = item.get("source_key") or f"work:{item['work_id']}"
        old = _legacy_state(previous.get(key, previous.get(item.get("source_id"), 0)))
        failures = 0 if item["ok"] else old.get("consecutive_failures", 0) + 1
        moved = bool(item["ok"] and item.get("final_url") and item["final_url"] != item["url"])
        fingerprint_changed = bool(
            item["ok"]
            and old.get("sha256")
            and item.get("sha256")
            and old["sha256"] != item["sha256"]
        )
        state[key] = {
            "consecutive_failures": failures,
            "sha256": item.get("sha256") if item["ok"] else old.get("sha256"),
            "sha256_scope": item.get("sha256_scope") if item["ok"] else old.get("sha256_scope"),
            "final_url": item.get("final_url") if item["ok"] else old.get("final_url"),
            "etag": item.get("etag") if item["ok"] else old.get("etag"),
            "last_modified": item.get("last_modified") if item["ok"] else old.get("last_modified"),
            "checked_at": item.get("checked_at"),
        }
        reasons = []
        if failures >= 3:
            reasons.append("three-consecutive-failures")
        if moved and old.get("final_url") and old["final_url"] != item["final_url"]:
            reasons.append("redirect-target-changed")
        if fingerprint_changed:
            reasons.append("fingerprint-changed")
        if reasons:
            issues.append({**item, "consecutive_failures": failures, "monitor_reasons": reasons})

    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.issues.write_text(json.dumps(issues, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(issues)} source(s) require a stale/drift issue; registry data was not changed.")


if __name__ == "__main__":
    main()
