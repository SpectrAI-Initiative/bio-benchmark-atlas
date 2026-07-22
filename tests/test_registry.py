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

from registry_io import load_entities, load_meta, load_taxonomies  # noqa: E402
from check_sources import _sources  # noqa: E402
import validate_registry as validator_module  # noqa: E402
from validate_registry import RegistryValidationError, _resolve_pointer, validate_registry  # noqa: E402
from build_registry import _build_payload  # noqa: E402


def test_registry_validates_and_has_v1_depth() -> None:
    entities = validate_registry()
    families = [item for item in entities["benchmark"] if item["parent_id"] is None]
    assert len(families) == 24
    assert len(entities["evaluation_run"]) >= 15
    assert {work["source_class"] for work in entities["work"]} <= {
        "benchmark_creator",
        "official_model_provider",
        "independent_reproduction",
    }
    assert all(work["current_version_id"] in {version["id"] for version in work["source_versions"]} for work in entities["work"])
    assert all(run["work_version_id"] for run in entities["evaluation_run"])
    assert all("selection" in run["scope"] for run in entities["evaluation_run"])
    assert all(metric["kind"] in {"absolute", "delta"} for run in entities["evaluation_run"] for metric in run["metrics"])


def test_bixbench_anthropic_claim_remains_partial_without_invented_results() -> None:
    entities = load_entities()
    use = next(item for item in entities["benchmark_use"] if item["id"] == "anthropic-life-sciences-bixbench")
    assert use["relation_type"] == "evaluation"
    assert use["status"] == "partial"
    assert use["benchmark_version"] is None
    assert use["scope"]["type"] == "unknown"
    assert use["scope"]["n"] is None
    assert use["metric_labels"] == []
    assert use["evaluation_run_ids"] == []
    assert set(use["model_ids"]) == {"claude-sonnet-4", "claude-sonnet-4-5"}
    gaps = " · ".join(use["reporting_gaps"])
    for phrase in ("benchmark version", "realized n", "metric", "numeric results", "prompt"):
        assert phrase in gaps


def test_spatialbench_versions_harnesses_and_external_summary_are_separate() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "spatialbench")
    versions = {version["label"]: version for version in benchmark["versions"]}
    assert benchmark["latest_version"] == "repo-159-5042c4f"
    assert benchmark["task_counts"]["total"] == 159
    assert versions["paper-v2"]["task_counts"]["total"] == 146
    current_subsets = benchmark["task_counts"]["subsets"]
    assert sum(item["count"] for item in current_subsets if item.get("partition_group") == "task-category") == 159
    assert sum(item["count"] for item in current_subsets if item.get("partition_group") == "platform") == 159
    historical_subsets = versions["paper-v2"]["task_counts"]["subsets"]
    assert sum(item["count"] for item in historical_subsets if item.get("partition_group") == "task-category") == 147
    assert sum(item["count"] for item in historical_subsets if item.get("partition_group") == "platform") == 147
    assert any(status["path"] == "/versions/0/task_counts/subsets" and status["status"] == "conflicted" for status in benchmark["field_status"])
    runs = [run for run in entities["evaluation_run"] if run["benchmark_id"] == "spatialbench"]
    assert {run["benchmark_version"] for run in runs} == {"paper-v2", "repo-159-5042c4f"}
    assert {run["comparability_group"] for run in runs} == {run["id"] for run in runs}
    assert len(runs) == 7
    summary = next(use for use in entities["benchmark_use"] if use["id"] == "anthropic-spatialbench-external-summary")
    assert summary["relation_type"] == "external-result-summary"
    assert summary["status"] == "partial"
    assert summary["evaluation_run_ids"] == []
    assert summary["scope"]["n"] == 146


def test_anthropic_internal_suite_preserves_only_labeled_deltas() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    root = benchmarks["anthropic-key-life-sciences-evals"]
    children = {item["id"] for item in entities["benchmark"] if item["parent_id"] == root["id"]}
    assert root["access"]["level"] == "private-internal"
    assert root["task_counts"]["total"] is None
    assert children == {
        "anthropic-scientific-figure-interpretation",
        "anthropic-computational-biology",
        "anthropic-protein-understanding",
    }
    expected = {
        "anthropic-scientific-figure-delta": 13.2,
        "anthropic-computational-biology-delta": 10.5,
        "anthropic-protein-understanding-delta": 10.3,
    }
    runs = {run["id"]: run for run in entities["evaluation_run"] if run["benchmark_id"] in children}
    assert set(runs) == set(expected)
    for run_id, delta in expected.items():
        run = runs[run_id]
        assert run["scope"]["type"] == "unknown" and run["scope"]["n"] is None
        assert len(run["metrics"]) == 1
        assert run["metrics"][0]["kind"] == "delta"
        assert run["metrics"][0]["baseline_model_id"] == "claude-opus-4-1"
        assert [(result["model_id"], result["value"]) for result in run["results"]] == [("claude-opus-4-5", delta)]
        assert run["protocol"]["grader"]["reporting_status"] == "not_reported"
    payload = _build_payload()
    internal_coverage = [row for row in payload["scientific_task_coverage"] if row["benchmark_id"].startswith("anthropic-")]
    assert internal_coverage and all(row["aggregate_eligible"] is False for row in internal_coverage)


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
        "type": "full", "n": 750, "subset_id": None, "filter": None, "selection": None, "reporting_status": "reported",
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
        "selection": None, "reporting_status": "reported",
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


def test_cameo_uses_a_dated_rolling_snapshot_and_common_subsets() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "cameo")
    work = next(item for item in entities["work"] if item["id"] == "cameo-paper")
    runs = {item["id"]: item for item in entities["evaluation_run"]}
    versions = {item["label"]: item for item in benchmark["versions"]}
    study = versions["2024-complex-study"]
    subsets = {item["id"]: item["count"] for item in study["task_counts"]["subsets"]}

    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["latest_version"] == "current-complex-3d"
    assert versions["current-complex-3d"]["status"] == "rolling"
    assert versions["current-complex-3d"]["as_of"] == "2026-07-21"
    assert versions["current-complex-3d"]["task_counts"] == benchmark["task_counts"]
    assert benchmark["task_counts"]["total"] is None
    assert benchmark["access"]["license"].startswith("CAMEO-provided data")
    assert {status["path"] for status in benchmark["field_status"]} == {"/release_date"}
    assert work["doi"] == "10.1002/prot.70060"
    assert work["publication_date"] == "2025-09-28"

    assert study["task_counts"]["total"] == 7150
    assert subsets["cameo-2024-medium"] == 1332
    assert subsets["cameo-2024-hard"] == 1981
    assert subsets["cameo-2024-ligand"] == 3837
    assert subsets["cameo-2024-ligand-baseline-common"] == 2584
    assert subsets["cameo-2024-ppi-three-server-common"] == 392
    assert subsets["cameo-2024-antibody-three-server-common"] == 83

    ligand_run = runs["cameo-2024-ligand-baseline-common"]
    ppi_run = runs["cameo-2024-ppi-three-server-common"]
    antibody_run = runs["cameo-2024-antibody-three-server-common"]
    assert ligand_run["scope"]["n"] == 2584
    assert len(ligand_run["model_ids"]) == 4
    assert ppi_run["scope"]["n"] == 392
    assert ppi_run["protocol"]["time_budget"]["value"] == "approximately 3.5 days per weekly target"
    assert antibody_run["scope"]["n"] == 83
    assert {(row["model_id"], row["value"]) for row in antibody_run["results"]} == {
        ("cameo-alphafold3-v301", 0.83), ("cameo-multifold-2024", 0.76),
    }


