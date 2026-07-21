from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from registry_io import ROOT, SCHEMA_PATH, load_entities, load_meta, load_taxonomies, without_internal_fields


class RegistryValidationError(Exception):
    pass


def _collect_taxonomy_ids(taxonomies: dict[str, list[dict[str, Any]]]) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = {}
    for axis, terms in taxonomies.items():
        axis_ids = [term["id"] for term in terms]
        if len(axis_ids) != len(set(axis_ids)):
            raise RegistryValidationError(f"taxonomy axis {axis!r} contains duplicate IDs")
        ids[axis] = set(axis_ids)
        for term in terms:
            parent = term.get("parent_id")
            if parent is not None and parent not in ids[axis]:
                # Parent may appear later; checked again after collection.
                continue
        for term in terms:
            parent = term.get("parent_id")
            if parent is not None and parent not in ids[axis]:
                raise RegistryValidationError(
                    f"taxonomy {axis}/{term['id']} references missing parent {parent}"
                )
    return ids


def _check_parent_cycles(benchmarks: dict[str, dict[str, Any]]) -> None:
    for benchmark_id in benchmarks:
        seen: set[str] = set()
        current: str | None = benchmark_id
        while current is not None:
            if current in seen:
                raise RegistryValidationError(f"benchmark parent cycle includes {current}")
            seen.add(current)
            current = benchmarks.get(current, {}).get("parent_id")


