from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DIR = ROOT / "registry"
SCHEMA_PATH = ROOT / "schema" / "registry.schema.json"
ENTITY_DIRS = {
    "benchmark": REGISTRY_DIR / "benchmarks",
    "work": REGISTRY_DIR / "works",
    "model": REGISTRY_DIR / "models",
    "evaluation_run": REGISTRY_DIR / "evaluations",
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return _json_safe(yaml.safe_load(handle))


def load_meta() -> dict[str, Any]:
    return load_yaml(REGISTRY_DIR / "meta.yaml")


def load_taxonomies() -> dict[str, list[dict[str, Any]]]:
    return load_yaml(REGISTRY_DIR / "taxonomies.yaml")


def load_changelog() -> list[dict[str, Any]]:
    return load_yaml(REGISTRY_DIR / "changelog.yaml")


def load_scientific_task_classifications() -> dict[str, dict[str, Any]]:
    directory = REGISTRY_DIR / "scientific_task_classifications"
    classifications: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.yaml")):
        payload = load_yaml(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("classifications"), dict):
            raise ValueError(f"{path}: expected a mapping with a classifications mapping")
        duplicates = sorted(set(classifications) & set(payload["classifications"]))
        if duplicates:
            raise ValueError(f"{path}: duplicate benchmark classifications: {', '.join(duplicates)}")
        classifications.update(payload["classifications"])
    if not classifications:
        raise ValueError(f"{directory}: no Scientific Task classification files found")
    return classifications


def load_entities() -> dict[str, list[dict[str, Any]]]:
    loaded: dict[str, list[dict[str, Any]]] = {key: [] for key in ENTITY_DIRS}
    for expected_type, directory in ENTITY_DIRS.items():
        for path in sorted(directory.glob("*.yaml")):
            entity = load_yaml(path)
            if not isinstance(entity, dict):
                raise ValueError(f"{path}: entity must be a mapping")
            entity["_source_file"] = str(path.relative_to(ROOT))
            if entity.get("entity_type") != expected_type:
                raise ValueError(
                    f"{path}: expected entity_type={expected_type!r}, "
                    f"found {entity.get('entity_type')!r}"
                )
            loaded[expected_type].append(entity)

    classifications = load_scientific_task_classifications()
    benchmarks = {item["id"]: item for item in loaded["benchmark"]}
    unknown_ids = sorted(set(classifications) - set(benchmarks))
    if unknown_ids:
        raise ValueError(
            "registry/scientific_task_classifications/ references missing benchmarks: "
            + ", ".join(unknown_ids)
        )
    for benchmark in loaded["benchmark"]:
        benchmark["scientific_task_classification"] = classifications.get(
            benchmark["id"],
            {
                "status": "unclassified",
                "benchmark_version": benchmark["latest_version"],
                "as_of": None,
                "notes": "No evidence-backed scientific-task mapping is published for this record yet.",
                "entries": [],
            },
        )
        evidence_by_id = {
            item.get("id"): item for item in benchmark.get("evidence", []) if item.get("id")
        }
        for index, entry in enumerate(benchmark["scientific_task_classification"]["entries"]):
            support_path = f"/scientific_task_classification/entries/{index}"
            for evidence_id in entry["evidence_ids"]:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is not None and support_path not in evidence["supports"]:
                    evidence["supports"].append(support_path)
    return loaded


def without_internal_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_internal_fields(item)
            for key, item in value.items()
            if not key.startswith("_")
        }
    if isinstance(value, list):
        return [without_internal_fields(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        without_internal_fields(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(payload + "\n", encoding="utf-8")


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