def test_flip_separates_task_counts_landscape_samples_and_all_split_protocols() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"].startswith("flip-")
    }
    root = benchmarks["flip"]
    root_subsets = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}

    assert root["audit"]["status"] == "audited"
    assert root["latest_version"] == "original-2021"
    assert root["versions"][0]["task_counts"] == root["task_counts"]
    assert root["task_counts"]["total"] == 15
    assert root_subsets == {
        "flip-aav-tasks": 7,
        "flip-gb1-tasks": 5,
        "flip-meltome-tasks": 3,
        "flip-active-comparison-tasks": 13,
        "flip-discourse-sampled-tasks": 2,
    }
    assert root["versions"][0]["formal_tracks"] == ["flip-aav", "flip-gb1", "flip-meltome"]
    assert root["capabilities"] == ["prediction", "regression"]
    binding = next(item for item in root["coverage_notes"] if item["tag"] == "protein-protein-binding")
    assert binding["count"] == 5

    aav = benchmarks["flip-aav"]
    gb1 = benchmarks["flip-gb1"]
    meltome = benchmarks["flip-meltome"]
    assert aav["task_counts"]["total"] == 284009
    assert gb1["task_counts"]["total"] == 8733
    assert meltome["task_counts"]["total"] == 27951
    assert {item["id"]: item["count"] for item in aav["task_counts"]["subsets"]} == {
        "aav-mut-des-test": 201426,
        "aav-des-mut-test": 82583,
        "aav-one-vs-rest-test": 81413,
        "aav-two-vs-rest-test": 50776,
        "aav-seven-vs-rest-test": 12581,
        "aav-low-vs-high-test": 35037,
        "aav-sampled-test": 16517,
    }
    assert {item["id"]: item["count"] for item in gb1["task_counts"]["subsets"]} == {
        "gb1-one-vs-rest-test": 8704,
        "gb1-two-vs-rest-test": 8306,
        "gb1-three-vs-rest-test": 5765,
        "gb1-low-vs-high-test": 3644,
        "gb1-sampled-test": 1772,
    }
    meltome_counts = {item["id"]: item["count"] for item in meltome["task_counts"]["subsets"]}
    assert meltome_counts["meltome-human-cell-all"] == 7158
    assert meltome_counts["meltome-human-cell-test"] == 1366
    assert meltome["field_status"] == []
    assert "paper Table 2 prints 7,156" in next(
        item["notes"] for item in meltome["task_counts"]["subsets"]
        if item["id"] == "meltome-human-cell-all"
    )

    assert len(runs) == 15
    assert all(run["scope"]["type"] == "subset" for run in runs.values())
    assert all({metric["metric_id"] for metric in run["metrics"]} == {
        "spearman-correlation", "mean-squared-error",
    } for run in runs.values())
    assert len({run["comparability_group"] for run in runs.values()}) == 15
    assert runs["flip-aav-mut-des"]["scope"]["n"] == 201426
    assert runs["flip-meltome-human-cell"]["scope"]["n"] == 1366
    assert len(runs["flip-aav-des-mut"]["model_ids"]) == 9
    assert {(row["model_id"], row["value"]) for row in runs["flip-gb1-three-vs-rest"]["results"]} >= {
        ("flip-cnn", 0.83), ("flip-esm1v-per-aa", 0.82),
    }
    assert {(row["model_id"], row["value"]) for row in runs["flip-meltome-human-cell"]["results"]} >= {
        ("flip-esm1v-per-aa", 0.78), ("flip-ridge", 0.24),
    }
    assert {row["status"] for run in runs.values() for row in run["results"]} == {"verified"}
    assert {row["confidence"] for run in runs.values() for row in run["results"]} == {"high"}


def test_proteinlmbench_pins_release_resolves_choice_counts_and_registers_table3() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "proteinlmbench")
    work = next(item for item in entities["work"] if item["id"] == "proteinlmbench-paper")
    run = next(item for item in entities["evaluation_run"] if item["id"] == "proteinlmbench-creator-full")
    versions = {item["label"]: item for item in benchmark["versions"]}
    counts = {item["id"]: item["count"] for item in benchmark["task_counts"]["subsets"]}

    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["release_date"] == "2024-04-29"
    assert benchmark["latest_version"] == "hf-f139796"
    assert versions["hf-f139796"]["task_counts"] == benchmark["task_counts"]
    assert benchmark["task_counts"]["total"] == 944
    assert counts == {
        "proteinlmbench-two-choice": 3,
        "proteinlmbench-three-choice": 21,
        "proteinlmbench-four-choice": 42,
        "proteinlmbench-five-choice": 1,
        "proteinlmbench-six-choice": 871,
        "proteinlmbench-seven-choice": 2,
        "proteinlmbench-eight-choice": 1,
        "proteinlmbench-ten-choice": 3,
    }
    assert sum(counts.values()) == 944
    assert versions["hf-c59f90c"]["task_counts"]["total"] == 944
    assert versions["paper-v2"]["task_counts"]["subsets"][0]["count"] == 944
    assert {item["path"] for item in benchmark["field_status"]} == {
        "/task_formats",
        "/task_counts/subsets/4/count",
        "/versions/1/task_counts/subsets/4/count",
        "/access/license",
    }
    dataset = next(item for item in benchmark["resources"] if item["id"] == "proteinlmbench-dataset-resource")
    repository = next(item for item in benchmark["resources"] if item["id"] == "proteinlmbench-repository-resource")
    assert dataset["pin"]["value"] == "f1397963c7f727a4a2f00cdd691e6e219c36e992"
    assert repository["pin"]["value"] == "d8586e22ff85f6805edea0bbc23002aaccf525c4"
    assert work["authors"][0] == "Yiqing Shen"
    assert work["publication_date"] == "2024-06-08"

    assert run["benchmark_version"] == "paper-v2"
    assert run["scope"] == {
        "type": "full", "n": 944, "subset_id": None,
        "filter": "All 944 paper-defined ProteinLMBench questions; the paper does not pin an exact Hugging Face commit.",
        "selection": None, "reporting_status": "reported",
    }
    assert run["protocol"]["shots"]["value"] == 0
    assert run["protocol"]["temperature"]["value"] == 0.1
    assert run["protocol"]["token_budget"]["value"] == "20 generated tokens per question"
    assert run["protocol"]["repeats"]["value"] == 1
    assert run["protocol"]["seed"]["value"] == "not set"
    assert len(run["model_ids"]) == 18
    assert len(run["results"]) == 36
    assert {item["metric_id"] for item in run["metrics"]} == {"accuracy", "inference-time-minutes"}
    assert {(row["model_id"], row["metric_id"], row["value"]) for row in run["results"]} >= {
        ("toursynbio-7b", "accuracy", 62.18),
        ("proteinlmbench-gpt4-turbo", "accuracy", 57.94),
        ("proteinlmbench-chatglm3-6b", "inference-time-minutes", 8.00),
    }
    assert next(item for item in entities["model"] if item["id"] == "proteinlmbench-gpt4-turbo")["version_status"] == "not_reported"


