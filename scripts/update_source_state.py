from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ACCESS_LIMITED_STATUSES = {401, 403, 429}


def _legacy_state(value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"consecutive_failures": value}
    return value if isinstance(value, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--issues", type=Path, required=True)
    parser.add_argument("--drift-report", type=Path)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    previous = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    state: dict[str, dict[str, Any]] = {}
    issues_by_url: dict[str, dict[str, Any]] = {}
    drift: list[dict[str, Any]] = []
    for item in report:
        key = item.get("source_key") or f"work:{item['work_id']}"
        old = _legacy_state(previous.get(key, previous.get(item.get("source_id"), 0)))
        access_limited = item.get("status") in ACCESS_LIMITED_STATUSES
        failures = (
            0
            if item["ok"] or access_limited
            else old.get("consecutive_failures", 0) + 1
        )
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
        issue_reasons = []
        if failures >= 3:
            issue_reasons.append("three-consecutive-failures")
        drift_reasons = []
        if moved and old.get("final_url") and old["final_url"] != item["final_url"]:
            drift_reasons.append("redirect-target-changed")
        if fingerprint_changed:
            drift_reasons.append("fingerprint-changed")
        if access_limited:
            drift_reasons.append("monitor-access-limited")
        if issue_reasons:
            issue_url = item.get("url") or key
            related_source = {
                "source_type": item["source_type"],
                "source_id": item["source_id"],
            }
            if issue_url in issues_by_url:
                issues_by_url[issue_url]["related_sources"].append(related_source)
            else:
                issues_by_url[issue_url] = {
                    **item,
                    "consecutive_failures": failures,
                    "monitor_reasons": issue_reasons,
                    "related_sources": [related_source],
                }
        if drift_reasons:
            drift.append({
                **item,
                "consecutive_failures": failures,
                "monitor_reasons": drift_reasons,
            })

    issues = list(issues_by_url.values())
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.issues.write_text(json.dumps(issues, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.drift_report:
        args.drift_report.write_text(
            json.dumps(drift, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        f"{len(issues)} source(s) require a failure issue; "
        f"{len(drift)} source(s) changed fingerprint or redirect target. "
        "Registry data was not changed."
    )


if __name__ == "__main__":
    main()
