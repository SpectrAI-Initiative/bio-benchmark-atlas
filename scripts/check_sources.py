from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

from registry_io import ROOT, load_entities


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "source-check-report.json")
    args = parser.parse_args()
    works = load_entities()["work"]
    report = []
    for work in works:
        url = work["canonical_url"]
        try:
            response = requests.get(url, timeout=25, allow_redirects=True, stream=True, headers={"User-Agent": "BioBench-Atlas/1.0 source monitor"})
            status = response.status_code
            final_url = response.url
            ok = status < 400
            response.close()
            error = None
        except requests.RequestException as exc:
            status = None
            final_url = None
            ok = False
            error = str(exc)
        report.append({"work_id": work["id"], "url": url, "ok": ok, "status": status, "final_url": final_url, "error": error})
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [item for item in report if not item["ok"]]
    print(f"Checked {len(report)} sources; {len(failures)} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