def test_biology_instructions_separates_21_tasks_splits_prompts_and_results() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"].startswith("bioinstruction-")
    }
    root = benchmarks["bioinstruction"]
    work = next(item for item in entities["work"] if item["id"] == "bioinstructions-paper")
    current = next(item for item in root["versions"] if item["label"] == "emnlp-2025")
    groups = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}

    assert root["name"] == "Biology-Instructions"
    assert root["audit"]["status"] == "audited-with-caveats"
    assert root["latest_version"] == "emnlp-2025"
    assert root["task_counts"]["total"] == 21
    assert groups == {
        "bioinstruction-dna-tasks": 6,
        "bioinstruction-rna-tasks": 6,
        "bioinstruction-protein-tasks": 5,
        "bioinstruction-multi-molecule-tasks": 4,
    }
    assert len(current["formal_tracks"]) == 21
    assert set(current["formal_tracks"]) == {
        item_id for item_id, item in benchmarks.items() if item.get("parent_id") == "bioinstruction"
    }
    assert root["access"]["level"] == "partially-open"
    assert root["access"]["license"] is None
    assert {item["path"] for item in root["field_status"]} == {
        "/access/artifacts", "/implementations",
    }
    assert "8,002" in root["access"]["artifacts"]
    assert "244,681" in current["notes"]
    assert work["publication_date"] == "2025-11-04"
    assert "Yuan Dong" in work["authors"]

    child_tracks = [benchmarks[item_id] for item_id in current["formal_tracks"]]
    assert all(item["audit"]["status"] == "audited" for item in child_tracks)
    assert all(item["task_counts"]["total"] == sum(
        split["count"] for split in item["task_counts"]["subsets"]
    ) for item in child_tracks)
    test_counts = {
        item["id"]: next(
            split["count"] for split in item["task_counts"]["subsets"]
            if split["id"] == f"{item['id']}-test"
        )
        for item in child_tracks
    }
    assert sum(test_counts.values()) == 243_227
    assert test_counts["bioinstruction-emp"] == 28_741
    assert test_counts["bioinstruction-aan"] == 3_301
    assert test_counts["bioinstruction-rpi"] == 4_164

    assert len(runs) == 63
    assert sum(len(run["results"]) for run in runs.values()) == 345
    assert all(run["scope"]["type"] == "subset" for run in runs.values())
    assert all(run["scope"]["n"] == test_counts[run["benchmark_id"]] for run in runs.values())
    assert len({run["comparability_group"] for run in runs.values()}) == 63
    assert sum(run["id"].endswith("-open-baselines") for run in runs.values()) == 21
    assert sum(run["id"].endswith("-closed-baselines") for run in runs.values()) == 21
    assert sum(run["id"].endswith("-creator-systems") for run in runs.values()) == 21
    assert runs["bioinstruction-emp-open-baselines"]["protocol"]["system_prompt_public"]["value"] is False
    assert runs["bioinstruction-emp-closed-baselines"]["protocol"]["system_prompt_public"]["value"] is True
    assert runs["bioinstruction-emp-creator-systems"]["protocol"]["system_prompt_public"]["value"] is True
    assert {(row["model_id"], row["value"]) for row in runs["bioinstruction-rpi-creator-systems"]["results"]} >= {
        ("bioinstruction-chatmultiomics-stage12", 70.80),
        ("bioinstruction-chatmultiomics-stage123", 74.26),
    }
    assert {(row["model_id"], row["metric_id"], row["value"]) for row in runs["bioinstruction-ea-closed-baselines"]["results"]} >= {
        ("bioinstruction-gpt4o", "pcc-housekeeping", -1.17),
        ("bioinstruction-gpt4o", "pcc-developmental", -1.49),
    }
    assert {row["status"] for run in runs.values() for row in run["results"]} == {"verified"}
    assert {row["confidence"] for run in runs.values() for row in run["results"]} == {"high"}
    assert next(item for item in entities["model"] if item["id"] == "bioinstruction-gpt4o")["version_status"] == "not_reported"


def test_lab_bench_resolves_counts_tracks_creator_results_and_official_runs() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {item["id"]: item for item in entities["evaluation_run"]}
    root = benchmarks["lab-bench"]
    work = next(item for item in entities["work"] if item["id"] == "lab-bench-paper")
    versions = {item["label"]: item for item in root["versions"]}
    counts = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}
    direct_children = {
        item["id"] for item in benchmarks.values() if item.get("parent_id") == "lab-bench"
    }

    assert root["audit"]["status"] == "audited-with-caveats"
    assert root["latest_version"] == "repository-998a8e0"
    assert root["task_counts"]["total"] == 2_457
    assert versions["repository-998a8e0"]["task_counts"] == root["task_counts"]
    assert counts == {
        "lab-bench-litqa2": 248,
        "lab-bench-suppqa": 102,
        "lab-bench-figqa": 226,
        "lab-bench-tableqa": 305,
        "lab-bench-dbqa": 650,
        "lab-bench-protocolqa": 135,
        "lab-bench-seqqa": 750,
        "lab-bench-cloning-scenarios": 41,
        "lab-bench-broad-categories": 8,
        "lab-bench-formal-task-files": 31,
        "lab-bench-readme-narrower-subtasks": 30,
    }
    assert direct_children == {
        "lab-bench-litqa2", "lab-bench-suppqa", "lab-bench-figqa", "lab-bench-tableqa",
        "lab-bench-dbqa", "lab-bench-protocolqa", "lab-bench-seqqa",
        "lab-bench-cloning-scenarios",
    }
    assert set(versions["repository-998a8e0"]["formal_tracks"]) == direct_children
    assert len(versions["paper-v3"]["task_counts"]["subsets"]) == 10
    assert {item["path"] for item in root["field_status"]} == {
        "/task_counts/subsets/10/count",
        "/versions/1/task_counts/subsets/10/count",
    }
    assert root["access"]["level"] == "partially-open"
    assert "1,967" in root["access"]["tasks"] and "490" in root["access"]["tasks"]
    assert root["access"]["license"] == "CC-BY-SA-4.0"
    assert work["title"] == "LAB-Bench: Measuring Capabilities of Language Models for Biology Research"
    assert work["authors"] == [
        "Jon M. Laurent", "Joseph D. Janizek", "Michael Ruzo", "Michaela M. Hinks",
        "Michael J. Hammerling", "Siddharth Narayanan", "Manvitha Ponnapati",
        "Andrew D. White", "Samuel G. Rodriques",
    ]

    db_tracks = benchmarks["lab-bench-dbqa"]["versions"][-1]["formal_tracks"]
    seq_tracks = benchmarks["lab-bench-seqqa"]["versions"][-1]["formal_tracks"]
    assert len(db_tracks) == 10 and sum(benchmarks[item]["task_counts"]["total"] for item in db_tracks) == 650
    assert len(seq_tracks) == 15 and sum(benchmarks[item]["task_counts"]["total"] for item in seq_tracks) == 750
    assert benchmarks["lab-bench-dbqa-viral-ppi"]["task_counts"]["total"] == 50

    creator_runs = [run for run in runs.values() if run["id"].endswith("-creator-mcq")]
    assert len(creator_runs) == 31
    assert sum(len(run["results"]) for run in creator_runs) == 642
    assert all(run["benchmark_version"] == "paper-v3" for run in creator_runs)
    assert all(run["scope"]["type"] == "full" for run in creator_runs)
    assert all(run["protocol"]["repeats"]["value"] == 3 for run in creator_runs)
    assert all(run["protocol"]["tools"]["internet"]["value"] is False for run in creator_runs)
    assert {
        row["model_id"] for row in runs["lab-bench-figqa-creator-mcq"]["results"]
    }.isdisjoint({"lab-bench-meta-llama-3-70b-instruct"})
    assert {
        row["model_id"] for row in runs["lab-bench-tableqa-creator-mcq"]["results"]
    }.isdisjoint({"lab-bench-meta-llama-3-70b-instruct"})

    def result_value(run_id: str, model_id: str, metric_id: str) -> float:
        return next(
            row["value"] for row in runs[run_id]["results"]
            if row["model_id"] == model_id and row["metric_id"] == metric_id
        )

    assert result_value(
        "lab-bench-figqa-creator-mcq", "lab-bench-claude-3-5-sonnet-20240620", "accuracy",
    ) == 0.46
    assert result_value(
        "lab-bench-dbqa-viral-ppi-creator-mcq", "lab-bench-gpt-4o-unversioned", "precision",
    ) == 0.69
    assert result_value(
        "lab-bench-seqqa-pcr-seq-primers-creator-mcq",
        "lab-bench-claude-3-5-sonnet-20240620", "accuracy",
    ) == 0.97
    llama_run = runs["lab-bench-cloning-scenarios-creator-mcq-llama-context"]
    assert llama_run["scope"]["n"] == 41
    assert "only 25 prompts" in llama_run["scope"]["filter"]
    assert len(llama_run["results"]) == 3

    open_runs = [run for run in runs.values() if run["id"].endswith("-creator-open-response")]
    assert {(run["benchmark_id"], run["scope"]["n"]) for run in open_runs} == {
        ("lab-bench-figqa", 10), ("lab-bench-protocolqa", 20),
        ("lab-bench-cloning-scenarios", 10),
    }
    assert all(run["protocol"]["grader"]["human_review"] is True for run in open_runs)
    assert sum(len(run["results"]) for run in open_runs) == 6

    sonnet45 = runs["lab-bench-protocolqa-anthropic-sonnet45-system-card"]
    assert sonnet45["scope"]["type"] == "track" and sonnet45["scope"]["n"] is None
    assert sonnet45["protocol"]["shots"]["value"] == 10
    assert sonnet45["protocol"]["tools"]["internet"]["value"] is False
    assert {(row["model_id"], row["value"]) for row in sonnet45["results"]} == {
        ("claude-opus-4", 0.796), ("claude-opus-4-1", 0.833),
    }
    no_tools = runs["lab-bench-figqa-no-tools"]
    crop = runs["lab-bench-figqa-crop-tool"]
    assert no_tools["protocol"]["reasoning"]["value"] == "adaptive thinking at max effort"
    assert no_tools["protocol"]["repeats"]["value"] == 5
    assert {(row["model_id"], row["value"]) for row in no_tools["results"]} == {
        ("claude-sonnet-4-6", 58.8), ("claude-sonnet-4-5", 53.4),
        ("claude-opus-4-6", 58.0),
    }
    assert {(row["model_id"], row["value"]) for row in crop["results"]} == {
        ("claude-sonnet-4-6", 77.1), ("claude-sonnet-4-5", 59.3),
        ("claude-opus-4-6", 78.3),
    }
    lab_results = [
        row for run in runs.values() if run["benchmark_id"].startswith("lab-bench")
        for row in run["results"]
    ]
    assert {row["status"] for row in lab_results} == {"verified"}
    assert {row["confidence"] for row in lab_results} == {"high"}


