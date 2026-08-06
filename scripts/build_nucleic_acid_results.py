from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from nucleic_acid_results import (
    AVAILABLE_SNAPSHOTS,
    EXPECTED_COUNTS_BY_SNAPSHOT,
    ROOT,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    SNAPSHOT_DATE,
    append_unique,
    assert_safe_public_value,
    canonical_json_bytes,
    deterministic_gzip,
    sha256_bytes,
    sorted_mapping,
)
from validate_nucleic_acid_results import validate


CATALOG_GZIP_LIMIT = 250_000
PROTOCOL_GZIP_LIMIT = 600_000


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    temporary.replace(path)


def _validate_json(schema: dict[str, Any], payload: dict[str, Any], label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "root"
        raise ValueError(f"{label}: schema validation failed at {location}: {first.message}")


def _write_gzip_asset(
    version_root: Path,
    stem: str,
    payload: dict[str, Any],
    row_count: int,
    schema: dict[str, Any],
    *,
    subdirectory: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    assert_safe_public_value(payload, stem)
    _validate_json(schema, payload, stem)
    raw = canonical_json_bytes(payload)
    compressed = deterministic_gzip(raw)
    digest = sha256_bytes(compressed)
    filename = f"{digest}.json.gz" if subdirectory else f"{stem}.{digest}.json.gz"
    relative_path = f"{subdirectory}/{filename}" if subdirectory else filename
    destination = version_root / relative_path
    _atomic_write(destination, compressed)
    descriptor = {
        "path": relative_path,
        "sha256": digest,
        "rowCount": row_count,
        "compressedBytes": len(compressed),
        "uncompressedBytes": len(raw),
    }
    return descriptor, raw


def _build_usage_index(
    protocols: list[dict[str, str]], results: list[dict[str, str]], snapshot_date: str
) -> dict[str, Any]:
    benchmark_protocols: defaultdict[str, list[str]] = defaultdict(list)
    task_protocols: defaultdict[str, list[str]] = defaultdict(list)
    track_protocols: defaultdict[str, list[str]] = defaultdict(list)
    participant_protocols: defaultdict[str, list[str]] = defaultdict(list)
    configuration_protocols: defaultdict[str, list[str]] = defaultdict(list)
    work_protocols: defaultdict[str, list[str]] = defaultdict(list)
    protocol_result_counts: Counter[str] = Counter()
    benchmark_result_counts: Counter[str] = Counter()
    participant_result_counts: Counter[str] = Counter()
    protocol_by_id = {row["protocol_id"]: row for row in protocols}

    for protocol in protocols:
        protocol_id = protocol["protocol_id"]
        append_unique(benchmark_protocols, protocol["benchmark_id"], protocol_id)
        append_unique(task_protocols, protocol["task_id"], protocol_id)
        append_unique(track_protocols, protocol["track_id"], protocol_id)
    for result in results:
        protocol_id = result["protocol_id"]
        protocol = protocol_by_id[protocol_id]
        append_unique(participant_protocols, result["participant_id"], protocol_id)
        append_unique(configuration_protocols, result["configuration_id"], protocol_id)
        append_unique(work_protocols, result["work_id"], protocol_id)
        protocol_result_counts[protocol_id] += 1
        benchmark_result_counts[protocol["benchmark_id"]] += 1
        participant_result_counts[result["participant_id"]] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": snapshot_date,
        "benchmark_protocols": sorted_mapping(benchmark_protocols),
        "task_protocols": sorted_mapping(task_protocols),
        "track_protocols": sorted_mapping(track_protocols),
        "participant_protocols": sorted_mapping(participant_protocols),
        "configuration_protocols": sorted_mapping(configuration_protocols),
        "work_protocols": sorted_mapping(work_protocols),
        "protocol_result_counts": dict(sorted(protocol_result_counts.items())),
        "benchmark_result_counts": dict(sorted(benchmark_result_counts.items())),
        "participant_result_counts": dict(sorted(participant_result_counts.items())),
    }


def build_snapshot(public_root: Path, generated_root: Path, snapshot_date: str) -> dict[str, Any]:
    tables, _, crosswalks = validate(snapshot_date)
    expected_counts = EXPECTED_COUNTS_BY_SNAPSHOT[snapshot_date]
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    version_root = public_root / "data" / "nucleic-acids" / snapshot_date
    generated_version_root = generated_root / "nucleic-acid-results" / snapshot_date
    version_root.mkdir(parents=True, exist_ok=True)
    generated_version_root.mkdir(parents=True, exist_ok=True)

    catalog = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": snapshot_date,
        "literature_cutoff": snapshot_date,
        "benchmarks": tables["benchmarks.csv"],
        "tasks": tables["tasks.csv"],
        "tracks": tables["benchmark_tracks.csv"],
        "task_benchmark": tables["task_benchmark.csv"],
        "coverage": tables["benchmark_result_coverage.csv"],
        "protocols": tables["evaluation_protocols.csv"],
        "metrics": tables["metric_definitions.csv"],
        "summaries": tables["baseline_sota_summary.csv"],
        "leaders": tables["protocol_leaders.csv"],
        "benchmark_crosswalk": crosswalks["benchmark_crosswalk"],
        "task_crosswalk": crosswalks["task_crosswalk"],
    }
    if "track_result_coverage.csv" in tables:
        catalog["track_coverage"] = tables["track_result_coverage.csv"]
    entities = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": snapshot_date,
        "participants": tables["participants.csv"],
        "configurations": tables["configurations.csv"],
        "works": tables["works.csv"],
    }
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": snapshot_date,
        "sources": tables["source_registry.csv"],
        "result_sources": tables["result_sources.csv"],
        "leaderboard_snapshots": tables["leaderboard_snapshots.csv"],
    }
    usage_index = _build_usage_index(
        tables["evaluation_protocols.csv"], tables["benchmark_results.csv"], snapshot_date
    )

    catalog_row_keys = [
            "benchmarks",
            "tasks",
            "tracks",
            "task_benchmark",
            "coverage",
            "protocols",
            "metrics",
            "summaries",
            "leaders",
            "benchmark_crosswalk",
            "task_crosswalk",
    ]
    if "track_coverage" in catalog:
        catalog_row_keys.append("track_coverage")
    catalog_rows = sum(len(catalog[key]) for key in catalog_row_keys)
    entity_rows = len(entities["participants"]) + len(entities["configurations"]) + len(entities["works"])
    evidence_rows = len(evidence["sources"]) + len(evidence["result_sources"]) + len(evidence["leaderboard_snapshots"])
    usage_rows = sum(
        len(value) for key, value in usage_index.items() if key not in {"schema_version", "snapshot_date"}
    )

    catalog_descriptor, catalog_raw = _write_gzip_asset(
        version_root, "catalog", catalog, catalog_rows, schema
    )
    if catalog_descriptor["compressedBytes"] >= CATALOG_GZIP_LIMIT:
        raise ValueError(
            f"catalog gzip is {catalog_descriptor['compressedBytes']} bytes; limit is {CATALOG_GZIP_LIMIT}"
        )
    entities_descriptor, _ = _write_gzip_asset(
        version_root, "entities", entities, entity_rows, schema
    )
    evidence_descriptor, _ = _write_gzip_asset(
        version_root, "evidence", evidence, evidence_rows, schema
    )
    usage_descriptor, _ = _write_gzip_asset(
        version_root, "usage-index", usage_index, usage_rows, schema
    )

    protocol_by_id = {
        row["protocol_id"]: row for row in tables["evaluation_protocols.csv"]
    }
    sources_by_result: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for source in tables["result_sources.csv"]:
        sources_by_result[source["result_id"]].append(source)
    results_by_protocol: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    assigned_result_ids: set[str] = set()
    for result in tables["benchmark_results.csv"]:
        result_id = result["result_id"]
        if result_id in assigned_result_ids:
            raise ValueError(f"result assigned more than once: {result_id}")
        assigned_result_ids.add(result_id)
        enriched: dict[str, Any] = dict(result)
        enriched["sources"] = sources_by_result[result_id]
        results_by_protocol[result["protocol_id"]].append(enriched)
    if len(assigned_result_ids) != expected_counts["results"]:
        raise ValueError("not every result was assigned to one protocol chunk")

    protocol_chunks: dict[str, dict[str, Any]] = {}
    for protocol_id in sorted(protocol_by_id):
        protocol = protocol_by_id[protocol_id]
        chunk = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_date": snapshot_date,
            "protocol_id": protocol_id,
            "protocol_fingerprint": protocol["protocol_fingerprint"],
            "benchmark_id": protocol["benchmark_id"],
            "task_id": protocol["task_id"],
            "track_id": protocol["track_id"],
            "results": results_by_protocol[protocol_id],
        }
        descriptor, _ = _write_gzip_asset(
            version_root,
            protocol_id,
            chunk,
            len(chunk["results"]),
            schema,
            subdirectory="protocols",
        )
        if descriptor["compressedBytes"] >= PROTOCOL_GZIP_LIMIT:
            raise ValueError(
                f"{protocol_id} gzip is {descriptor['compressedBytes']} bytes; limit is {PROTOCOL_GZIP_LIMIT}"
            )
        protocol_chunks[protocol_id] = descriptor

    counts = dict(expected_counts)
    counts.update(
        {
            "numeric_benchmarks": sum(
                row["result_count"] != "0" for row in tables["benchmark_result_coverage.csv"]
            ),
            "benchmarks_without_numeric_results": sum(
                row["result_count"] == "0" for row in tables["benchmark_result_coverage.csv"]
            ),
            "strict_cross_work_sota": sum(
                row["claim_type"] == "strict_cross_work_sota"
                for row in tables["protocol_leaders.csv"]
            ),
        }
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": snapshot_date,
        "literature_cutoff": snapshot_date,
        "counts": counts,
        "assets": {
            "catalog": catalog_descriptor,
            "entities": entities_descriptor,
            "evidence": evidence_descriptor,
            "usage_index": usage_descriptor,
        },
        "protocolChunks": protocol_chunks,
    }
    assert_safe_public_value(manifest, "manifest")
    _validate_json(schema, manifest, "manifest")
    manifest_bytes = canonical_json_bytes(manifest, pretty=True)
    _atomic_write(version_root / "manifest.json", manifest_bytes)

    public_schema = public_root / "schema" / SCHEMA_PATH.name
    _atomic_write(public_schema, SCHEMA_PATH.read_bytes())

    _atomic_write(
        generated_version_root / "catalog.json", canonical_json_bytes(catalog, pretty=True)
    )
    _atomic_write(
        generated_version_root / "entities.json", canonical_json_bytes(entities, pretty=True)
    )
    _atomic_write(
        generated_version_root / "usage-index.json",
        canonical_json_bytes(usage_index, pretty=True)
    )
    _atomic_write(generated_version_root / "manifest.json", manifest_bytes)
    return {
        "snapshot_date": snapshot_date,
        "manifest": str(version_root / "manifest.json"),
        "catalog_gzip_bytes": catalog_descriptor["compressedBytes"],
        "largest_protocol_gzip_bytes": max(
            descriptor["compressedBytes"] for descriptor in protocol_chunks.values()
        ),
        "protocol_chunks": len(protocol_chunks),
        "result_rows": sum(descriptor["rowCount"] for descriptor in protocol_chunks.values()),
        "catalog_sha256": catalog_descriptor["sha256"],
        "catalog_uncompressed_bytes": len(catalog_raw),
    }


def build(public_root: Path, generated_root: Path) -> dict[str, Any]:
    receipts = [build_snapshot(public_root, generated_root, date) for date in AVAILABLE_SNAPSHOTS]
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    latest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": SNAPSHOT_DATE,
        "manifest_path": f"{SNAPSHOT_DATE}/manifest.json",
        "available_snapshots": list(AVAILABLE_SNAPSHOTS),
    }
    assert_safe_public_value(latest, "latest")
    _validate_json(schema, latest, "latest")
    latest_bytes = canonical_json_bytes(latest, pretty=True)
    _atomic_write(public_root / "data" / "nucleic-acids" / "latest.json", latest_bytes)
    _atomic_write(generated_root / "nucleic-acid-results" / "latest.json", latest_bytes)
    return {"latest_snapshot": SNAPSHOT_DATE, "available_snapshots": list(AVAILABLE_SNAPSHOTS), "snapshots": receipts}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build static nucleic-acid benchmark result artifacts.")
    parser.add_argument("--public-root", type=Path, default=ROOT / "site" / "public")
    parser.add_argument("--generated-root", type=Path, default=ROOT / "site" / "src" / "generated")
    args = parser.parse_args()
    receipt = build(args.public_root.resolve(), args.generated_root.resolve())
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