def validate_registry() -> dict[str, list[dict[str, Any]]]:
    meta = load_meta()
    taxonomies = load_taxonomies()
    taxonomy_ids = _collect_taxonomy_ids(taxonomies)
    entities = load_entities()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    schema_errors: list[str] = []
    for entity_type, records in entities.items():
        for entity in records:
            source = entity["_source_file"]
            for error in sorted(
                validator.iter_errors(without_internal_fields(entity)),
                key=lambda item: list(item.absolute_path),
            ):
                location = ".".join(str(part) for part in error.absolute_path) or "<root>"
                schema_errors.append(f"{source}:{location}: {error.message}")
    if schema_errors:
        raise RegistryValidationError("Schema validation failed:\n" + "\n".join(schema_errors))

    by_type: dict[str, dict[str, dict[str, Any]]] = {}
    global_ids: dict[str, str] = {}
    for entity_type, records in entities.items():
        by_type[entity_type] = {}
        for entity in records:
            entity_id = entity["id"]
            if entity_id in global_ids:
                raise RegistryValidationError(
                    f"duplicate global ID {entity_id!r} in {entity['_source_file']} and {global_ids[entity_id]}"
                )
            global_ids[entity_id] = entity["_source_file"]
            by_type[entity_type][entity_id] = entity

    aliases: dict[str, str] = {}
    for benchmark in entities["benchmark"]:
        for alias in benchmark["aliases"]:
            normalized = alias.lower().replace("_", "-").replace(" ", "-")
            if normalized in aliases or normalized in by_type["benchmark"]:
                raise RegistryValidationError(f"duplicate benchmark alias {alias!r}")
            aliases[normalized] = benchmark["id"]

        parent = benchmark["parent_id"]
        if parent is not None and parent not in by_type["benchmark"]:
            raise RegistryValidationError(f"{benchmark['id']}: missing parent benchmark {parent}")
        for tag in benchmark["domains"]:
            if tag not in taxonomy_ids["domains"]:
                raise RegistryValidationError(f"{benchmark['id']}: unknown domain {tag}")
        for tag in benchmark["capabilities"]:
            if tag not in taxonomy_ids["capabilities"]:
                raise RegistryValidationError(f"{benchmark['id']}: unknown capability {tag}")
        for tag in benchmark["modalities"]:
            if tag not in taxonomy_ids["modalities"]:
                raise RegistryValidationError(f"{benchmark['id']}: unknown modality {tag}")
        if benchmark["access"]["level"] not in taxonomy_ids["access_levels"]:
            raise RegistryValidationError(
                f"{benchmark['id']}: unknown access level {benchmark['access']['level']}"
            )
        for note in benchmark["coverage_notes"]:
            if note["tag"] not in taxonomy_ids["domains"]:
                raise RegistryValidationError(
                    f"{benchmark['id']}: coverage note has unknown domain {note['tag']}"
                )

        counts = benchmark["task_counts"]
        total = counts["total"]
        if total is None and counts.get("reporting_status") != "not_reported":
            raise RegistryValidationError(f"{benchmark['id']}: null total must be not_reported")
        exhaustive_counts = [
            subset["count"]
            for subset in counts["subsets"]
            if subset["exclusive"] and subset["exhaustive"] and subset["count"] is not None
        ]
        exhaustive_members = [
            subset
            for subset in counts["subsets"]
            if subset["exclusive"] and subset["exhaustive"]
        ]
        if exhaustive_members and len(exhaustive_counts) == len(exhaustive_members) and total is not None:
            if sum(exhaustive_counts) != total:
                raise RegistryValidationError(
                    f"{benchmark['id']}: exhaustive subset counts sum to {sum(exhaustive_counts)}, not {total}"
                )
        for note in benchmark["coverage_notes"]:
            if note["count"] is None and note["reporting_status"] != "not_reported":
                raise RegistryValidationError(
                    f"{benchmark['id']}: null coverage count must be not_reported"
                )

    _check_parent_cycles(by_type["benchmark"])

    published_classes = set(meta["published_source_classes"])
    for work in entities["work"]:
        if work["verification"]["status"] == "verified" and work["source_class"] not in published_classes:
            raise RegistryValidationError(
                f"{work['id']}: source class {work['source_class']} is not publishable in v1"
            )

    evidence_users: defaultdict[str, list[str]] = defaultdict(list)
    for entity_type in ("benchmark", "evaluation_run"):
        for entity in entities[entity_type]:
            for evidence in entity["evidence"]:
                if evidence["work_id"] not in by_type["work"]:
                    raise RegistryValidationError(
                        f"{entity['id']}: evidence references missing work {evidence['work_id']}"
                    )
                evidence_users[entity["id"]].extend(evidence["supports"])

    comparability: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in entities["evaluation_run"]:
        if run["work_id"] not in by_type["work"]:
            raise RegistryValidationError(f"{run['id']}: missing work {run['work_id']}")
        if run["benchmark_id"] not in by_type["benchmark"]:
            raise RegistryValidationError(f"{run['id']}: missing benchmark {run['benchmark_id']}")
        metric_ids = {metric["metric_id"] for metric in run["metrics"]}
        if len(metric_ids) != len(run["metrics"]):
            raise RegistryValidationError(f"{run['id']}: duplicate metric IDs")
        for result in run["results"]:
            if result["model_id"] not in by_type["model"]:
                raise RegistryValidationError(f"{run['id']}: missing model {result['model_id']}")
            if result["metric_id"] not in metric_ids:
                raise RegistryValidationError(
                    f"{run['id']}: result uses undeclared metric {result['metric_id']}"
                )
            metric = next(item for item in run["metrics"] if item["metric_id"] == result["metric_id"])
            if metric["range"] is not None and not (metric["range"][0] <= result["value"] <= metric["range"][1]):
                raise RegistryValidationError(
                    f"{run['id']}: result {result['value']} falls outside {metric['metric_id']} range {metric['range']}"
                )
            if result["ci_low"] is not None and result["ci_high"] is not None:
                if not result["ci_low"] <= result["value"] <= result["ci_high"]:
                    raise RegistryValidationError(f"{run['id']}: confidence interval does not contain result value")

        for field_name in ("shots", "turns", "system_prompt_public", "reasoning", "token_budget", "time_budget", "temperature", "seed", "repeats", "statistical", "contamination"):
            reported_value = run["protocol"][field_name]
            if reported_value["value"] is None and reported_value["reporting_status"] == "reported":
                raise RegistryValidationError(f"{run['id']}: protocol.{field_name} reports a null value")
        for field_name, reported_value in run["protocol"]["tools"].items():
            if reported_value["value"] is None and reported_value["reporting_status"] == "reported":
                raise RegistryValidationError(f"{run['id']}: protocol.tools.{field_name} reports a null value")

        scope = run["scope"]
        benchmark = by_type["benchmark"][run["benchmark_id"]]
        benchmark_total = benchmark["task_counts"]["total"]
        if scope["type"] == "full":
            if scope["n"] is None or scope["reporting_status"] != "reported":
                raise RegistryValidationError(f"{run['id']}: full scope requires reported n")
            if benchmark_total is not None and scope["n"] != benchmark_total:
                raise RegistryValidationError(
                    f"{run['id']}: full scope n={scope['n']} does not equal total={benchmark_total}"
                )
        if scope["n"] is not None and benchmark_total is not None and scope["n"] > benchmark_total:
            raise RegistryValidationError(f"{run['id']}: scope n exceeds benchmark total")
        subset_ids = {item["id"] for item in benchmark["task_counts"]["subsets"]}
        if scope["subset_id"] is not None and scope["subset_id"] not in subset_ids:
            raise RegistryValidationError(f"{run['id']}: scope references missing subset {scope['subset_id']}")
        if scope["type"] == "subset" and (scope["subset_id"] is None or scope["n"] is None):
            raise RegistryValidationError(f"{run['id']}: subset scope requires subset_id and realized n")

        supports = {path for evidence in run["evidence"] for path in evidence["supports"]}
        if not any(path == "scope" or path.startswith("scope.") for path in supports):
            raise RegistryValidationError(f"{run['id']}: scope lacks evidence support")
        if not any(path == "metrics" or path.startswith("metrics.") for path in supports):
            raise RegistryValidationError(f"{run['id']}: metrics lack evidence support")
        if run["results"] and not any(path == "results" or path.startswith("results.") for path in supports):
            raise RegistryValidationError(f"{run['id']}: public results lack evidence support")
        comparability[run["comparability_group"]].append(run)

    for group, runs in comparability.items():
        signatures = {
            (
                run["benchmark_id"],
                run["benchmark_version"],
                run["scope"]["type"],
                run["scope"]["subset_id"],
                tuple(metric["metric_id"] for metric in run["metrics"]),
                json.dumps(
                    {
                        "shots": run["protocol"]["shots"],
                        "turns": run["protocol"]["turns"],
                        "reasoning": run["protocol"]["reasoning"],
                        "tools": run["protocol"]["tools"],
                        "token_budget": run["protocol"]["token_budget"],
                        "time_budget": run["protocol"]["time_budget"],
                        "repeats": run["protocol"]["repeats"],
                        "grader": run["protocol"]["grader"],
                    },
                    sort_keys=True,
                ),
            )
            for run in runs
        }
        if len(signatures) > 1:
            raise RegistryValidationError(
                f"comparability group {group!r} mixes incompatible benchmark/version/scope/metrics"
            )

    return entities


def main() -> int:
    try:
        entities = validate_registry()
    except (RegistryValidationError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    counts = ", ".join(f"{kind}={len(records)}" for kind, records in entities.items())
    print(f"Registry validation passed ({counts}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
