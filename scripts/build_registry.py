from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from registry_io import ROOT, load_changelog, load_meta, load_taxonomies, without_internal_fields, write_csv, write_json
from validate_registry import validate_registry


def _reported(value: dict[str, Any]) -> str:
    if value["reporting_status"] != "reported":
        return "Not reported"
    raw = value["value"]
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    if isinstance(raw, list):
        return "; ".join(str(item) for item in raw)
    return str(raw)


def _build_payload() -> dict[str, Any]:
    entities = validate_registry()
    meta = load_meta()
    taxonomies = load_taxonomies()
    verified = {
        kind: sorted(
            [without_internal_fields(item) for item in records if item["verification"]["status"] == "verified"],
            key=lambda item: item["id"],
        )
        for kind, records in entities.items()
    }
    work_by_id = {item["id"]: item for item in verified["work"]}
    model_by_id = {item["id"]: item for item in verified["model"]}
    runs_by_benchmark: defaultdict[str, list[str]] = defaultdict(list)
    works_by_benchmark: defaultdict[str, set[str]] = defaultdict(set)
    for run in verified["evaluation_run"]:
        runs_by_benchmark[run["benchmark_id"]].append(run["id"])
        works_by_benchmark[run["benchmark_id"]].add(run["work_id"])
    benchmarks = []
    for benchmark in verified["benchmark"]:
        enriched = dict(benchmark)
        enriched["audit"] = benchmark.get("audit", {
            "status": "legacy", "audited_date": None, "unresolved_fields": 0,
            "notes": "Not yet processed by the v1.1 field-level audit.",
        })
        enriched["field_status"] = benchmark.get("field_status", [])
        enriched["evaluation_run_ids"] = sorted(runs_by_benchmark[benchmark["id"]])
        enriched["evaluating_work_ids"] = sorted(works_by_benchmark[benchmark["id"]])
        benchmarks.append(enriched)
    return {
        "meta": meta,
        "taxonomies": taxonomies,
        "changelog": load_changelog(),
        "benchmarks": benchmarks,
        "works": verified["work"],
        "models": verified["model"],
        "evaluation_runs": verified["evaluation_run"],
    }


def main() -> None:
    payload = _build_payload()
    exports = ROOT / "exports"
    public_data = ROOT / "site" / "public" / "data"
    generated = ROOT / "site" / "src" / "generated"
    public_schema = ROOT / "site" / "public" / "schema"

    public_data.mkdir(parents=True, exist_ok=True)

    write_json(exports / "registry.json", payload)
    write_json(exports / "benchmarks.json", payload["benchmarks"])
    write_json(exports / "works.json", payload["works"])
    write_json(exports / "models.json", payload["models"])
    write_json(exports / "evaluation-runs.json", payload["evaluation_runs"])
    write_json(generated / "registry.json", payload)
    for filename in ("registry.json", "benchmarks.json", "works.json", "models.json", "evaluation-runs.json"):
        shutil.copy2(exports / filename, public_data / filename)
    public_schema.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "schema" / "registry.schema.json", public_schema / "registry.schema.json")

    benchmark_rows = []
    for item in payload["benchmarks"]:
        benchmark_rows.append({
            "id": item["id"], "name": item["name"], "kind": item["kind"], "parent_id": item["parent_id"] or "",
            "release_date": item["release_date"], "latest_version": item["latest_version"],
            "task_count": item["task_counts"]["total"] if item["task_counts"]["total"] is not None else "",
            "domains": ";".join(item["domains"]), "capabilities": ";".join(item["capabilities"]),
            "modalities": ";".join(item["modalities"]), "access": item["access"]["level"],
            "evaluation_runs": len(item["evaluation_run_ids"]), "works": len(item["evaluating_work_ids"]),
            "audit_status": item["audit"]["status"],
            "provisional_fields": ";".join(
                status["path"] for status in item["field_status"] if status["status"] == "provisional"
            ),
            "conflicted_fields": ";".join(
                status["path"] for status in item["field_status"] if status["status"] == "conflicted"
            ),
            "last_verified": item["verification"]["last_verified"],
        })
    benchmark_fields = ["id", "name", "kind", "parent_id", "release_date", "latest_version", "task_count", "domains", "capabilities", "modalities", "access", "evaluation_runs", "works", "audit_status", "provisional_fields", "conflicted_fields", "last_verified"]
    write_csv(exports / "benchmarks.csv", benchmark_fields, benchmark_rows)
    shutil.copy2(exports / "benchmarks.csv", public_data / "benchmarks.csv")

    result_rows = []
    for run in payload["evaluation_runs"]:
        for result in run["results"]:
            result_rows.append({
                "evaluation_run_id": run["id"], "work_id": run["work_id"], "benchmark_id": run["benchmark_id"],
                "benchmark_version": run["benchmark_version"] or "", "scope": run["scope"]["type"],
                "subset_id": run["scope"]["subset_id"] or "", "n": run["scope"]["n"] or "",
                "model_id": result["model_id"], "metric_id": result["metric_id"], "value": result["value"],
                "ci_low": result["ci_low"] if result["ci_low"] is not None else "", "ci_high": result["ci_high"] if result["ci_high"] is not None else "",
                "shots": _reported(run["protocol"]["shots"]), "turns": _reported(run["protocol"]["turns"]),
                "browser": _reported(run["protocol"]["tools"]["browser"]), "internet": _reported(run["protocol"]["tools"]["internet"]),
                "code_execution": _reported(run["protocol"]["tools"]["code_execution"]), "repeats": _reported(run["protocol"]["repeats"]),
                "grader": run["protocol"]["grader"]["type"] or "Not reported", "comparability_group": run["comparability_group"],
                "result_status": result.get("status", "legacy"),
                "confidence": result.get("confidence", ""),
                "result_evidence_ids": ";".join(result.get("evidence_ids", [])),
            })
    result_fields = ["evaluation_run_id", "work_id", "benchmark_id", "benchmark_version", "scope", "subset_id", "n", "model_id", "metric_id", "value", "ci_low", "ci_high", "shots", "turns", "browser", "internet", "code_execution", "repeats", "grader", "comparability_group", "result_status", "confidence", "result_evidence_ids"]
    write_csv(exports / "evaluation-results.csv", result_fields, result_rows)
    shutil.copy2(exports / "evaluation-results.csv", public_data / "evaluation-results.csv")
    print(f"Built registry {payload['meta']['version']} with {len(payload['benchmarks'])} benchmarks and {len(payload['evaluation_runs'])} evaluation runs.")


if __name__ == "__main__":
    main()