def test_genebench_pro_partitions_release_strata_and_all_sixty_configurations() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "genebench-pro")
    work = next(item for item in entities["work"] if item["id"] == "genebench-pro-report")
    runs = {
        item["id"]: item for item in entities["evaluation_run"]
        if item["benchmark_id"] == "genebench-pro"
    }
    subsets = {item["id"]: item for item in benchmark["task_counts"]["subsets"]}

    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["latest_version"] == "paper-v1"
    assert benchmark["task_counts"]["total"] == 129
    assert benchmark["versions"][0]["task_counts"] == benchmark["task_counts"]
    release_counts = {
        item_id: subsets[item_id]["count"] for item_id in (
            "genebench-pro-public-release",
            "genebench-pro-artificial-analysis",
            "genebench-pro-internal-holdout",
        )
    }
    assert release_counts == {
        "genebench-pro-public-release": 10,
        "genebench-pro-artificial-analysis": 50,
        "genebench-pro-internal-holdout": 69,
    }
    assert all(subsets[item_id]["exclusive"] and subsets[item_id]["exhaustive"] for item_id in release_counts)
    primary = [item for item in subsets.values() if item["id"].startswith("genebench-pro-primary-")]
    terminal = [item for item in subsets.values() if item["id"].startswith("genebench-pro-terminal-")]
    assert len(primary) == 10 and sum(item["count"] for item in primary) == 129
    assert len(terminal) == 21 and sum(item["count"] for item in terminal) == 129
    assert subsets["genebench-pro-externally-reviewed"]["count"] == 82
    assert subsets["genebench-pro-not-externally-reviewed"]["count"] == 47
    assert "single-cell" in benchmark["domains"]
    binding = next(item for item in benchmark["coverage_notes"] if item["tag"] == "protein-protein-binding")
    assert binding["count"] is None and binding["reporting_status"] == "not_reported"
    assert {item["path"] for item in benchmark["field_status"]} == {"/access/license"}
    assert "CC-BY-4.0" in benchmark["access"]["license"] and "MIT" in benchmark["access"]["license"]
    public_resource = next(
        item for item in benchmark["resources"] if item["id"] == "genebench-pro-public-dataset-resource"
    )
    assert public_resource["pin"]["value"] == "9bd2c54a6c0beef041e3504aa7eb65fc77783e18"
    assert work["authors"] == ["Jeremiah H. Li", "Andrew J. Ho"]
    assert work["doi"] == "10.64898/2026.06.29.735386"

    assert len(runs) == 13
    assert sum(len(run["results"]) for run in runs.values()) == 60
    assert {len(run["results"]) for run in runs.values()} >= {1, 3, 6, 10, 17}
    assert all(run["benchmark_version"] == "paper-v1" for run in runs.values())
    assert all(run["scope"]["type"] == "full" and run["scope"]["n"] == 129 for run in runs.values())
    assert all(run["protocol"]["tools"]["internet"]["value"] is False for run in runs.values())
    assert all(run["protocol"]["tools"]["code_execution"]["value"] is True for run in runs.values())
    assert all(run["protocol"]["tools"]["container"]["value"] is True for run in runs.values())
    assert all(run["protocol"]["grader"]["human_review"] is False for run in runs.values())
    assert all("20,000 resamples" in run["protocol"]["statistical"]["value"] for run in runs.values())
    assert all(run["metrics"][0]["aggregation"].startswith("unweighted mean") for run in runs.values())
    standard_runs = [run for run in runs.values() if not run["id"].startswith("genebench-pro-claude-") and run["id"] != "genebench-pro-pro-mode"]
    five_attempt_runs = [run for run in runs.values() if run not in standard_runs]
    assert {run["protocol"]["repeats"]["value"] for run in standard_runs} == {10}
    assert {run["protocol"]["repeats"]["value"] for run in five_attempt_runs} == {5}

    def result(run_id: str, model_id: str) -> dict[str, object]:
        return next(row for row in runs[run_id]["results"] if row["model_id"] == model_id)

    assert result("genebench-pro-official", "gpt-5-4")["value"] == 8.9
    assert result("genebench-pro-official", "gpt-5-5")["ci_high"] == 16.1
    sol_max = result("genebench-pro-standard-max", "gpt-5-6-sol")
    assert (sol_max["value"], sol_max["ci_low"], sol_max["ci_high"]) == (28.7, 22.5, 35.1)
    assert result("genebench-pro-claude-max", "claude-opus-4-8")["value"] == 16.0
    assert result("genebench-pro-pro-mode", "gpt-5-6-pro")["value"] == 31.5
    all_results = [row for run in runs.values() for row in run["results"]]
    assert all(row["ci_low"] is not None and row["ci_high"] is not None for row in all_results)
    assert {row["status"] for row in all_results} == {"verified"}
    assert {row["confidence"] for row in all_results} == {"high"}


def test_biomysterybench_scope_and_repeats() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "biomysterybench")
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"] == "biomysterybench"
    }
    subsets = {item["id"]: item["count"] for item in benchmark["task_counts"]["subsets"]}
    assert benchmark["audit"]["status"] == "audited"
    assert benchmark["latest_version"] == "v11"
    assert benchmark["access"]["level"] == "partially-open"
    assert benchmark["task_counts"]["total"] == 90
    assert subsets == {"human-solvable": 73, "human-difficult": 17}
    versions = {item["label"]: item for item in benchmark["versions"]}
    assert set(versions) == {"v8", "v11"}
    assert versions["v8"]["status"] == "superseded"
    assert versions["v8"]["task_counts"]["total"] == 99
    assert {item["id"]: item["count"] for item in versions["v8"]["task_counts"]["subsets"]} == {
        "human-solvable": 76,
        "human-difficult": 23,
    }
    assert versions["v11"]["status"] == "current"

    assert set(runs) == {
        "biomysterybench-official-run",
        "biomysterybench-v8-human-solvable",
        "biomysterybench-v8-human-difficult",
    }
    full = runs["biomysterybench-official-run"]
    assert full["benchmark_version"] == "v8"
    assert full["scope"]["type"] == "full"
    assert full["scope"]["n"] == 99
    assert full["protocol"]["repeats"]["value"] == 5
    assert full["protocol"]["tools"]["internet"]["value"] == "allowlisted external access"
    assert full["protocol"]["tools"]["code_execution"]["value"] is True
    assert full["protocol"]["tools"]["container"]["value"] is True
    assert full["protocol"]["grader"]["human_review"] is None
    assert "0-of-5 through 5-of-5" in full["protocol"]["statistical"]["value"]
    assert full["results"] == []

    solvable = runs["biomysterybench-v8-human-solvable"]
    difficult = runs["biomysterybench-v8-human-difficult"]
    assert (solvable["scope"]["type"], solvable["scope"]["n"], solvable["scope"]["subset_id"]) == (
        "subset",
        76,
        "human-solvable",
    )
    assert (difficult["scope"]["type"], difficult["scope"]["n"], difficult["scope"]["subset_id"]) == (
        "subset",
        23,
        "human-difficult",
    )
    assert {item["model_id"]: item["value"] for item in solvable["results"]} == {
        "claude-haiku-4-5": 36.8,
        "claude-sonnet-4-6": 71.8,
        "claude-opus-4-6": 77.4,
        "claude-opus-4-7": 78.9,
        "claude-mythos-preview": 82.6,
    }
    assert {item["model_id"]: item["value"] for item in difficult["results"]} == {
        "claude-haiku-4-5": 5.2,
        "claude-sonnet-4-6": 19.1,
        "claude-opus-4-6": 23.5,
        "claude-opus-4-7": 27.0,
        "claude-mythos-preview": 29.6,
    }
    for run in (solvable, difficult):
        assert run["protocol"]["repeats"]["value"] == 5
        assert {metric["metric_id"] for metric in run["metrics"]} == {"episode-accuracy"}
        assert {result["status"] for result in run["results"]} == {"verified"}
        assert {result["confidence"] for result in run["results"]} == {"high"}
        assert all(result["ci_low"] is None and result["ci_high"] is None for result in run["results"])


