from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from package_nucleic_acid_release import (  # noqa: E402
    ReleasePackagingError,
    assert_safe_artifact,
    assert_safe_artifact_bytes,
    deterministic_zip,
    package_release,
    sha256_bytes,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _fixture_snapshot(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "audited-snapshot"
    source.mkdir()
    local_root = source.resolve().as_posix()
    _write_json(
        source / "validation_summary.json",
        {"built_at": "2026-08-05", "source_snapshot": "/mnt/research/upstream-snapshot"},
    )
    _write_json(
        source / "workbook_validation.json",
        {
            "workbook": {
                "file": "nucleic_acid_baseline_sota_20260805.xlsx",
                "bytes": 123,
                "sha256": "0" * 64,
                "zip_integrity": "passed",
                "independent_reimport": "passed",
            }
        },
    )
    _write_json(source / "independent_qa_receipt.json", {"all_passed": True})
    _write_json(
        source / "programmatic_import_summary.json",
        {"source_file": f"{local_root}/source_snapshots/table.csv"},
    )
    (source / "data_dictionary.md").write_text(
        f"# Dictionary\n\nSource: `{local_root}/source_snapshots/table.csv`\n",
        encoding="utf-8",
    )
    workbook = deterministic_zip(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "xl/workbook.xml": (
                f'<?xml version="1.0"?><workbook><path>{local_root}/source.csv</path></workbook>'
            ).encode(),
        }
    )
    (source / "nucleic_acid_baseline_sota_20260805.xlsx").write_bytes(workbook)

    csv_member = b"benchmark_id,result_count\r\nB01,1\r\n"
    (source / "benchmarks.csv").write_bytes(csv_member)
    (source / "review_log.csv").write_bytes(b"review_id,status\r\nREV-1,accepted\r\n")
    csv_archive = tmp_path / "source-csvs.zip"
    csv_archive.write_bytes(deterministic_zip({"benchmarks.csv": csv_member}))
    manifest = tmp_path / "source-manifest.json"
    _write_json(
        manifest,
        {
            "archive": {
                "bytes": csv_archive.stat().st_size,
                "sha256": sha256_bytes(csv_archive.read_bytes()),
            },
            "files": {
                "benchmarks.csv": {
                    "bytes": len(csv_member),
                    "sha256": sha256_bytes(csv_member),
                }
            },
        },
    )
    return source, csv_archive, manifest


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "payload",
    [
        b'"path":"/Users/example/private.csv"',
        b'"path":"/mnt/shared/private.csv"',
        b'"url":"https://example.org/data?token=secret"',
        b'"url":"https://example.org/data?X-Amz-Signature=secret"',
        b'"url":"https://example.org/data?sig=secret"',
    ],
)
def test_public_artifact_guard_rejects_local_paths_and_signed_urls(payload: bytes) -> None:
    with pytest.raises(ReleasePackagingError):
        assert_safe_artifact_bytes(payload, name="artifact.json")


def test_public_artifact_guard_scans_nested_zip_members() -> None:
    archive = deterministic_zip(
        {"safe.csv": b"id,value\n1,/Users/example/not-public\n"}
    )
    with pytest.raises(ReleasePackagingError):
        assert_safe_artifact_bytes(archive, name="release.zip")


def test_release_package_is_deterministic_sanitized_and_complete(tmp_path: Path) -> None:
    source, csv_archive, manifest = _fixture_snapshot(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_paths = package_release(
        source_root=source,
        output_dir=first,
        csv_archive=csv_archive,
        source_manifest=manifest,
    )
    package_release(
        source_root=source,
        output_dir=second,
        csv_archive=csv_archive,
        source_manifest=manifest,
    )

    assert _tree_hashes(first) == _tree_hashes(second)
    assert {path.name for path in first_paths} == {
        "SHA256SUMS",
        "nucleic-acid-results-2026-08-05-csv.zip",
        "nucleic-acid-results-2026-08-05-data-dictionary.md",
        "nucleic-acid-results-2026-08-05-independent-qa-receipt.json",
        "nucleic-acid-results-2026-08-05-programmatic-import-summary.json",
        "nucleic-acid-results-2026-08-05-validation-summary.json",
        "nucleic-acid-results-2026-08-05-workbook-validation.json",
        "nucleic_acid_baseline_sota_20260805.xlsx",
    }
    for path in first_paths:
        assert_safe_artifact(path)

    dictionary = (first / "nucleic-acid-results-2026-08-05-data-dictionary.md").read_text()
    assert "source_snapshots/table.csv" in dictionary
    assert "/Users/" not in dictionary
    imported = json.loads(
        (first / "nucleic-acid-results-2026-08-05-programmatic-import-summary.json").read_text()
    )
    assert imported["source_file"] == "source_snapshots/table.csv"

    workbook_path = first / "nucleic_acid_baseline_sota_20260805.xlsx"
    with ZipFile(workbook_path) as workbook:
        assert workbook.testzip() is None
        assert b"/Users/" not in workbook.read("xl/workbook.xml")
    workbook_qc = json.loads(
        (first / "nucleic-acid-results-2026-08-05-workbook-validation.json").read_text()
    )["workbook"]
    assert workbook_qc["sha256"] == sha256_bytes(workbook_path.read_bytes())
    assert workbook_qc["bytes"] == workbook_path.stat().st_size
    assert workbook_qc["pre_release_sha256"] == "0" * 64

    with ZipFile(first / "nucleic-acid-results-2026-08-05-csv.zip") as csv_release:
        assert csv_release.namelist() == ["benchmarks.csv", "review_log.csv"]
        assert csv_release.read("benchmarks.csv") == b"benchmark_id,result_count\r\nB01,1\r\n"

    checksum_lines = (first / "SHA256SUMS").read_text().splitlines()
    assert len(checksum_lines) == len(first_paths) - 1
    for line in checksum_lines:
        digest, name = line.split("  ", 1)
        assert digest == sha256_bytes((first / name).read_bytes())


def test_tracked_and_generated_public_artifacts_have_no_local_or_secret_values() -> None:
    tracked_archive = ROOT / "data" / "nucleic-acid-results" / "2026-08-05" / "source-csvs.zip"
    assert tracked_archive.is_file()
    assert_safe_artifact(tracked_archive)

    public_root = ROOT / "site" / "public" / "data" / "nucleic-acids"
    if public_root.is_dir():
        for path in sorted(public_root.rglob("*")):
            if path.is_file():
                assert_safe_artifact(path)

    public_schema = ROOT / "site" / "public" / "schema" / "nucleic-acid-results.schema.json"
    if public_schema.is_file():
        assert_safe_artifact(public_schema)

    release_root = ROOT / "release-artifacts"
    if release_root.is_dir():
        for path in sorted(release_root.rglob("*")):
            if path.is_file():
                assert_safe_artifact(path)
