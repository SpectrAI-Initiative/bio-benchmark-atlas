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
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "lifescibench")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "lifescibench-official-full")
    work = next(item for item in entities["work"] if item["id"] == "lifescibench-preprint")
    subsets = {item["id"]: item for item in benchmark["task_counts"]["subsets"]}
    notes = {item["tag"]: item for item in benchmark["coverage_notes"]}
    assert benchmark["audit"]["status"] == "audited"
    assert benchmark["latest_version"] == "initial-release"
    assert benchmark["versions"][0]["task_counts"] == benchmark["task_counts"]
    assert benchmark["task_counts"]["total"] == 750
    assert subsets["protein-primary-domain"]["count"] == 136
    assert subsets["protein-design-optimization"]["count"] == 62
    assert notes["protein-protein-binding"]["count"] is None
    assert notes["protein-protein-binding"]["reporting_status"] == "not_reported"
    assert run["benchmark_version"] == "initial-release"
    assert run["scope"] == {
        "type": "full", "n": 750, "subset_id": None, "filter": None, "reporting_status": "reported",
    }
    assert run["protocol"]["turns"]["value"] == "single-turn"
    assert run["protocol"]["tools"]["internet"]["value"] is True
    assert run["protocol"]["repeats"]["value"] is None
    assert run["protocol"]["repeats"]["reporting_status"] == "not_reported"
    assert {metric["source_label"] for metric in run["metrics"]} == {
        "Normalized rubric score", "Task pass rate",
    }
    assert {result["status"] for result in run["results"]} == {"verified"}
    assert work["title"] == "LifeSciBench: Evaluating Language Models on Realistic, Expert-Level Tasks in the Life Sciences"


def test_proteingym_versions_counts_binding_and_protocol() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "proteingym")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "proteingym-v10-dms-substitutions-zero-shot")
    work = next(item for item in entities["work"] if item["id"] == "proteingym-paper")
    versions = {item["label"]: item for item in benchmark["versions"]}
    latest_subsets = {item["id"]: item for item in benchmark["task_counts"]["subsets"]}
    v10_subsets = {item["id"]: item for item in versions["1.0"]["task_counts"]["subsets"]}
    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["latest_version"] == "1.3"
    assert versions["1.3"]["task_counts"] == benchmark["task_counts"]
    assert latest_subsets["dms-substitution-assays"]["count"] == 217
    assert latest_subsets["dms-indel-assays"]["count"] == 66
    assert latest_subsets["clinical-substitution-proteins"]["count"] == 2525
    assert latest_subsets["clinical-indel-proteins"]["count"] == 1555
    assert v10_subsets["dms-substitution-binding-assays"]["count"] == 14
    assert latest_subsets["dms-substitution-binding-assays"]["count"] == 13
    assert {item["path"] for item in benchmark["field_status"]} == {
        "/task_counts/subsets/1/count",
        "/versions/3/task_counts/subsets/1/count",
    }
    assert run["benchmark_version"] == "1.0"
    assert run["benchmark_id"] == "proteingym-dms-substitutions"
    assert run["scope"] == {
        "type": "full", "n": 217, "subset_id": None, "filter": None,
        "reporting_status": "reported",
    }
    assert {item["metric_id"] for item in run["metrics"]} == {
        "spearman-correlation", "auc-roc", "matthews-correlation",
        "ndcg-at-10-percent", "top-10-percent-recall",
    }
    assert run["protocol"]["statistical"]["value"].startswith("Non-parametric bootstrap")
    assert work["arxiv"] is None
    assert "Hansen Spinner" in work["authors"]


