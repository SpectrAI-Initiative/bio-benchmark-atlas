from __future__ import annotations

import hashlib
import json
import copy
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from registry_io import load_entities  # noqa: E402
from check_sources import _sources  # noqa: E402
import validate_registry as validator_module  # noqa: E402
from validate_registry import RegistryValidationError, _resolve_pointer, validate_registry  # noqa: E402


def test_registry_validates_and_has_v1_depth() -> None:
    entities = validate_registry()
    families = [item for item in entities["benchmark"] if item["parent_id"] is None]
    assert len(families) == 15
    assert len(entities["evaluation_run"]) >= 15
    assert {work["source_class"] for work in entities["work"]} <= {
        "benchmark_creator",
        "official_model_provider",
    }


def test_lifescibench_protein_and_binding_contract() -> None:
    benchmark = next(item for item in load_entities()["benchmark"] if item["id"] == "lifescibench")
    subsets = {item["id"]: item for item in benchmark["task_counts"]["subsets"]}
    notes = {item["tag"]: item for item in benchmark["coverage_notes"]}
    assert benchmark["task_counts"]["total"] == 750
    assert subsets["protein-primary-domain"]["count"] == 136
    assert subsets["protein-design-optimization"]["count"] == 62
    assert notes["protein-protein-binding"]["count"] is None
    assert notes["protein-protein-binding"]["reporting_status"] == "not_reported"


def test_biomysterybench_scope_and_repeats() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "biomysterybench")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "biomysterybench-official-run")
    subsets = {item["id"]: item["count"] for item in benchmark["task_counts"]["subsets"]}
    assert benchmark["task_counts"]["total"] == 99
    assert subsets == {"human-solvable": 76, "human-difficult": 23}
    assert run["scope"]["type"] == "full"
    assert run["protocol"]["repeats"]["value"] == 5
    assert {metric["metric_id"] for metric in run["metrics"]} == {"accuracy", "consistency"}


def test_public_registry_contains_no_local_absolute_paths() -> None:
    for path in ROOT.glob("registry/**/*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert "/mnt/" not in text


def _export_hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "exports").glob("*"))
        if path.is_file()
    }


def test_build_is_deterministic_and_surfaces_match() -> None:
    subprocess.run([sys.executable, "scripts/build_registry.py"], cwd=ROOT, check=True)
    first = _export_hashes()
    subprocess.run([sys.executable, "scripts/build_registry.py"], cwd=ROOT, check=True)
    assert first == _export_hashes()
    assert (ROOT / "exports" / "registry.json").read_bytes() == (
        ROOT / "site" / "public" / "data" / "registry.json"
    ).read_bytes()
    payload = json.loads((ROOT / "exports" / "registry.json").read_text(encoding="utf-8"))
    assert payload["meta"]["version"] == "1.1.0-dev"


def test_v11_exports_surface_audit_and_result_status_columns() -> None:
    subprocess.run([sys.executable, "scripts/build_registry.py"], cwd=ROOT, check=True)
    benchmark_header = (ROOT / "exports" / "benchmarks.csv").read_text(encoding="utf-8").splitlines()[0]
    result_header = (ROOT / "exports" / "evaluation-results.csv").read_text(encoding="utf-8").splitlines()[0]
    assert {"audit_status", "provisional_fields", "conflicted_fields"} <= set(benchmark_header.split(","))
    assert {"result_status", "confidence", "result_evidence_ids"} <= set(result_header.split(","))
    payload = json.loads((ROOT / "exports" / "registry.json").read_text(encoding="utf-8"))
    assert {benchmark["audit"]["status"] for benchmark in payload["benchmarks"]} == {"legacy"}