def test_compbiobench_counts_access_and_creator_protocols() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "compbiobench")
    work = next(item for item in entities["work"] if item["id"] == "compbiobench-preprint")
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"] == "compbiobench"
    }
    models = {item["id"]: item for item in entities["model"]}
    subsets = {item["id"]: item["count"] for item in benchmark["task_counts"]["subsets"]}

    assert benchmark["audit"]["status"] == "audited"
    assert benchmark["latest_version"] == "v1"
    assert benchmark["task_counts"]["total"] == 100
    assert benchmark["versions"][0]["task_counts"] == benchmark["task_counts"]
    assert sum(value for key, value in subsets.items() if key.startswith("compbiobench-domain-")) == 100
    assert sum(value for key, value in subsets.items() if key.startswith("compbiobench-style-")) == 100
    assert sum(value for key, value in subsets.items() if key.startswith("compbiobench-difficulty-")) == 100
    assert subsets["compbiobench-internet-required"] == 78
    assert subsets["compbiobench-internet-not-required"] == 22
    protein = next(item for item in benchmark["coverage_notes"] if item["tag"] == "protein-structure")
    assert protein["count"] == 1 and protein["reporting_status"] == "reported"
    binding = [item for item in benchmark["coverage_notes"] if "binding" in item["tag"]]
    assert all(item["count"] is None and item["reporting_status"] == "not_reported" for item in binding)
    assert benchmark["access"]["level"] == "partially-open"
    assert "private" in benchmark["access"]["grader"]
    assert {item["license"] for item in benchmark["resources"]} == {
        "CC BY-NC 4.0", "CC BY 4.0", "MIT", "Apache-2.0",
    }
    assert work["publication_date"] == "2026-04-09"
    assert work["doi"] == "10.64898/2026.04.06.716850"

    assert set(runs) == {
        "compbiobench-creator-full", "compbiobench-opus-full",
        "compbiobench-sonnet-full", "compbiobench-haiku-full",
        "compbiobench-codex-hardest", "compbiobench-opus-hardest",
        "compbiobench-sonnet-hardest", "compbiobench-haiku-hardest",
        "compbiobench-nonagentic-baselines",
    }
    agent_full = [
        runs["compbiobench-creator-full"], runs["compbiobench-opus-full"],
        runs["compbiobench-sonnet-full"], runs["compbiobench-haiku-full"],
    ]
    assert all(run["scope"]["type"] == "full" and run["scope"]["n"] == 100 for run in agent_full)
    assert all(run["protocol"]["tools"]["internet"]["value"] is True for run in agent_full)
    assert all(run["protocol"]["tools"]["code_execution"]["value"] is True for run in agent_full)
    assert all(run["protocol"]["tools"]["container"]["value"] is False for run in agent_full)
    assert all(run["protocol"]["grader"]["human_review"] is True for run in agent_full)
    assert {
        run["id"]: run["protocol"]["repeats"]["value"] for run in agent_full
    } == {
        "compbiobench-creator-full": 3, "compbiobench-opus-full": 3,
        "compbiobench-sonnet-full": 1, "compbiobench-haiku-full": 1,
    }

    def values(run_id: str) -> dict[str, float]:
        return {item["metric_id"]: item["value"] for item in runs[run_id]["results"]}

    assert values("compbiobench-creator-full") == {
        "accuracy": 83.3, "mean-wall-clock-time": 679.0, "mean-cost": 1.0,
    }
    assert values("compbiobench-opus-full")["accuracy"] == 81.0
    assert values("compbiobench-sonnet-full")["accuracy"] == 70.0
    assert values("compbiobench-haiku-full")["accuracy"] == 34.0
    hardest = {
        run_id: run["results"][0]["value"]
        for run_id, run in runs.items()
        if run_id.endswith("-hardest")
    }
    assert hardest == {
        "compbiobench-codex-hardest": 59.0,
        "compbiobench-opus-hardest": 69.0,
        "compbiobench-sonnet-hardest": 53.0,
        "compbiobench-haiku-hardest": 12.0,
    }
    assert all(
        run["scope"]["subset_id"] == "compbiobench-difficulty-levels-4-5"
        and run["scope"]["n"] == 17
        for run_id, run in runs.items() if run_id.endswith("-hardest")
    )
    baseline = runs["compbiobench-nonagentic-baselines"]
    assert baseline["protocol"]["repeats"]["value"] == 3
    assert all(setting["value"] is False for setting in baseline["protocol"]["tools"].values())
    assert {item["model_id"]: item["value"] for item in baseline["results"]} == {
        "chatgpt-5-2": 5.3, "claude-opus-4-6": 3.7,
    }
    assert models["codex-cli-gpt-5-4"]["version_string"] == "Codex CLI v0.115.0 + gpt-5.4"
    assert models["claude-code-opus-4-6"]["version_string"].startswith("Claude Code v2.1.87")
    all_results = [result for run in runs.values() for result in run["results"]]
    assert {result["status"] for result in all_results} == {"verified"}
    assert {result["confidence"] for result in all_results} == {"high"}


