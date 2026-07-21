from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from registry_io import ROOT, load_entities


USER_AGENT = "BioBench-Atlas/1.1 source monitor"
WEB_FINGERPRINT_LIMIT = 2 * 1024 * 1024


def _sources() -> list[dict[str, Any]]:
    entities = load_entities()
    sources: list[dict[str, Any]] = []
    for work in entities["work"]:
        sources.append({
            "source_key": f"work:{work['id']}",
            "source_type": "work",
            "source_id": work["id"],
            "benchmark_id": None,
            "url": work["canonical_url"],
            "pin": None,
        })
    for benchmark in entities["benchmark"]:
        for index, resource in enumerate(benchmark["resources"]):
            resource_id = resource.get("id", f"{benchmark['id']}-legacy-resource-{index + 1}")
            pin = resource.get("pin")
            sources.append({
                "source_key": f"resource:{resource_id}",
                "source_type": "resource",
                "source_id": resource_id,
                "benchmark_id": benchmark["id"],
                "url": (pin or {}).get("url") or resource["url"],
                "canonical_url": resource["url"],
                "resource_type": resource["type"],
                "pin": pin,
            })
    return sources


def _check(source: dict[str, Any]) -> dict[str, Any]:
    checked_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    try:
        response = requests.get(
            source["url"],
            timeout=40,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": USER_AGENT},
        )
        status = response.status_code
        ok = status < 400
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        is_pdf = content_type == "application/pdf" or response.url.lower().endswith(".pdf")
        digest = hashlib.sha256()
        bytes_hashed = 0
        fingerprint_complete = True
        if ok:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if not is_pdf and bytes_hashed + len(chunk) > WEB_FINGERPRINT_LIMIT:
                    remaining = WEB_FINGERPRINT_LIMIT - bytes_hashed
                    if remaining > 0:
                        digest.update(chunk[:remaining])
                        bytes_hashed += remaining
                    fingerprint_complete = False
                    break
                digest.update(chunk)
                bytes_hashed += len(chunk)
        result = {
            **source,
            "checked_at": checked_at,
            "ok": ok,
            "status": status,
            "final_url": response.url,
            "content_type": content_type or None,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "sha256": digest.hexdigest() if ok else None,
            "sha256_scope": "full-pdf" if is_pdf and ok else ("full" if fingerprint_complete and ok else "first-2-mib" if ok else None),
            "bytes_hashed": bytes_hashed,
            "error": None,
        }
        response.close()
        return result
    except requests.RequestException as exc:
        return {
            **source,
            "checked_at": checked_at,
            "ok": False,
            "status": None,
            "final_url": None,
            "content_type": None,
            "etag": None,
            "last_modified": None,
            "sha256": None,
            "sha256_scope": None,
            "bytes_hashed": 0,
            "error": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "source-check-report.json")
    args = parser.parse_args()
    report = [_check(source) for source in _sources()]
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [item for item in report if not item["ok"]]
    works = sum(item["source_type"] == "work" for item in report)
    resources = len(report) - works
    print(f"Checked {works} works and {resources} resources; {len(failures)} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
