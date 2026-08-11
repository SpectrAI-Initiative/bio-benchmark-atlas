from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE_SNAPSHOTS = ("2026-08-05", "2026-08-06")
SNAPSHOT_DATE = AVAILABLE_SNAPSHOTS[-1]
SCHEMA_VERSION = "nucleic-acid-results-v1"
SOURCE_SNAPSHOT_ID = "nucleic_acid_benchmark_results_20260806"
SOURCE_ROOT = ROOT / "data" / "nucleic-acid-results" / SNAPSHOT_DATE
SOURCE_ARCHIVE = SOURCE_ROOT / "source-csvs.zip"
SOURCE_MANIFEST = SOURCE_ROOT / "source-manifest.json"
CROSSWALK_PATH = ROOT / "data" / "nucleic-acid-results" / "crosswalks.json"
SCHEMA_PATH = ROOT / "schema" / "nucleic-acid-results.schema.json"

BASE_SOURCE_TABLES = (
    "baseline_sota_summary.csv",
    "benchmark_result_coverage.csv",
    "benchmark_results.csv",
    "benchmark_tracks.csv",
    "benchmarks.csv",
    "configurations.csv",
    "evaluation_protocols.csv",
    "leaderboard_snapshots.csv",
    "metric_definitions.csv",
    "participants.csv",
    "protocol_leaders.csv",
    "result_sources.csv",
    "source_registry.csv",
    "task_benchmark.csv",
    "tasks.csv",
    "works.csv",
)

SNAPSHOT_EXTRA_TABLES = {
    "2026-08-05": (),
    "2026-08-06": ("track_result_coverage.csv",),
}

SOURCE_TABLES = BASE_SOURCE_TABLES + SNAPSHOT_EXTRA_TABLES[SNAPSHOT_DATE]

EXPECTED_COUNTS = {
    "benchmarks": 47,
    "tasks": 58,
    "tracks": 146,
    "protocols": 345,
    "metrics": 52,
    "results": 56_014,
    "summaries": 556,
    "leaders": 695,
    "participants": 415,
    "configurations": 3_719,
    "works": 29,
    "coverage": 47,
    "result_sources": 56_014,
    "track_coverage": 146,
}

EXPECTED_COUNTS_BY_SNAPSHOT = {
    "2026-08-05": {
        "benchmarks": 47, "tasks": 58, "tracks": 146, "protocols": 334,
        "metrics": 47, "results": 55_989, "summaries": 557, "leaders": 670,
        "participants": 398, "configurations": 3_699, "works": 25,
        "coverage": 47, "result_sources": 55_989,
    },
    "2026-08-06": EXPECTED_COUNTS,
}


def source_tables_for(snapshot_date: str) -> tuple[str, ...]:
    if snapshot_date not in AVAILABLE_SNAPSHOTS:
        raise NucleicAcidResultsError(f"unknown snapshot: {snapshot_date}")
    return BASE_SOURCE_TABLES + SNAPSHOT_EXTRA_TABLES[snapshot_date]


def source_paths_for(snapshot_date: str) -> tuple[Path, Path]:
    root = ROOT / "data" / "nucleic-acid-results" / snapshot_date
    return root / "source-csvs.zip", root / "source-manifest.json"

LOCAL_OR_SECRET_PATTERN = re.compile(
    r"(?:/Users/|/home/|/mnt/|file://|"
    r"(?:token|access_token|x-amz-signature|x-amz-credential|googleaccessid|signature|sig)=)",
    re.IGNORECASE,
)


class NucleicAcidResultsError(ValueError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def deterministic_gzip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as stream:
        stream.write(data)
    return buffer.getvalue()


def parse_csv(data: bytes, filename: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NucleicAcidResultsError(f"{filename}: not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise NucleicAcidResultsError(f"{filename}: missing CSV header")
    rows = list(reader)
    if any(None in row for row in rows):
        raise NucleicAcidResultsError(f"{filename}: ragged CSV row")
    return rows


def load_source_tables(
    archive_path: Path | None = None,
    snapshot_date: str = SNAPSHOT_DATE,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, bytes]]:
    if archive_path is None:
        archive_path = source_paths_for(snapshot_date)[0]
    source_tables = source_tables_for(snapshot_date)
    if not archive_path.exists():
        raise NucleicAcidResultsError(f"missing source archive: {archive_path}")
    tables: dict[str, list[dict[str, str]]] = {}
    raw_files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise NucleicAcidResultsError("source archive contains duplicate members")
            if set(names) != set(source_tables):
                missing = sorted(set(source_tables) - set(names))
                extra = sorted(set(names) - set(source_tables))
                raise NucleicAcidResultsError(
                    f"source archive member mismatch: missing={missing}, extra={extra}"
                )
            for filename in source_tables:
                data = archive.read(filename)
                raw_files[filename] = data
                tables[filename] = parse_csv(data, filename)
    except zipfile.BadZipFile as exc:
        raise NucleicAcidResultsError(f"invalid source archive: {archive_path}") from exc
    return tables, raw_files


def index_unique(rows: Iterable[dict[str, str]], key: str, table: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        value = row.get(key, "")
        if not value:
            raise NucleicAcidResultsError(f"{table}:{row_number}: empty {key}")
        if value in indexed:
            raise NucleicAcidResultsError(f"{table}: duplicate {key} {value!r}")
        indexed[value] = row
    return indexed


def split_ids(value: str) -> list[str]:
    return [part for part in value.split(";") if part and part != "NR"]


def assert_safe_public_value(value: Any, location: str = "root") -> None:
    if isinstance(value, str):
        if LOCAL_OR_SECRET_PATTERN.search(value):
            raise NucleicAcidResultsError(f"unsafe local path or signed credential at {location}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_safe_public_value(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert_safe_public_value(item, f"{location}.{key}")


def append_unique(mapping: defaultdict[str, list[str]], key: str, value: str) -> None:
    if key and key != "NR" and value not in mapping[key]:
        mapping[key].append(value)


def sorted_mapping(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: sorted(values) for key, values in sorted(mapping.items())}