def test_casp_separates_rolling_round_tracks_and_assessment_units() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {item["id"]: item for item in entities["evaluation_run"]}
    root = benchmarks["casp"]
    versions = {item["label"]: item for item in root["versions"]}
    current_counts = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}
    completed_counts = {
        item["id"]: item["count"] for item in versions["CASP16"]["task_counts"]["subsets"]
    }

    assert root["audit"]["status"] == "audited-with-caveats"
    assert root["latest_version"] == "CASP17"
    assert versions["CASP17"]["status"] == "rolling"
    assert versions["CASP17"]["as_of"] == "2026-07-21"
    assert versions["CASP17"]["task_counts"] == root["task_counts"]
    assert root["task_counts"]["total"] is None
    assert current_counts == {
        "casp17-protein-no-stoichiometry": 75,
        "casp17-protein-with-stoichiometry": 61,
        "casp17-rna-targets": 45,
        "casp17-hybrid-targets": 8,
    }
    assert completed_counts["casp16-tertiary-releases"] == 156
    assert completed_counts["casp16-multimer-releases"] == 108
    assert completed_counts["casp16-pharma-pose-releases"] == 233
    assert completed_counts["casp16-pharma-affinity-releases"] == 140
    assert set(versions["CASP17"]["formal_tracks"]) == {
        "casp-protein-monomers", "casp-protein-multimers", "casp-protein-ligands",
    }
    assert benchmarks["casp-immune-complexes"]["parent_id"] == "casp-protein-multimers"
    assert benchmarks["casp-immune-complexes"]["task_counts"]["total"] is None

    monomer = benchmarks["casp-protein-monomers"]
    multimer = benchmarks["casp-protein-multimers"]
    ligand = benchmarks["casp-protein-ligands"]
    assert next(v for v in monomer["versions"] if v["label"] == "CASP16")["task_counts"]["total"] == 54
    assert next(v for v in multimer["versions"] if v["label"] == "CASP16")["task_counts"]["total"] == 40
    ligand_subsets = {
        item["id"]: item["count"]
        for item in next(v for v in ligand["versions"] if v["label"] == "CASP16")["task_counts"]["subsets"]
    }
    assert ligand_subsets["casp16-pharma-pose-assessed"] == 229
    assert ligand_subsets["casp16-affinity-stage1-analysis"] == 122
    assert ligand_subsets["casp16-affinity-stage2-releases"] == 110
    assert ligand_subsets["casp16-affinity-stage2-analysis"] == 103

    assert runs["casp16-monomer-regular-official"]["scope"]["n"] == 54
    assert runs["casp16-multimer-phase1-regular"]["scope"]["n"] == 40
    assert runs["casp16-ligand-pose-regular"]["scope"]["n"] == 229
    assert runs["casp16-ligand-affinity-stage1"]["scope"]["n"] == 122
    assert runs["casp16-ligand-affinity-stage2"]["scope"]["n"] == 103
    assert runs["casp16-ligand-affinity-stage1"]["scope"]["subset_id"] in ligand_subsets
    assert runs["casp16-ligand-affinity-stage2"]["comparability_group"] != runs["casp16-ligand-affinity-stage1"]["comparability_group"]
    assert {metric["metric_id"] for metric in runs["casp16-multimer-phase1-regular"]["metrics"]} == {
        "dockq", "tm-score", "lddt", "interface-contact-score", "interface-patch-score", "qs-best",
    }


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
    audit_statuses = {benchmark["audit"]["status"] for benchmark in payload["benchmarks"]}
    assert audit_statuses <= {"legacy", "audited", "audited-with-caveats"}
    assert "legacy" in audit_statuses


def _audited_lifescibench_entities() -> dict[str, list[dict[str, object]]]:
    entities = copy.deepcopy(load_entities())
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "lifescibench")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "lifescibench-official-full")
    if benchmark.get("audit", {}).get("status") in validator_module.AUDITED_STATUSES:
        return entities
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
    total_evidence_id = next(
        item["id"] for item in benchmark["evidence"] if "/task_counts/total" in item["supports"]
    )
    benchmark["audit"].update({"status": "audited-with-caveats", "unresolved_fields": 1})
    benchmark["field_status"] = [{
        "path": "/task_counts/total", "status": "provisional", "confidence": "low",
        "reason": "Synthetic regression case.", "evidence_ids": [total_evidence_id],
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


def test_comparability_group_rejects_any_protocol_difference(monkeypatch) -> None:
    entities = copy.deepcopy(load_entities())
    figqa_runs = [
        run for run in entities["evaluation_run"] if run["benchmark_id"] == "lab-bench-figqa"
    ]
    assert len(figqa_runs) >= 2
    figqa_runs[1]["comparability_group"] = figqa_runs[0]["comparability_group"]
    monkeypatch.setattr(validator_module, "load_entities", lambda: entities)
    try:
        validator_module.validate_registry()
    except RegistryValidationError as error:
        assert "mixes incompatible benchmark/version/scope/metrics" in str(error)
    else:
        raise AssertionError("incompatible protocols unexpectedly shared a comparability group")


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
        source_work_ids = {
            item.get("source_id") if item.get("source_type") == "work" else item.get("work_id")
            for item in benchmark["evidence"]
        }
        assert any(
            work_id in works and works[work_id]["source_class"] == "benchmark_creator"
            for work_id in source_work_ids
        )


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