def test_bixbench_versions_units_and_creator_protocols() -> None:
    entities = load_entities()
    benchmark = next(item for item in entities["benchmark"] if item["id"] == "bixbench")
    works = {item["id"]: item for item in entities["work"]}
    models = {item["id"]: item for item in entities["model"]}
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"] == "bixbench"
    }
    subsets = {item["id"]: item["count"] for item in benchmark["task_counts"]["subsets"]}

    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["latest_version"] == "v1.5"
    assert benchmark["task_counts"]["total"] == 205
    assert benchmark["versions"][0]["label"] == "v1.0"
    assert benchmark["versions"][0]["task_counts"]["total"] == 296
    assert benchmark["versions"][0]["task_counts"]["subsets"][0]["count"] == 53
    assert benchmark["versions"][1]["task_counts"] == benchmark["task_counts"]
    assert subsets["bixbench-v1-5-referenced-capsules"] == 59
    assert subsets["bixbench-v1-5-readme-notebooks"] == 60
    assert subsets["bixbench-v1-5-release-archives"] == 64
    assert [subsets[key] for key in (
        "bixbench-v1-5-llm-verifier",
        "bixbench-v1-5-string-verifier",
        "bixbench-v1-5-range-verifier",
    )] == [83, 61, 61]
    assert {item["path"] for item in benchmark["field_status"]} == {
        "/task_counts/subsets/1/count",
        "/versions/1/task_counts/subsets/1/count",
    }
    assert {item["status"] for item in benchmark["field_status"]} == {"conflicted"}
    coverage = {item["tag"]: item for item in benchmark["coverage_notes"]}
    assert {tag: coverage[tag]["count"] for tag in (
        "genomics", "transcriptomics", "epigenomics", "single-cell", "proteomics", "multiomics",
    )} == {
        "genomics": 74, "transcriptomics": 69, "epigenomics": 12,
        "single-cell": 2, "proteomics": 4, "multiomics": 2,
    }
    assert all(
        coverage[tag]["count"] is None and coverage[tag]["reporting_status"] == "not_reported"
        for tag in ("protein-protein-binding", "protein-ligand-binding")
    )
    assert benchmark["access"]["level"] == "fully-open"
    assert {resource["license"] for resource in benchmark["resources"]} == {None, "Apache-2.0"}
    assert works["bixbench-paper"]["publication_date"] == "2025-02-28"
    assert works["bixbench-v1-5-release"]["work_type"] == "official-release"

    assert set(runs) == {
        "bixbench-creator-paper",
        "bixbench-paper-mcq-refusal",
        "bixbench-paper-mcq-no-refusal",
        "bixbench-paper-mcq-no-images",
        "bixbench-v1-5-zero-shot-open",
        "bixbench-v1-5-zero-shot-mcq-refusal",
        "bixbench-v1-5-zero-shot-mcq-no-refusal",
        "bixbench-v1-5-agentic-open-images",
        "bixbench-v1-5-agentic-mcq-refusal-images",
        "bixbench-v1-5-agentic-mcq-no-refusal-images",
        "bixbench-v1-5-agentic-mcq-refusal-no-images",
    }
    paper = runs["bixbench-creator-paper"]
    assert paper["benchmark_version"] == "v1.0"
    assert paper["scope"]["type"] == "full" and paper["scope"]["n"] == 296
    assert paper["protocol"]["repeats"]["value"] == 10
    assert paper["protocol"]["tools"]["code_execution"]["value"] is True
    assert paper["protocol"]["tools"]["container"]["value"] is True
    assert {item["model_id"]: item["value"] for item in paper["results"]} == {
        "bixbench-gpt-4o-unversioned": 9.0,
        "bixbench-claude-3-5-sonnet-unversioned": 17.0,
    }
    for run_id in (
        "bixbench-paper-mcq-refusal",
        "bixbench-paper-mcq-no-refusal",
        "bixbench-paper-mcq-no-images",
    ):
        assert runs[run_id]["results"] == []
        assert runs[run_id]["protocol"]["repeats"]["value"] == 10

    zero_open = runs["bixbench-v1-5-zero-shot-open"]
    assert zero_open["scope"]["type"] == "full" and zero_open["scope"]["n"] == 205
    assert zero_open["protocol"]["repeats"]["value"] == 1
    assert all(setting["value"] is False for setting in zero_open["protocol"]["tools"].values())
    assert {
        (item["model_id"], item["metric_id"]): item["value"] for item in zero_open["results"]
    }[("bixbench-gpt-4o-unversioned", "accuracy")] == 2.9268292683
    zero_forced = runs["bixbench-v1-5-zero-shot-mcq-no-refusal"]
    assert {
        item["model_id"]: item["value"]
        for item in zero_forced["results"] if item["metric_id"] == "accuracy"
    } == {
        "bixbench-gpt-4o-unversioned": 36.0975609756,
        "bixbench-claude-3-5-sonnet-unversioned": 34.1463414634,
    }
    agentic = [run for run_id, run in runs.items() if "v1-5-agentic" in run_id]
    assert all(run["scope"]["n"] == 205 for run in agentic)
    assert all(run["protocol"]["repeats"]["value"] == 5 for run in agentic)
    assert all(run["protocol"]["reasoning"]["value"].startswith("SimpleAgent") for run in agentic)
    assert all(run["results"] == [] for run in agentic)
    assert models["bixbench-gpt-4o-unversioned"]["version_status"] == "not_reported"
    assert models["bixbench-claude-3-5-sonnet-unversioned"]["version_status"] == "not_reported"
    assert models["bixbench-claude-3-5-sonnet-20241022"]["version_string"].endswith("20241022")
    all_results = [result for run in runs.values() for result in run["results"]]
    assert {result["status"] for result in all_results} == {"verified"}
    assert {result["confidence"] for result in all_results} == {"high"}


def test_blade_separates_source_tasks_decisions_mcqs_and_agent_protocols() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"] in {"blade-mcq", "blade-analysis-generation"}
    }
    models = {item["id"]: item for item in entities["model"]}
    work = next(item for item in entities["work"] if item["id"] == "blade-paper")

    root = benchmarks["blade"]
    root_counts = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}
    assert root["kind"] == "suite"
    assert root["audit"]["status"] == "audited"
    assert root["latest_version"] == "arXiv v3"
    assert root["task_counts"]["total"] == 12
    assert root_counts == {
        "blade-mcq-items": 188,
        "blade-mcq-conceptual": 20,
        "blade-mcq-transform": 168,
        "blade-ground-truth-decisions": 536,
        "blade-ground-truth-conceptual": 118,
        "blade-ground-truth-transform": 246,
        "blade-ground-truth-modeling": 172,
    }
    assert root["versions"][0]["task_counts"] == root["task_counts"]
    assert set(root["versions"][0]["formal_tracks"]) == {
        "blade-mcq", "blade-analysis-generation",
    }
    coverage = {item["tag"]: item for item in root["coverage_notes"]}
    assert coverage["life-science"]["count"] == 4
    assert coverage["protein-protein-binding"]["count"] == 0
    assert coverage["protein-ligand-binding"]["count"] == 0

    mcq_track = benchmarks["blade-mcq"]
    generation_track = benchmarks["blade-analysis-generation"]
    assert mcq_track["parent_id"] == "blade" and mcq_track["task_counts"]["total"] == 188
    assert {item["id"]: item["count"] for item in mcq_track["task_counts"]["subsets"]} == {
        "blade-mcq-conceptual-items": 20,
        "blade-mcq-transform-items": 168,
        "blade-mcq-source-datasets": 11,
    }
    assert generation_track["parent_id"] == "blade"
    assert generation_track["task_counts"]["total"] == 12
    assert next(
        item["count"] for item in generation_track["task_counts"]["subsets"]
        if item["id"] == "blade-generation-ground-truth-decisions"
    ) == 536

    assert set(runs) == {"blade-creator-paper", "blade-creator-react", "blade-creator-decision-mcq"}
    direct = runs["blade-creator-paper"]
    react = runs["blade-creator-react"]
    mcq = runs["blade-creator-decision-mcq"]
    assert direct["benchmark_version"] == "arXiv v3"
    assert direct["scope"]["n"] == 12
    assert direct["protocol"]["shots"]["value"] == "one-shot"
    assert direct["protocol"]["turns"]["value"] == "single-turn"
    assert direct["protocol"]["tools"]["code_execution"]["value"] is False
    assert direct["protocol"]["temperature"]["value"] == 0.8
    assert direct["protocol"]["repeats"]["value"] == 40
    assert len(direct["results"]) == 9
    assert {(item["model_id"], item["value"], item["ci_low"], item["ci_high"]) for item in direct["results"]} >= {
        ("blade-codellama-7b-instruct", 16.8, 15.2, 18.5),
        ("blade-claude-3-5-sonnet-20240620", 43.9, 42.6, 44.9),
    }

    assert react["scope"]["n"] == 12
    assert react["protocol"]["turns"]["value"] == "multi-turn"
    assert react["protocol"]["tools"]["code_execution"]["value"] is True
    assert react["protocol"]["time_budget"]["value"] == "maximum 10 agent steps"
    assert react["protocol"]["repeats"]["value"] == 20
    assert {item["model_id"]: item["value"] for item in react["results"]} == {
        "blade-mixtral-8x22b-unversioned": 40.8,
        "blade-gpt35-turbo-unversioned": 37.2,
        "blade-gpt4o-unversioned": 44.8,
        "blade-gemini-1-5-pro-unversioned": 40.1,
        "blade-claude-3-5-sonnet-20240620": 43.1,
    }
    assert mcq["scope"]["n"] == 188
    assert mcq["protocol"]["temperature"]["value"] == 0
    assert mcq["protocol"]["repeats"]["value"] == 1
    assert mcq["results"] == []

    assert models["blade-codellama-7b-instruct"]["version_status"] == "reported"
    assert models["blade-gpt4o-unversioned"]["version_status"] == "not_reported"
    assert models["blade-claude-3-5-sonnet-20240620"]["version_string"] == "claude-3-5-sonnet-20240620"
    assert set(work["organizations"]) == {
        "University of Washington", "UC Berkeley", "New York University", "Stanford University",
        "University of British Columbia", "Microsoft", "George Washington University",
    }