def _audited_lifescibench_entities() -> dict[str, list[dict[str, object]]]:
    entities = copy.deepcopy(load_entities())
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "lifescibench")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "lifescibench-official-full")
    for index, resource in enumerate(benchmark["resources"], start=1):
        resource["id"] = f"lifescibench-resource-{index}"
        resource["last_checked"] = "2026-07-21"
    critical = sorted(validator_module.BENCHMARK_CRITICAL_PATHS)
    benchmark["evidence"] = [{
        "id": "lifescibench-core-evidence",
        "source_type": "work",
        "source_id": "lifescibench-preprint",
        "accessed_date": "2026-07-21",
        "locator": {"type": "section", "value": "Abstract; Table 2; Sections 2.1 and 4"},
        "supports": critical,
    }]
    benchmark["versions"] = [{
        "id": "lifescibench-v1-0",
        "label": "1.0",
        "status": "current",
        "release_date": "2026-06-17",
        "as_of": None,
        "task_counts": copy.deepcopy(benchmark["task_counts"]),
        "formal_tracks": [],
        "notes": None,
        "evidence_ids": ["lifescibench-core-evidence"],
    }]
    benchmark["audit"] = {
        "status": "audited", "audited_date": "2026-07-21", "unresolved_fields": 0, "notes": None,
    }
    benchmark["field_status"] = []
    run["evidence"] = [{
        "id": "lifescibench-run-evidence",
        "source_type": "work",
        "source_id": "lifescibench-preprint",
        "accessed_date": "2026-07-21",
        "locator": {"type": "section", "value": "Sections 3 and 4; overall results table"},
        "supports": ["/scope", "/protocol", "/metrics", "/results"],
    }]
    for result in run["results"]:
        result.update({
            "status": "verified", "confidence": "high",
            "evidence_ids": ["lifescibench-run-evidence"],
        })
    return entities


def test_audited_record_contract_and_provisional_total_blocks_full_scope(monkeypatch) -> None:
    entities = _audited_lifescibench_entities()
    monkeypatch.setattr(validator_module, "load_entities", lambda: entities)
    validator_module.validate_registry()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "lifescibench")
    benchmark["audit"].update({"status": "audited-with-caveats", "unresolved_fields": 1})
    benchmark["field_status"] = [{
        "path": "/task_counts/total", "status": "provisional", "confidence": "low",
        "reason": "Synthetic regression case.", "evidence_ids": ["lifescibench-core-evidence"],
    }]
    try:
        validator_module.validate_registry()
    except RegistryValidationError as error:
        assert "full scope cannot rely on a provisional/conflicted total" in str(error)
    else:
        raise AssertionError("provisional total unexpectedly allowed a full-scope evaluation")


def test_registry_pointer_resolution_supports_arrays_and_wildcards() -> None:
    document = {"results": [{"value": 1}, {"value": 2}], "task_counts": {"total": 2}}
    assert _resolve_pointer(document, "/task_counts/total")
    assert _resolve_pointer(document, "/results/*/value")
    assert not _resolve_pointer(document, "/results/*/missing")


def test_source_monitor_inventory_includes_works_and_resources() -> None:
    sources = _sources()
    assert sum(item["source_type"] == "work" for item in sources) == len(load_entities()["work"])
    assert sum(item["source_type"] == "resource" for item in sources) == sum(
        len(benchmark["resources"]) for benchmark in load_entities()["benchmark"]
    )
    assert len({item["source_key"] for item in sources}) == len(sources)


def test_source_monitor_opens_drift_issue_without_mutating_registry(tmp_path) -> None:
    report = tmp_path / "report.json"
    state = tmp_path / "state.json"
    issues = tmp_path / "issues.json"
    source = {
        "source_key": "work:example", "source_type": "work", "source_id": "example",
        "url": "https://example.org/source", "ok": True, "status": 200,
        "final_url": "https://example.org/source", "sha256": "new", "sha256_scope": "full",
        "etag": None, "last_modified": None, "checked_at": "2026-07-21T00:00:00+00:00",
    }
    report.write_text(json.dumps([source]), encoding="utf-8")
    state.write_text(json.dumps({"work:example": {"consecutive_failures": 0, "sha256": "old"}}), encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in ROOT.glob("registry/**/*.yaml")
    }
    subprocess.run([
        sys.executable, "scripts/update_source_state.py", "--report", str(report),
        "--state", str(state), "--issues", str(issues),
    ], cwd=ROOT, check=True)
    flagged = json.loads(issues.read_text(encoding="utf-8"))
    assert flagged[0]["monitor_reasons"] == ["fingerprint-changed"]
    assert before == {path: path.read_bytes() for path in ROOT.glob("registry/**/*.yaml")}


def test_all_primary_families_have_creator_evidence() -> None:
    entities = load_entities()
    works = {item["id"]: item for item in entities["work"]}
    for benchmark in entities["benchmark"]:
        if benchmark["parent_id"] is not None:
            continue
        assert any(works[item["work_id"]]["source_class"] == "benchmark_creator" for item in benchmark["evidence"])


def test_registry_yaml_has_no_empty_strings() -> None:
    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            assert value != ""

    for path in ROOT.glob("registry/**/*.yaml"):
        walk(yaml.safe_load(path.read_text(encoding="utf-8")))
