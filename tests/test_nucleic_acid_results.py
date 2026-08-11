from __future__ import annotations

import gzip
import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_nucleic_acid_results import (  # noqa: E402
    CATALOG_GZIP_LIMIT,
    PROTOCOL_GZIP_LIMIT,
    build,
)
from nucleic_acid_results import (  # noqa: E402
    AVAILABLE_SNAPSHOTS,
    EXPECTED_COUNTS,
    LOCAL_OR_SECRET_PATTERN,
    SNAPSHOT_DATE,
    SOURCE_ARCHIVE,
    SOURCE_MANIFEST,
    sha256_bytes,
)
from package_nucleic_acid_results import package  # noqa: E402
from validate_nucleic_acid_results import validate  # noqa: E402


def _load_gzip_json(path: Path) -> dict[str, object]:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


@pytest.fixture(scope="module")
def built_site(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path_factory.mktemp("nucleic-results-build")
    public = root / "public"
    generated = root / "generated"
    receipt = build(public, generated)
    return public, generated, receipt


def test_source_snapshot_validates_with_exact_counts_and_semantics() -> None:
    tables, manifest, crosswalks = validate()
    assert len(tables["benchmarks.csv"]) == EXPECTED_COUNTS["benchmarks"]
    assert len(tables["tasks.csv"]) == EXPECTED_COUNTS["tasks"]
    assert len(tables["evaluation_protocols.csv"]) == EXPECTED_COUNTS["protocols"]
    assert len(tables["benchmark_results.csv"]) == EXPECTED_COUNTS["results"]
    assert len(tables["baseline_sota_summary.csv"]) == EXPECTED_COUNTS["summaries"]
    assert len(tables["protocol_leaders.csv"]) == EXPECTED_COUNTS["leaders"]
    assert manifest["snapshot_date"] == SNAPSHOT_DATE
    assert len(crosswalks["benchmark_crosswalk"]) == EXPECTED_COUNTS["benchmarks"]
    assert len(crosswalks["task_crosswalk"]) == EXPECTED_COUNTS["tasks"]
    assert not any(
        row["claim_type"] == "strict_cross_work_sota"
        for row in tables["protocol_leaders.csv"]
    )


def test_source_package_is_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(SOURCE_ARCHIVE) as archive:
        archive.extractall(source)
    rebuilt_archive = tmp_path / "source-csvs.zip"
    rebuilt_manifest = tmp_path / "source-manifest.json"
    package(source, rebuilt_archive, rebuilt_manifest, SNAPSHOT_DATE)
    assert rebuilt_archive.read_bytes() == SOURCE_ARCHIVE.read_bytes()
    assert json.loads(rebuilt_manifest.read_text()) == json.loads(SOURCE_MANIFEST.read_text())


def test_manifest_and_catalog_have_expected_interface_and_limits(
    built_site: tuple[Path, Path, dict[str, object]],
) -> None:
    public, generated, receipt = built_site
    version = public / "data" / "nucleic-acids" / SNAPSHOT_DATE
    manifest = json.loads((version / "manifest.json").read_text())
    assert manifest["counts"]["benchmarks"] == 47
    assert manifest["counts"]["tasks"] == 58
    assert manifest["counts"]["protocols"] == 345
    assert manifest["counts"]["results"] == 56_014
    assert manifest["counts"]["summaries"] == 556
    assert manifest["counts"]["leaders"] == 695
    assert manifest["counts"]["strict_cross_work_sota"] == 0
    assert len(manifest["protocolChunks"]) == 345
    assert manifest["assets"]["catalog"]["compressedBytes"] < CATALOG_GZIP_LIMIT
    assert receipt["snapshots"][-1]["largest_protocol_gzip_bytes"] < PROTOCOL_GZIP_LIMIT

    catalog_descriptor = manifest["assets"]["catalog"]
    catalog = _load_gzip_json(version / catalog_descriptor["path"])
    assert len(catalog["benchmarks"]) == 47
    assert len(catalog["tasks"]) == 58
    assert len(catalog["protocols"]) == 345
    assert len(catalog["summaries"]) == 556
    assert len(catalog["leaders"]) == 695
    assert len(catalog["track_coverage"]) == 146
    assert {row["claim_type"] for row in catalog["leaders"]} == {
        "official_baseline",
        "original_table_best",
        "official_board_or_challenge_leader",
        "single_reported_result",
    }
    generated_version = generated / "nucleic-acid-results" / SNAPSHOT_DATE
    assert json.loads((generated_version / "catalog.json").read_text()) == catalog
    assert (generated_version / "entities.json").is_file()
    assert (generated_version / "usage-index.json").is_file()
    assert json.loads((generated_version / "manifest.json").read_text()) == manifest
    latest = json.loads((public / "data" / "nucleic-acids" / "latest.json").read_text())
    assert latest["snapshot_date"] == SNAPSHOT_DATE
    assert latest["available_snapshots"] == list(AVAILABLE_SNAPSHOTS)
    old_manifest = json.loads((public / "data" / "nucleic-acids" / "2026-08-05" / "manifest.json").read_text())
    assert old_manifest["counts"]["results"] == 55_989
    assert old_manifest["counts"]["protocols"] == 334


def test_every_result_appears_in_exactly_one_hashed_protocol_chunk(
    built_site: tuple[Path, Path, dict[str, object]],
) -> None:
    public, _, _ = built_site
    version = public / "data" / "nucleic-acids" / SNAPSHOT_DATE
    manifest = json.loads((version / "manifest.json").read_text())
    result_ids: set[str] = set()
    result_rows = 0
    for protocol_id, descriptor in manifest["protocolChunks"].items():
        path = version / descriptor["path"]
        compressed = path.read_bytes()
        assert sha256_bytes(compressed) == descriptor["sha256"]
        assert path.name == f"{descriptor['sha256']}.json.gz"
        assert len(compressed) == descriptor["compressedBytes"]
        assert len(gzip.decompress(compressed)) == descriptor["uncompressedBytes"]
        assert descriptor["compressedBytes"] < PROTOCOL_GZIP_LIMIT
        chunk = _load_gzip_json(path)
        assert chunk["protocol_id"] == protocol_id
        assert len(chunk["results"]) == descriptor["rowCount"]
        for result in chunk["results"]:
            assert result["protocol_id"] == protocol_id
            assert result["result_id"] not in result_ids
            assert result["sources"]
            assert any(
                source["result_source_id"] == result["canonical_evidence_source_id"]
                and source["canonical_evidence"] == "true"
                for source in result["sources"]
            )
            result_ids.add(result["result_id"])
            result_rows += 1
    assert result_rows == EXPECTED_COUNTS["results"]
    assert len(result_ids) == EXPECTED_COUNTS["results"]


def test_public_artifacts_have_no_local_paths_or_signed_credentials(
    built_site: tuple[Path, Path, dict[str, object]],
) -> None:
    public, generated, _ = built_site
    paths = [path for path in public.rglob("*") if path.is_file()]
    paths += [path for path in generated.rglob("*") if path.is_file()]
    for path in paths:
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        if path.suffix == ".json" or path.name.endswith(".json.gz"):
            text = data.decode("utf-8")
            assert LOCAL_OR_SECRET_PATTERN.search(text) is None, path


def test_two_builds_are_byte_identical(
    built_site: tuple[Path, Path, dict[str, object]], tmp_path: Path
) -> None:
    public, generated, _ = built_site
    second_public = tmp_path / "public"
    second_generated = tmp_path / "generated"
    build(second_public, second_generated)
    first_files = {
        path.relative_to(public): sha256_bytes(path.read_bytes())
        for path in public.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second_public): sha256_bytes(path.read_bytes())
        for path in second_public.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    first_generated = {
        path.relative_to(generated): sha256_bytes(path.read_bytes())
        for path in generated.rglob("*")
        if path.is_file()
    }
    second_generated_files = {
        path.relative_to(second_generated): sha256_bytes(path.read_bytes())
        for path in second_generated.rglob("*")
        if path.is_file()
    }
    assert first_generated == second_generated_files