def test_scigym_separates_released_splits_and_creator_protocols() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {
        item["id"]: item
        for item in entities["evaluation_run"]
        if item["benchmark_id"] in {"scigym-small", "scigym-large"}
    }
    models = {item["id"]: item for item in entities["model"]}
    work = next(item for item in entities["work"] if item["id"] == "scigym-paper")

    root = benchmarks["scigym"]
    counts = {item["id"]: item["count"] for item in root["task_counts"]["subsets"]}
    assert root["kind"] == "suite"
    assert root["audit"]["status"] == "audited"
    assert root["release_date"] == "2025-05-16"
    assert root["latest_version"] == "2025 release"
    assert root["task_counts"]["total"] == 350
    assert counts == {"scigym-small-systems": 137, "scigym-large-systems": 213}
    assert root["versions"][0]["task_counts"] == root["task_counts"]
    assert root["versions"][0]["formal_tracks"] == ["scigym-small", "scigym-large"]
    assert root["access"]["license"] is None
    assert "wet-lab-output" not in root["modalities"]
    coverage = {item["tag"]: item for item in root["coverage_notes"]}
    assert coverage["protein-design"]["count"] == 0
    assert coverage["protein-protein-binding"]["count"] == 0
    assert coverage["protein-ligand-binding"]["count"] == 0

    small = benchmarks["scigym-small"]
    large = benchmarks["scigym-large"]
    assert small["parent_id"] == "scigym" and small["task_counts"]["total"] == 137
    assert large["parent_id"] == "scigym" and large["task_counts"]["total"] == 213
    assert all(run["benchmark_id"] != "scigym-large" for run in entities["evaluation_run"])

    assert set(runs) == {"scigym-small-creator-paper", "scigym-small-zero-shot"}
    main = runs["scigym-small-creator-paper"]
    assert main["benchmark_version"] == "2025 release"
    assert main["scope"] == {
        "type": "full", "n": 137, "subset_id": None,
        "filter": "All 137 systems in the official small split, each having fewer than ten reactions.",
        "selection": None, "reporting_status": "reported",
    }
    assert main["protocol"]["turns"]["value"] == "multi-turn"
    assert main["protocol"]["tools"]["code_execution"]["value"] is True
    assert main["protocol"]["tools"]["internet"]["value"] is False
    assert main["protocol"]["tools"]["container"]["value"] is None
    assert main["protocol"]["tools"]["container"]["reporting_status"] == "not_reported"
    assert main["protocol"]["time_budget"]["value"] == (
        "maximum 20 action iterations plus up to 3 invalid-submission debugging iterations"
    )
    assert main["protocol"]["temperature"]["value"] is None
    assert main["protocol"]["temperature"]["reporting_status"] == "not_reported"
    assert main["protocol"]["repeats"]["value"] == 3
    assert len(main["model_ids"]) == 6
    assert {item["metric_id"] for item in main["metrics"]} == {
        "network-topology-f1",
        "simulation-trajectory-error",
        "reaction-matching-with-modifiers-precision",
        "reaction-matching-with-modifiers-recall",
        "reaction-matching-with-modifiers-f1",
        "reaction-matching-without-modifiers-precision",
        "reaction-matching-without-modifiers-recall",
        "reaction-matching-without-modifiers-f1",
    }
    assert len(main["results"]) == 42
    assert {item["status"] for item in main["results"]} == {"verified"}
    assert {item["confidence"] for item in main["results"]} == {"high"}
    values = {
        (item["model_id"], item["metric_id"]): item["value"]
        for item in main["results"]
    }
    assert values[("scigym-gemini-2-5-pro-preview-03-25", "simulation-trajectory-error")] == 0.3212
    assert values[("scigym-claude-3-7-sonnet-20250219", "reaction-matching-without-modifiers-f1")] == 0.3047

    zero_shot = runs["scigym-small-zero-shot"]
    assert zero_shot["scope"]["type"] == "full" and zero_shot["scope"]["n"] == 137
    assert zero_shot["protocol"]["shots"]["value"] == "zero-shot"
    assert all(
        zero_shot["protocol"]["tools"][tool]["value"] is False
        for tool in ("browser", "internet", "databases", "code_execution", "container")
    )
    assert zero_shot["protocol"]["tools"]["external_tools"]["value"] == "none"
    assert zero_shot["results"] == []

    assert work["status"] == "published"
    assert work["publication_date"] == "2025-11-30"
    assert set(work["organizations"]) == {
        "University of Toronto", "SickKids", "Axiom", "Mila", "Vector Institute",
    }
    assert models["scigym-gpt-4-1-2025-04-14"]["version_string"] == "gpt-4.1-2025-04-14"
    assert models["scigym-claude-3-7-sonnet-20250219"]["version_string"] == "claude-3-7-sonnet-20250219"


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
    assert payload["meta"]["version"] == "1.4.0-dev"
    assert all("model_ids" in run for run in payload["evaluation_runs"])


def test_v11_exports_surface_audit_and_result_status_columns() -> None:
    subprocess.run([sys.executable, "scripts/build_registry.py"], cwd=ROOT, check=True)
    benchmark_header = (ROOT / "exports" / "benchmarks.csv").read_text(encoding="utf-8").splitlines()[0]
    result_header = (ROOT / "exports" / "evaluation-results.csv").read_text(encoding="utf-8").splitlines()[0]
    assert {"audit_status", "provisional_fields", "conflicted_fields"} <= set(benchmark_header.split(","))
    assert {"result_status", "confidence", "result_evidence_ids"} <= set(result_header.split(","))
    payload = json.loads((ROOT / "exports" / "registry.json").read_text(encoding="utf-8"))
    audit_statuses = {benchmark["audit"]["status"] for benchmark in payload["benchmarks"]}
    assert audit_statuses <= {"legacy", "audited", "audited-with-caveats"}
    legacy_ids = {
        benchmark["id"]
        for benchmark in payload["benchmarks"]
        if benchmark["audit"]["status"] == "legacy"
    }
    exemption_ids = {
        exemption["benchmark_id"] for exemption in payload["meta"]["audit_exemptions"]
    }
    assert legacy_ids == exemption_ids == {"virbench"}
    root_benchmarks = [benchmark for benchmark in payload["benchmarks"] if benchmark["parent_id"] is None]
    assert sum(benchmark["audit"]["status"] != "legacy" for benchmark in root_benchmarks) == 23


def test_unapproved_legacy_record_is_rejected(monkeypatch) -> None:
    meta = copy.deepcopy(load_meta())
    meta["audit_exemptions"] = []
    monkeypatch.setattr(validator_module, "load_meta", lambda: meta)
    try:
        validator_module.validate_registry()
    except RegistryValidationError as error:
        assert "legacy benchmarks require an explicit" in str(error)
        assert "virbench" in str(error)
    else:
        raise AssertionError("an undeclared legacy record unexpectedly passed validation")


