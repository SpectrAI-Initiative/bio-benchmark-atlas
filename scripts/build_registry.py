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


def _root_family_id(benchmark: dict[str, Any], benchmark_by_id: dict[str, dict[str, Any]]) -> str:
    current = benchmark
    while current["parent_id"] is not None:
        current = benchmark_by_id[current["parent_id"]]
    return current["id"]


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
    benchmark_uses = verified["benchmark_use"]
    evaluation_runs = []
    for run in verified["evaluation_run"]:
        enriched = dict(run)
        enriched["model_ids"] = sorted({
            *run.get("model_ids", []),
            *(result["model_id"] for result in run["results"]),
        })
        evaluation_runs.append(enriched)
    runs_by_benchmark: defaultdict[str, list[str]] = defaultdict(list)
    works_by_benchmark: defaultdict[str, set[str]] = defaultdict(set)
    uses_by_benchmark: defaultdict[str, list[str]] = defaultdict(list)
    uses_by_work: defaultdict[str, list[str]] = defaultdict(list)
    runs_by_work: defaultdict[str, list[str]] = defaultdict(list)
    for run in evaluation_runs:
        runs_by_benchmark[run["benchmark_id"]].append(run["id"])
        works_by_benchmark[run["benchmark_id"]].add(run["work_id"])
        runs_by_work[run["work_id"]].append(run["id"])
    for use in benchmark_uses:
        uses_by_benchmark[use["benchmark_id"]].append(use["id"])
        uses_by_work[use["work_id"]].append(use["id"])
        if use["relation_type"] == "evaluation":
            works_by_benchmark[use["benchmark_id"]].add(use["work_id"])
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
        enriched["benchmark_use_ids"] = sorted(uses_by_benchmark[benchmark["id"]])
        benchmarks.append(enriched)
    works = []
    for work in verified["work"]:
        enriched = dict(work)
        enriched["evaluation_run_ids"] = sorted(runs_by_work[work["id"]])
        enriched["benchmark_use_ids"] = sorted(uses_by_work[work["id"]])
        enriched["benchmark_ids"] = sorted({
            run["benchmark_id"] for run in evaluation_runs if run["work_id"] == work["id"]
        } | {
            use["benchmark_id"] for use in benchmark_uses if use["work_id"] == work["id"]
        })
        works.append(enriched)
    benchmark_by_id = {item["id"]: item for item in benchmarks}
    task_coverage = []
    for benchmark in benchmarks:
        classification = benchmark["scientific_task_classification"]
        flagged_paths = {item["path"] for item in benchmark["field_status"]}
        for index, entry in enumerate(classification["entries"]):
            path = f"/scientific_task_classification/entries/{index}"
            task_coverage.append({
                "task_type_id": entry["task_type_id"],
                "benchmark_id": benchmark["id"],
                "benchmark_name": benchmark["name"],
                "root_family_id": _root_family_id(benchmark, benchmark_by_id),
                "benchmark_kind": benchmark["kind"],
                "benchmark_version": classification["benchmark_version"],
                "as_of": classification["as_of"],
                "classification_status": classification["status"],
                "coverage": entry["coverage"],
                "mapping_method": entry["mapping_method"],
                "confidence": entry["confidence"],
                "count": entry["count"],
                "count_unit": entry["count_unit"],
                "count_basis": entry["count_basis"],
                "count_ref": entry["count_ref"],
                "reporting_status": entry["reporting_status"],
                "evidence_ids": entry["evidence_ids"],
                "notes": entry["notes"],
                "audit_status": benchmark["audit"]["status"],
                "evaluation_run_ids": benchmark["evaluation_run_ids"],
                "evaluating_work_ids": benchmark["evaluating_work_ids"],
                "aggregate_eligible": (
                    entry["confidence"] != "low"
                    and path not in flagged_paths
                    and entry["coverage"] != "not-in-scope"
                ),
            })
    task_coverage.sort(key=lambda item: (item["task_type_id"], item["root_family_id"], item["benchmark_id"]))

    scientific_tasks = []
    for term in taxonomies["scientific_tasks"]:
        positive = [
            item for item in task_coverage
            if item["task_type_id"] == term["id"] and item["aggregate_eligible"]
        ]
        enriched = dict(term)
        enriched["coverage_family_count"] = len({item["root_family_id"] for item in positive})
        enriched["coverage_track_count"] = len({
            item["benchmark_id"] for item in positive if item["benchmark_kind"] == "track"
        })
        enriched["official_work_count"] = len({
            work_id for item in positive for work_id in item["evaluating_work_ids"]
        })
        scientific_tasks.append(enriched)
    return {
        "meta": meta,
        "taxonomies": taxonomies,
        "changelog": load_changelog(),
        "benchmarks": benchmarks,
        "works": works,
        "models": verified["model"],
        "evaluation_runs": evaluation_runs,
        "benchmark_uses": benchmark_uses,
        "scientific_tasks": scientific_tasks,
        "scientific_task_coverage": task_coverage,
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
    write_json(exports / "benchmark-uses.json", payload["benchmark_uses"])
    write_json(exports / "scientific-tasks.json", payload["scientific_tasks"])
    write_json(exports / "scientific-task-coverage.json", payload["scientific_task_coverage"])
    write_json(generated / "registry.json", payload)
    for filename in ("registry.json", "benchmarks.json", "works.json", "models.json", "evaluation-runs.json", "benchmark-uses.json", "scientific-tasks.json", "scientific-task-coverage.json"):
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
            "scientific_task_ids": ";".join(
                entry["task_type_id"] for entry in item["scientific_task_classification"]["entries"]
            ),
            "task_classification_status": item["scientific_task_classification"]["status"],
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
    benchmark_fields = ["id", "name", "kind", "parent_id", "release_date", "latest_version", "task_count", "domains", "capabilities", "modalities", "access", "scientific_task_ids", "task_classification_status", "evaluation_runs", "works", "audit_status", "provisional_fields", "conflicted_fields", "last_verified"]
    write_csv(exports / "benchmarks.csv", benchmark_fields, benchmark_rows)
    shutil.copy2(exports / "benchmarks.csv", public_data / "benchmarks.csv")

    work_rows = []
    for item in payload["works"]:
        work_rows.append({
            "id": item["id"], "title": item["title"], "publication_date": item["publication_date"],
            "work_type": item["work_type"], "source_class": item["source_class"],
            "status": item["status"], "organizations": ";".join(item["organizations"]),
            "current_version_id": item["current_version_id"], "doi": item["doi"] or "",
            "arxiv": item["arxiv"] or "", "canonical_url": item["canonical_url"],
            "benchmark_ids": ";".join(item["benchmark_ids"]),
            "benchmark_use_ids": ";".join(item["benchmark_use_ids"]),
            "evaluation_run_ids": ";".join(item["evaluation_run_ids"]),
            "last_verified": item["verification"]["last_verified"],
        })
    work_fields = ["id", "title", "publication_date", "work_type", "source_class", "status", "organizations", "current_version_id", "doi", "arxiv", "canonical_url", "benchmark_ids", "benchmark_use_ids", "evaluation_run_ids", "last_verified"]
    write_csv(exports / "works.csv", work_fields, work_rows)
    shutil.copy2(exports / "works.csv", public_data / "works.csv")

    use_rows = []
    for item in payload["benchmark_uses"]:
        use_rows.append({
            "id": item["id"], "work_id": item["work_id"],
            "work_version_id": item["work_version_id"], "benchmark_id": item["benchmark_id"],
            "benchmark_version": item["benchmark_version"] or "", "relation_type": item["relation_type"],
            "status": item["status"], "scope": item["scope"]["type"],
            "subset_kind": item["scope"]["subset_kind"],
            "n": "" if item["scope"]["n"] is None else item["scope"]["n"],
            "selection": item["scope"]["selection"] or "",
            "selection_method": item["scope"]["selection_method"] or "",
            "model_ids": ";".join(item["model_ids"]),
            "metric_labels": ";".join(item["metric_labels"]),
            "evaluation_run_ids": ";".join(item["evaluation_run_ids"]),
            "reporting_gaps": ";".join(item["reporting_gaps"]),
            "evidence_ids": ";".join(evidence.get("id", "") for evidence in item["evidence"]),
            "last_verified": item["verification"]["last_verified"],
        })
    use_fields = ["id", "work_id", "work_version_id", "benchmark_id", "benchmark_version", "relation_type", "status", "scope", "subset_kind", "n", "selection", "selection_method", "model_ids", "metric_labels", "evaluation_run_ids", "reporting_gaps", "evidence_ids", "last_verified"]
    write_csv(exports / "benchmark-uses.csv", use_fields, use_rows)
    shutil.copy2(exports / "benchmark-uses.csv", public_data / "benchmark-uses.csv")

    result_rows = []
    for run in payload["evaluation_runs"]:
        for result in run["results"]:
            result_rows.append({
                "evaluation_run_id": run["id"], "work_id": run["work_id"], "work_version_id": run["work_version_id"], "benchmark_id": run["benchmark_id"],
                "benchmark_version": run["benchmark_version"] or "", "scope": run["scope"]["type"],
                "subset_id": run["scope"]["subset_id"] or "", "selection": run["scope"]["selection"] or "", "n": run["scope"]["n"] or "",
                "model_id": result["model_id"], "metric_id": result["metric_id"], "value": result["value"],
                "metric_kind": next(metric["kind"] for metric in run["metrics"] if metric["metric_id"] == result["metric_id"]),
                "baseline_model_id": next((metric["baseline_model_id"] or "") for metric in run["metrics"] if metric["metric_id"] == result["metric_id"]),
                "ci_low": result["ci_low"] if result["ci_low"] is not None else "", "ci_high": result["ci_high"] if result["ci_high"] is not None else "",
                "shots": _reported(run["protocol"]["shots"]), "turns": _reported(run["protocol"]["turns"]),
                "browser": _reported(run["protocol"]["tools"]["browser"]), "internet": _reported(run["protocol"]["tools"]["internet"]),
                "code_execution": _reported(run["protocol"]["tools"]["code_execution"]), "repeats": _reported(run["protocol"]["repeats"]),
                "grader": run["protocol"]["grader"]["type"] or "Not reported", "comparability_group": run["comparability_group"],
                "result_status": result.get("status", "legacy"),
                "confidence": result.get("confidence", ""),
                "result_evidence_ids": ";".join(result.get("evidence_ids", [])),
            })
    result_fields = ["evaluation_run_id", "work_id", "work_version_id", "benchmark_id", "benchmark_version", "scope", "subset_id", "selection", "n", "model_id", "metric_id", "metric_kind", "baseline_model_id", "value", "ci_low", "ci_high", "shots", "turns", "browser", "internet", "code_execution", "repeats", "grader", "comparability_group", "result_status", "confidence", "result_evidence_ids"]
    write_csv(exports / "evaluation-results.csv", result_fields, result_rows)
    shutil.copy2(exports / "evaluation-results.csv", public_data / "evaluation-results.csv")

    coverage_fields = [
        "task_type_id", "benchmark_id", "benchmark_name", "root_family_id", "benchmark_kind",
        "benchmark_version", "as_of", "classification_status", "coverage", "mapping_method",
        "confidence", "count", "count_unit", "count_basis", "count_ref", "reporting_status",
        "evidence_ids", "audit_status", "aggregate_eligible", "notes",
    ]
    coverage_rows = []
    for item in payload["scientific_task_coverage"]:
        row = dict(item)
        row["count"] = "" if item["count"] is None else item["count"]
        row["as_of"] = item["as_of"] or ""
        row["count_ref"] = item["count_ref"] or ""
        row["evidence_ids"] = ";".join(item["evidence_ids"])
        coverage_rows.append(row)
    write_csv(exports / "scientific-task-coverage.csv", coverage_fields, coverage_rows)
    shutil.copy2(exports / "scientific-task-coverage.csv", public_data / "scientific-task-coverage.csv")
    print(f"Built registry {payload['meta']['version']} with {len(payload['benchmarks'])} benchmarks, {len(payload['benchmark_uses'])} benchmark uses, and {len(payload['evaluation_runs'])} evaluation runs.")


if __name__ == "__main__":
    main()
