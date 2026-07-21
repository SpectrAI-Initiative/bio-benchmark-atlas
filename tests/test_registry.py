from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from registry_io import load_entities  # noqa: E402
from validate_registry import validate_registry  # noqa: E402


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
    assert payload["meta"]["version"] == "1.0.0"


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