def test_scientific_task_taxonomy_and_core_mapping_contracts() -> None:
    taxonomies = load_taxonomies()
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    task_terms = {item["id"]: item for item in taxonomies["scientific_tasks"]}

    assert len(taxonomies["scientific_objects"]) == 10
    assert len(taxonomies["task_families"]) == 11
    assert len(task_terms) >= 69
    assert "protein folding" in task_terms["protein-monomer-structure-prediction"]["aliases"]
    assert "PPI" in task_terms["protein-protein-interaction-prediction"]["aliases"]
    assert set(task_terms["protein-ligand-binding-prediction"]["object_ids"]) == {
        "protein", "small-molecule",
    }

    classifications = {
        benchmark_id: benchmark["scientific_task_classification"]
        for benchmark_id, benchmark in benchmarks.items()
    }
    assert {benchmark_id for benchmark_id, item in classifications.items() if item["status"] == "unclassified"} == {"virbench"}

    lifescience = {item["task_type_id"]: item for item in classifications["lifescibench"]["entries"]}
    assert lifescience["protein-design"]["count"] == 62
    assert lifescience["protein-design"]["count_unit"] == "tasks"
    assert lifescience["protein-protein-interaction-prediction"]["count"] is None
    assert lifescience["protein-ligand-binding-prediction"]["reporting_status"] == "not_reported"

    casp_tasks = {item["task_type_id"] for item in classifications["casp"]["entries"]}
    assert {
        "protein-monomer-structure-prediction", "protein-complex-structure-prediction",
        "protein-model-quality-assessment", "protein-ligand-pose-prediction",
        "protein-ligand-binding-affinity",
    } <= casp_tasks
    flip_tasks = {item["task_type_id"] for item in classifications["flip"]["entries"]}
    assert flip_tasks == {"protein-fitness-prediction", "protein-mutation-effect-prediction"}
    assert "protein-sequence-design" not in flip_tasks

    biology = {item["task_type_id"]: item for item in classifications["bioinstruction"]["entries"]}
    assert biology["protein-sequence-design"]["coverage"] == "not-in-scope"
    assert biology["protein-sequence-design"]["count"] == 0
    viral_ppi = {item["task_type_id"]: item for item in classifications["lab-bench-dbqa-viral-ppi"]["entries"]}
    assert viral_ppi["protein-protein-interaction-prediction"]["count"] == 50
    assert viral_ppi["protein-protein-interaction-prediction"]["count_unit"] == "questions"


def test_scientific_task_exports_are_normalized_and_preserve_units() -> None:
    subprocess.run([sys.executable, "scripts/build_registry.py"], cwd=ROOT, check=True)
    tasks = json.loads((ROOT / "exports" / "scientific-tasks.json").read_text(encoding="utf-8"))
    coverage = json.loads((ROOT / "exports" / "scientific-task-coverage.json").read_text(encoding="utf-8"))
    assert len(tasks) >= 69
    assert {item["root_family_id"] for item in coverage} == {
        "lifescibench", "proteingym", "casp", "cameo", "flip", "proteinlmbench",
        "bioinstruction", "lab-bench", "genebench-pro", "biomysterybench",
            "compbiobench", "bixbench", "blade", "scigym", "tape", "genomic-benchmarks",
            "beacon-rna", "moleculenet", "atom3d", "guacamol", "scib", "spatialbench",
            "anthropic-key-life-sciences-evals",
        }
    generation = next(item for item in tasks if item["id"] == "small-molecule-generation")
    assert generation["coverage_family_count"] == 1
    units = {
        item["count_unit"] for item in coverage
        if item["count"] is not None and item["coverage"] != "not-in-scope"
    }
    assert {"tasks", "questions", "examples", "assays", "targets", "systems", "problems"} <= units
    benchmark_header = (ROOT / "exports" / "benchmarks.csv").read_text(encoding="utf-8").splitlines()[0]
    coverage_header = (ROOT / "exports" / "scientific-task-coverage.csv").read_text(encoding="utf-8").splitlines()[0]
    assert {"scientific_task_ids", "task_classification_status"} <= set(benchmark_header.split(","))
    assert {"task_type_id", "root_family_id", "count_unit", "evidence_ids", "aggregate_eligible"} <= set(coverage_header.split(","))


def test_creator_audited_benchmark_expansion_contracts() -> None:
    entities = load_entities()
    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    runs = {item["id"]: item for item in entities["evaluation_run"]}
    expected_totals = {
        "tape": (5, "supervised downstream benchmark tasks"),
        "genomic-benchmarks": (9, "benchmark dataset classification tasks"),
        "beacon-rna": (13, "formal RNA benchmark tasks"),
        "moleculenet": (17, "original paper dataset collections"),
        "atom3d": (8, "curated 3D benchmark datasets"),
        "guacamol": (25, "formal benchmark problems"),
        "scib": (13, "atlas-level integration tasks"),
    }
    for benchmark_id, (total, basis) in expected_totals.items():
        benchmark = benchmarks[benchmark_id]
        assert benchmark["parent_id"] is None
        assert benchmark["audit"]["status"] == "audited"
        assert benchmark["verification"]["status"] == "verified"
        assert benchmark["task_counts"]["total"] == total
        assert benchmark["task_counts"]["basis"] == basis
        assert benchmark["scientific_task_classification"]["status"] in {"complete", "partial"}

    tape_tasks = {
        item["task_type_id"]: item["count"]
        for item in benchmarks["tape"]["scientific_task_classification"]["entries"]
    }
    assert tape_tasks == {
        "protein-secondary-structure-prediction": 1,
        "protein-contact-map-prediction": 1,
        "protein-remote-homology-detection": 1,
        "protein-fluorescence-prediction": 1,
        "protein-stability-prediction": 1,
    }
    assert "protein-monomer-structure-prediction" not in tape_tasks
    assert "protein-sequence-design" not in tape_tasks

    beacon_subsets = {
        item["id"]: item["count"] for item in benchmarks["beacon-rna"]["task_counts"]["subsets"]
    }
    assert beacon_subsets == {"beacon-structure": 4, "beacon-function": 5, "beacon-engineering": 4}
    assert runs["beacon-creator-full"]["protocol"]["repeats"]["value"] == 3

    genomic_tasks = {
        item["task_type_id"]: item["count"]
        for item in benchmarks["genomic-benchmarks"]["scientific_task_classification"]["entries"]
    }
    assert genomic_tasks == {
        "enhancer-activity-prediction": 4,
        "promoter-detection": 1,
        "epigenetic-mark-prediction": 1,
        "dna-sequence-analysis": 3,
    }

    molecule_net = benchmarks["moleculenet"]
    assert sum(item["count"] for item in molecule_net["task_counts"]["subsets"]) == 17
    assert "endpoint" not in molecule_net["task_counts"]["basis"]
    assert runs["moleculenet-creator-full"]["comparability_group"].endswith("task-native")
    assert runs["moleculenet-creator-full"]["results"] == []
    assert runs["moleculenet-creator-full"]["protocol"]["repeats"]["value"] == 3

    atom_tasks = benchmarks["atom3d"]["scientific_task_classification"]["entries"]
    assert len(atom_tasks) == 8
    assert sum(item["count"] for item in atom_tasks) == 8
    assert {item["task_type_id"] for item in atom_tasks} == {
        "small-molecule-property-prediction",
        "protein-protein-interface-prediction",
        "protein-residue-identity-prediction",
        "protein-complex-mutation-stability-prediction",
        "protein-ligand-binding-affinity",
        "protein-ligand-efficacy-prediction",
        "protein-model-quality-assessment",
        "rna-structure-quality-assessment",
    }
    assert runs["atom3d-creator-full"]["protocol"]["repeats"]["value"] == 3

    guacamol_subsets = {
        item["id"]: item["count"] for item in benchmarks["guacamol"]["task_counts"]["subsets"]
    }
    assert guacamol_subsets == {
        "guacamol-distribution-learning": 5,
        "guacamol-goal-directed-v2": 20,
    }
    guacamol_task = benchmarks["guacamol"]["scientific_task_classification"]["entries"][0]
    assert (guacamol_task["task_type_id"], guacamol_task["count_unit"]) == (
        "small-molecule-generation", "problems",
    )

    scib_subsets = {item["id"]: item["count"] for item in benchmarks["scib"]["task_counts"]["subsets"]}
    assert scib_subsets == {"scib-simulation": 2, "scib-scrna": 5, "scib-scatac": 6}
    scib_overall = next(
        item for item in runs["scib-creator-full"]["metrics"]
        if item["metric_id"] == "overall-integration-score"
    )
    assert scib_overall["higher_is_better"] is True
    assert scib_overall["aggregation"] == "0.6 bio-conservation plus 0.4 batch-removal"


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
    assert sum(item["source_type"] == "work" for item in sources) == sum(
        len(work["source_versions"]) for work in load_entities()["work"]
    )
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
        allowed_source_classes = {"benchmark_creator"}
        if benchmark["access"]["level"] == "private-internal":
            allowed_source_classes.add("official_model_provider")
        assert any(
            work_id in works and works[work_id]["source_class"] in allowed_source_classes
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
