from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--issues", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    previous = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    state: dict[str, int] = {}
    issues = []
    for item in report:
        work_id = item["work_id"]
        failures = 0 if item["ok"] else previous.get(work_id, 0) + 1
        state[work_id] = failures
        if failures >= 3:
            issues.append({**item, "consecutive_failures": failures})

    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.issues.write_text(json.dumps(issues, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(issues)} source(s) reached the three-check stale threshold.")


if __name__ == "__main__":
    main()
