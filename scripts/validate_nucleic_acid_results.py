from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from nucleic_acid_results import (
    CROSSWALK_PATH,
    EXPECTED_COUNTS,
    SCHEMA_VERSION,
    SNAPSHOT_DATE,
    SOURCE_ARCHIVE,
    SOURCE_MANIFEST,
    SOURCE_TABLES,
    NucleicAcidResultsError,
    assert_safe_public_value,
    index_unique,
    load_source_tables,
    sha256_bytes,
    split_ids,
)
from registry_io import load_entities, load_taxonomies


ALLOWED_CLAIMS = {
    "official_baseline",
    "original_table_best",
    "official_board_or_challenge_leader",
    "strict_cross_work_sota",
    "single_reported_result",
    "NR",
}
ALLOWED_BENCHMARK_CROSSWALK = {"same_entity", "family_member", "unmapped"}
ALLOWED_TASK_CROSSWALK = {"exact", "narrower_than_registry", "composite", "taxonomy_gap"}


def _require_fk(value: str, target: dict[str, Any], location: str, *, allow_nr: bool = False) -> None:
    if allow_nr and value in {"", "NR"}:
        return
    if value not in target:
        raise NucleicAcidResultsError(f"{location}: missing foreign key {value!r}")


def _validate_source_manifest(raw_files: dict[str, bytes]) -> dict[str, Any]:
    try:
        manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise NucleicAcidResultsError(f"invalid source manifest: {SOURCE_MANIFEST}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise NucleicAcidResultsError("source manifest schema_version mismatch")
    if manifest.get("snapshot_date") != SNAPSHOT_DATE or manifest.get("literature_cutoff") != SNAPSHOT_DATE:
        raise NucleicAcidResultsError("source manifest snapshot/cutoff mismatch")
    archive = manifest.get("archive", {})
    archive_bytes = SOURCE_ARCHIVE.read_bytes()
    if archive.get("path") != SOURCE_ARCHIVE.name:
        raise NucleicAcidResultsError("source manifest archive path mismatch")
    if archive.get("sha256") != sha256_bytes(archive_bytes) or archive.get("bytes") != len(archive_bytes):
        raise NucleicAcidResultsError("source archive hash/size mismatch")
    files = manifest.get("files", {})
    if set(files) != set(SOURCE_TABLES):
        raise NucleicAcidResultsError("source manifest file inventory mismatch")
    for filename, data in raw_files.items():
        entry = files[filename]
        if entry.get("sha256") != sha256_bytes(data) or entry.get("bytes") != len(data):
            raise NucleicAcidResultsError(f"{filename}: source manifest hash/size mismatch")
    assert_safe_public_value(manifest, "source_manifest")
    return manifest


def _validate_crosswalks(
    benchmarks: dict[str, dict[str, str]], tasks: dict[str, dict[str, str]]
) -> dict[str, Any]:
    try:
        crosswalks = json.loads(CROSSWALK_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise NucleicAcidResultsError(f"invalid crosswalk file: {CROSSWALK_PATH}") from exc
    if crosswalks.get("schema_version") != SCHEMA_VERSION:
        raise NucleicAcidResultsError("crosswalk schema_version mismatch")
    benchmark_rows = crosswalks.get("benchmark_crosswalk", [])
    task_rows = crosswalks.get("task_crosswalk", [])
    registry_benchmark_ids = {row["id"] for row in load_entities()["benchmark"]}
    registry_task_ids = {row["id"] for row in load_taxonomies()["scientific_tasks"]}
    if {row.get("nucleic_acid_benchmark_id") for row in benchmark_rows} != set(benchmarks):
        raise NucleicAcidResultsError("benchmark crosswalk must contain exactly B01-B47")
    if {row.get("nucleic_acid_task_id") for row in task_rows} != set(tasks):
        raise NucleicAcidResultsError("task crosswalk must contain exactly T01-T58")
    for row in benchmark_rows:
        relation = row.get("relationship")
        registry_id = row.get("registry_benchmark_id")
        if relation not in ALLOWED_BENCHMARK_CROSSWALK:
            raise NucleicAcidResultsError(f"invalid benchmark crosswalk relationship: {relation!r}")
        if relation == "unmapped" and registry_id is not None:
            raise NucleicAcidResultsError("unmapped benchmark crosswalk must have null registry ID")
        if relation != "unmapped" and not registry_id:
            raise NucleicAcidResultsError("mapped benchmark crosswalk requires a registry ID")
        if registry_id and registry_id not in registry_benchmark_ids:
            raise NucleicAcidResultsError(
                f"benchmark crosswalk references missing Registry benchmark {registry_id!r}"
            )
    for row in task_rows:
        relation = row.get("relationship")
        registry_id = row.get("registry_task_id")
        if relation not in ALLOWED_TASK_CROSSWALK:
            raise NucleicAcidResultsError(f"invalid task crosswalk relationship: {relation!r}")
        if relation == "taxonomy_gap" and registry_id is not None:
            raise NucleicAcidResultsError("taxonomy_gap task crosswalk must have null registry ID")
        if relation != "taxonomy_gap" and not registry_id:
            raise NucleicAcidResultsError("mapped task crosswalk requires a registry ID")
        if registry_id and registry_id not in registry_task_ids:
            raise NucleicAcidResultsError(
                f"task crosswalk references missing Registry task {registry_id!r}"
            )
    assert_safe_public_value(crosswalks, "crosswalks")
    return crosswalks


def validate() -> tuple[dict[str, list[dict[str, str]]], dict[str, Any], dict[str, Any]]:
    tables, raw_files = load_source_tables()
    source_manifest = _validate_source_manifest(raw_files)
    for filename, rows in tables.items():
        assert_safe_public_value(rows, filename)
        manifest_entry = source_manifest["files"][filename]
        if manifest_entry.get("row_count") != len(rows):
            raise NucleicAcidResultsError(f"{filename}: row count differs from source manifest")
        observed_columns = list(rows[0]) if rows else list(manifest_entry.get("columns", []))
        if manifest_entry.get("columns") != observed_columns:
            raise NucleicAcidResultsError(f"{filename}: columns differ from source manifest")

    benchmark_rows = tables["benchmarks.csv"]
    task_rows = tables["tasks.csv"]
    track_rows = tables["benchmark_tracks.csv"]
    protocol_rows = tables["evaluation_protocols.csv"]
    metric_rows = tables["metric_definitions.csv"]
    result_rows = tables["benchmark_results.csv"]
    summary_rows = tables["baseline_sota_summary.csv"]
    leader_rows = tables["protocol_leaders.csv"]
    participant_rows = tables["participants.csv"]
    configuration_rows = tables["configurations.csv"]
    work_rows = tables["works.csv"]
    coverage_rows = tables["benchmark_result_coverage.csv"]
    result_source_rows = tables["result_sources.csv"]
    source_registry_rows = tables["source_registry.csv"]

    observed_counts = {
        "benchmarks": len(benchmark_rows),
        "tasks": len(task_rows),
        "tracks": len(track_rows),
        "protocols": len(protocol_rows),
        "metrics": len(metric_rows),
        "results": len(result_rows),
        "summaries": len(summary_rows),
        "leaders": len(leader_rows),
        "participants": len(participant_rows),
        "configurations": len(configuration_rows),
        "works": len(work_rows),
        "coverage": len(coverage_rows),
        "result_sources": len(result_source_rows),
    }
    if observed_counts != EXPECTED_COUNTS:
        raise NucleicAcidResultsError(
            f"snapshot count mismatch: observed={observed_counts}, expected={EXPECTED_COUNTS}"
        )

    benchmarks = index_unique(benchmark_rows, "benchmark_id", "benchmarks.csv")
    tasks = index_unique(task_rows, "task_id", "tasks.csv")
    tracks = index_unique(track_rows, "track_id", "benchmark_tracks.csv")
    protocols = index_unique(protocol_rows, "protocol_id", "evaluation_protocols.csv")
    metrics = index_unique(metric_rows, "metric_id", "metric_definitions.csv")
    results = index_unique(result_rows, "result_id", "benchmark_results.csv")
    participants = index_unique(participant_rows, "participant_id", "participants.csv")
    configurations = index_unique(configuration_rows, "configuration_id", "configurations.csv")
    works = index_unique(work_rows, "work_id", "works.csv")
    sources = index_unique(result_source_rows, "result_source_id", "result_sources.csv")
    source_registry = index_unique(source_registry_rows, "source_registry_id", "source_registry.csv")
    index_unique(summary_rows, "summary_id", "baseline_sota_summary.csv")
    index_unique(leader_rows, "leader_id", "protocol_leaders.csv")

    if len({row["protocol_fingerprint"] for row in protocol_rows}) != len(protocol_rows):
        raise NucleicAcidResultsError("protocol fingerprints are not unique")
    coverage_ids = [row["benchmark_id"] for row in coverage_rows]
    if len(coverage_ids) != len(set(coverage_ids)) or set(coverage_ids) != set(benchmarks):
        raise NucleicAcidResultsError("coverage must contain each benchmark exactly once")

    for row in track_rows:
        _require_fk(row["benchmark_id"], benchmarks, f"track {row['track_id']}.benchmark_id")
    for row in tables["task_benchmark.csv"]:
        _require_fk(row["task_id"], tasks, "task_benchmark.task_id")
        _require_fk(row["track_id"], tracks, "task_benchmark.track_id")
        _require_fk(row["benchmark_id"], benchmarks, "task_benchmark.benchmark_id")
        if tracks[row["track_id"]]["benchmark_id"] != row["benchmark_id"]:
            raise NucleicAcidResultsError("task_benchmark track/benchmark mismatch")
    for row in protocol_rows:
        _require_fk(row["benchmark_id"], benchmarks, f"protocol {row['protocol_id']}.benchmark_id")
        _require_fk(row["task_id"], tasks, f"protocol {row['protocol_id']}.task_id")
        _require_fk(row["track_id"], tracks, f"protocol {row['protocol_id']}.track_id", allow_nr=True)
        _require_fk(row["protocol_evidence_id"], source_registry, f"protocol {row['protocol_id']}.evidence")
        if not row["protocol_fingerprint"] or not row["fingerprint_schema_version"]:
            raise NucleicAcidResultsError(f"protocol {row['protocol_id']} lacks a fingerprint")
    for row in configuration_rows:
        _require_fk(row["participant_id"], participants, f"configuration {row['configuration_id']}")
    for row in result_source_rows:
        _require_fk(row["result_id"], results, f"result_source {row['result_source_id']}.result")
        _require_fk(row["source_registry_id"], source_registry, f"result_source {row['result_source_id']}.source")

    for row in result_rows:
        rid = row["result_id"]
        _require_fk(row["protocol_id"], protocols, f"result {rid}.protocol")
        _require_fk(row["participant_id"], participants, f"result {rid}.participant")
        _require_fk(row["configuration_id"], configurations, f"result {rid}.configuration")
        _require_fk(row["work_id"], works, f"result {rid}.work")
        _require_fk(row["metric_id"], metrics, f"result {rid}.metric")
        _require_fk(row["canonical_evidence_source_id"], sources, f"result {rid}.canonical_evidence")
        evidence = sources[row["canonical_evidence_source_id"]]
        if evidence["result_id"] != rid or evidence["canonical_evidence"] != "true":
            raise NucleicAcidResultsError(f"result {rid}: invalid canonical evidence ownership")
        if configurations[row["configuration_id"]]["participant_id"] != row["participant_id"]:
            raise NucleicAcidResultsError(f"result {rid}: configuration/participant mismatch")
        protocol = protocols[row["protocol_id"]]
        if not row["protocol_metric_fingerprint"].startswith("PMF-v1-"):
            raise NucleicAcidResultsError(f"result {rid}: invalid protocol-metric fingerprint")
        if row["sota_eligible"] == "true" and not (
            protocol["comparability_status"] == "strict_comparable"
            and row["comparison_scope"] == "cross_work"
            and row["rank_eligible"] == "true"
        ):
            raise NucleicAcidResultsError(f"result {rid}: invalid strict SOTA eligibility")

    ranked_groups: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in result_rows:
        if row["rank_eligible"] == "true" and row["observed_rank"]:
            ranked_groups[(row["protocol_metric_fingerprint"], row["snapshot_date"])].append(row)
    for group_key, group in ranked_groups.items():
        metric_ids = {row["metric_id"] for row in group}
        if len(metric_ids) != 1:
            raise NucleicAcidResultsError(f"rank group {group_key}: multiple metrics")
        direction = metrics[next(iter(metric_ids))]["direction"]
        if direction not in {"higher", "lower"}:
            raise NucleicAcidResultsError(f"rank group {group_key}: unknown direction {direction!r}")
        try:
            scored = [(row, Decimal(row["canonical_value"])) for row in group]
        except InvalidOperation as exc:
            raise NucleicAcidResultsError(f"rank group {group_key}: nonnumeric score") from exc
        score_counts = Counter(value for _, value in scored)
        ordered_scores = sorted(score_counts, reverse=direction == "higher")
        expected_by_score: dict[Decimal, int] = {}
        preceding = 0
        for value in ordered_scores:
            expected_by_score[value] = preceding + 1
            preceding += score_counts[value]
        for row, value in scored:
            expected_rank = expected_by_score[value]
            if int(row["observed_rank"]) != expected_rank:
                raise NucleicAcidResultsError(
                    f"result {row['result_id']}: observed rank {row['observed_rank']} != {expected_rank}"
                )
        tied: defaultdict[Decimal, list[dict[str, str]]] = defaultdict(list)
        for row, value in scored:
            tied[value].append(row)
        for value, tie_rows in tied.items():
            if len(tie_rows) > 1:
                tie_groups = {row["tie_group"] for row in tie_rows}
                if len(tie_groups) != 1 or "" in tie_groups:
                    raise NucleicAcidResultsError(
                        f"rank group {group_key}: score tie {value} has inconsistent tie groups"
                    )

    for row in leader_rows:
        lid = row["leader_id"]
        if row["claim_type"] not in ALLOWED_CLAIMS:
            raise NucleicAcidResultsError(f"leader {lid}: invalid claim type")
        _require_fk(row["protocol_id"], protocols, f"leader {lid}.protocol")
        _require_fk(row["leader_result_id"], results, f"leader {lid}.result")
        _require_fk(row["participant_id"], participants, f"leader {lid}.participant")
        _require_fk(row["configuration_id"], configurations, f"leader {lid}.configuration")
        _require_fk(row["work_id"], works, f"leader {lid}.work")
        _require_fk(row["metric_id"], metrics, f"leader {lid}.metric")
        _require_fk(row["benchmark_id"], benchmarks, f"leader {lid}.benchmark")
        _require_fk(row["task_id"], tasks, f"leader {lid}.task")
        _require_fk(row["track_id"], tracks, f"leader {lid}.track", allow_nr=True)
        _require_fk(row["canonical_evidence_source_id"], sources, f"leader {lid}.evidence")
        result = results[row["leader_result_id"]]
        if result["protocol_id"] != row["protocol_id"] or result["metric_id"] != row["metric_id"]:
            raise NucleicAcidResultsError(f"leader {lid}: result/protocol/metric mismatch")
        if result["rank_eligible"] != "true":
            raise NucleicAcidResultsError(f"leader {lid}: result is not rank eligible")
        if row["canonical_value"] != result["canonical_value"]:
            raise NucleicAcidResultsError(f"leader {lid}: score differs from leader result")
        if row["protocol_metric_fingerprint"] != result["protocol_metric_fingerprint"]:
            raise NucleicAcidResultsError(f"leader {lid}: fingerprint differs from leader result")
        if row["official_rank"] != result["official_rank"]:
            raise NucleicAcidResultsError(f"leader {lid}: official rank was not preserved")
        if row["computed_rank"] != result["observed_rank"]:
            raise NucleicAcidResultsError(f"leader {lid}: computed rank differs from observed rank")
        if row["claim_type"] == "strict_cross_work_sota" and result["sota_eligible"] != "true":
            raise NucleicAcidResultsError(f"leader {lid}: strict SOTA is not eligible")
        if row["claim_type"] == "original_table_best" and row["sota_eligible"] == "true":
            raise NucleicAcidResultsError(f"leader {lid}: table best mislabeled as SOTA eligible")

    for row in summary_rows:
        sid = row["summary_id"]
        _require_fk(row["benchmark_id"], benchmarks, f"summary {sid}.benchmark")
        _require_fk(row["task_id"], tasks, f"summary {sid}.task")
        is_gap_summary = row["protocol_id"] == "NR" and row["metric_id"] == "NR"
        if (row["protocol_id"] == "NR") != (row["metric_id"] == "NR"):
            raise NucleicAcidResultsError(
                f"summary {sid}: protocol_id and metric_id must both be NR or both be resolved"
            )
        _require_fk(row["protocol_id"], protocols, f"summary {sid}.protocol", allow_nr=True)
        _require_fk(row["metric_id"], metrics, f"summary {sid}.metric", allow_nr=True)
        _require_fk(row["track_id"], tracks, f"summary {sid}.track", allow_nr=True)
        claims = set(split_ids(row["claim_types_present"]))
        if not claims <= ALLOWED_CLAIMS:
            raise NucleicAcidResultsError(f"summary {sid}: invalid claim type list")
        strict_ids = split_ids(row["strict_cross_work_sota_result_ids"])
        for rid in strict_ids:
            _require_fk(rid, results, f"summary {sid}.strict_sota")
            if results[rid]["sota_eligible"] != "true":
                raise NucleicAcidResultsError(f"summary {sid}: ineligible strict SOTA")
        if (
            not strict_ids
            and not is_gap_summary
            and row["sota_claim_status"] != "no_strict_cross_work_sota"
        ):
            raise NucleicAcidResultsError(f"summary {sid}: missing explicit no-SOTA status")
        if is_gap_summary and row["result_count"] != "0":
            raise NucleicAcidResultsError(f"summary {sid}: gap row must have result_count=0")

    strict_leaders = [row for row in leader_rows if row["claim_type"] == "strict_cross_work_sota"]
    strict_summary_ids = [
        rid
        for row in summary_rows
        for rid in split_ids(row["strict_cross_work_sota_result_ids"])
    ]
    if strict_leaders or strict_summary_ids:
        raise NucleicAcidResultsError(
            "2026-08-05 snapshot must not claim a strict cross-work SOTA"
        )

    for row in source_registry_rows:
        for benchmark_id in split_ids(row["benchmark_ids"]):
            _require_fk(benchmark_id, benchmarks, f"source_registry {row['source_registry_id']}")
    for row in work_rows:
        for benchmark_id in split_ids(row["benchmark_ids"]):
            _require_fk(benchmark_id, benchmarks, f"work {row['work_id']}")

    crosswalks = _validate_crosswalks(benchmarks, tasks)
    return tables, source_manifest, crosswalks


def main() -> None:
    tables, source_manifest, _ = validate()
    output = {
        "status": "valid",
        "snapshot_date": SNAPSHOT_DATE,
        "source_archive_sha256": source_manifest["archive"]["sha256"],
        "counts": {
            "benchmarks": len(tables["benchmarks.csv"]),
            "tasks": len(tables["tasks.csv"]),
            "protocols": len(tables["evaluation_protocols.csv"]),
            "results": len(tables["benchmark_results.csv"]),
            "summaries": len(tables["baseline_sota_summary.csv"]),
            "leaders": len(tables["protocol_leaders.csv"]),
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
