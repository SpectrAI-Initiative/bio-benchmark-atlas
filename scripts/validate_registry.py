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


AUDITED_STATUSES = {"audited", "audited-with-caveats"}
BENCHMARK_CRITICAL_PATHS = {
    "/name",
    "/organizations",
    "/release_date",
    "/latest_version",
    "/kind",
    "/domains",
    "/capabilities",
    "/modalities",
    "/task_counts/total",
    "/task_counts/basis",
    "/task_counts/subsets",
    "/access/level",
    "/access/license",
    "/resources",
}


def _resolve_pointer(document: Any, pointer: str) -> bool:
    """Return whether an RFC 6901-style path (plus `*`) resolves."""
    if not pointer.startswith("/"):
        return False
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]

    def walk(value: Any, remaining: list[str]) -> bool:
        if not remaining:
            return True
        part, *tail = remaining
        if part == "*":
            if isinstance(value, dict):
                return bool(value) and all(walk(item, tail) for item in value.values())
            if isinstance(value, list):
                return bool(value) and all(walk(item, tail) for item in value)
            return False
        if isinstance(value, dict):
            return part in value and walk(value[part], tail)
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                return False
            return 0 <= index < len(value) and walk(value[index], tail)
        return False

    return walk(document, parts)


def _is_structured_evidence(evidence: dict[str, Any]) -> bool:
    return all(key in evidence for key in ("id", "source_type", "source_id", "accessed_date"))


def _supports_path(evidence: list[dict[str, Any]], path: str) -> bool:
    return any(path in item["supports"] for item in evidence)


def _field_is_flagged(field_status: list[dict[str, Any]], path: str) -> bool:
    return any(item["path"] == path for item in field_status)


def _validate_counts(owner_id: str, counts: dict[str, Any]) -> None:
    total = counts["total"]
    if total is None and counts.get("reporting_status") != "not_reported":
        raise RegistryValidationError(f"{owner_id}: null total must be not_reported")
    exhaustive_members = [
        subset for subset in counts["subsets"] if subset["exclusive"] and subset["exhaustive"]
    ]
    exhaustive_counts = [subset["count"] for subset in exhaustive_members if subset["count"] is not None]
    if exhaustive_members and len(exhaustive_counts) == len(exhaustive_members) and total is not None:
        if sum(exhaustive_counts) != total:
            raise RegistryValidationError(
                f"{owner_id}: exhaustive subset counts sum to {sum(exhaustive_counts)}, not {total}"
            )


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

    resource_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    version_ids: dict[str, str] = {}
    evidence_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    permanent_ids = set(global_ids)
    for benchmark in entities["benchmark"]:
        for resource in benchmark["resources"]:
            resource_id = resource.get("id")
            if resource_id:
                if resource_id in permanent_ids:
                    raise RegistryValidationError(f"duplicate permanent ID {resource_id!r}")
                permanent_ids.add(resource_id)
                resource_by_id[resource_id] = (benchmark["id"], resource)
        for version in benchmark.get("versions", []):
            if version["id"] in permanent_ids:
                raise RegistryValidationError(f"duplicate permanent ID {version['id']!r}")
            permanent_ids.add(version["id"])
            version_ids[version["id"]] = benchmark["id"]
    for entity_type in ("benchmark", "evaluation_run"):
        for entity in entities[entity_type]:
            for evidence in entity["evidence"]:
                evidence_id = evidence.get("id")
                if evidence_id:
                    if evidence_id in permanent_ids:
                        raise RegistryValidationError(f"duplicate permanent ID {evidence_id!r}")
                    permanent_ids.add(evidence_id)
                    evidence_by_id[evidence_id] = (entity["id"], evidence)

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
        _validate_counts(benchmark["id"], counts)
        for note in benchmark["coverage_notes"]:
            if note["count"] is None and note["reporting_status"] != "not_reported":
                raise RegistryValidationError(
                    f"{benchmark['id']}: null coverage count must be not_reported"
                )

        audit = benchmark.get("audit", {"status": "legacy"})
        field_status = benchmark.get("field_status", [])
        if audit["status"] in AUDITED_STATUSES:
            if audit["audited_date"] is None:
                raise RegistryValidationError(f"{benchmark['id']}: audited record requires audited_date")
            if audit["unresolved_fields"] != len(field_status):
                raise RegistryValidationError(
                    f"{benchmark['id']}: unresolved_fields must equal the number of field_status entries"
                )
            if audit["status"] == "audited" and field_status:
                raise RegistryValidationError(f"{benchmark['id']}: audited record cannot have unresolved fields")
            if audit["status"] == "audited-with-caveats" and not field_status:
                raise RegistryValidationError(
                    f"{benchmark['id']}: audited-with-caveats requires at least one field_status entry"
                )
            versions = benchmark.get("versions", [])
            latest = [
                version
                for version in versions
                if version["label"] == benchmark["latest_version"]
                and version["status"] in {"current", "rolling"}
            ]
            if len(latest) != 1:
                raise RegistryValidationError(
                    f"{benchmark['id']}: latest_version must match exactly one current/rolling version label"
                )
            current_versions = [version for version in versions if version["status"] in {"current", "rolling"}]
            if len(current_versions) != 1:
                raise RegistryValidationError(
                    f"{benchmark['id']}: audited record requires exactly one current/rolling version"
                )
            for version in versions:
                _validate_counts(version["id"], version["task_counts"])
                for evidence_id in version["evidence_ids"]:
                    if evidence_id not in evidence_by_id:
                        raise RegistryValidationError(
                            f"{benchmark['id']}: version references missing evidence {evidence_id}"
                        )
                    if evidence_by_id[evidence_id][0] != benchmark["id"]:
                        raise RegistryValidationError(
                            f"{benchmark['id']}: version evidence {evidence_id} belongs to another entity"
                        )
                for track_id in version["formal_tracks"]:
                    track = by_type["benchmark"].get(track_id)
                    if track is None or track.get("parent_id") != benchmark["id"]:
                        raise RegistryValidationError(
                            f"{benchmark['id']}: formal track {track_id} is not a registered child"
                        )
            if benchmark["parent_id"] is None and latest[0]["task_counts"] != counts:
                raise RegistryValidationError(
                    f"{benchmark['id']}: root task_counts must match the latest version snapshot"
                )
            for resource in benchmark["resources"]:
                if "id" not in resource or "last_checked" not in resource:
                    raise RegistryValidationError(
                        f"{benchmark['id']}: audited resources require id and last_checked"
                    )
                if resource["type"] in {"repository", "dataset"} and not resource.get("pin"):
                    raise RegistryValidationError(
                        f"{benchmark['id']}: audited {resource['type']} resource requires a pin"
                    )
            for status in field_status:
                if not _resolve_pointer(without_internal_fields(benchmark), status["path"]):
                    raise RegistryValidationError(
                        f"{benchmark['id']}: field_status path does not resolve: {status['path']}"
                    )
                for evidence_id in status["evidence_ids"]:
                    if evidence_id not in evidence_by_id:
                        raise RegistryValidationError(
                            f"{benchmark['id']}: field_status references missing evidence {evidence_id}"
                        )
                    owner_id, evidence = evidence_by_id[evidence_id]
                    if owner_id != benchmark["id"] or status["path"] not in evidence["supports"]:
                        raise RegistryValidationError(
                            f"{benchmark['id']}: field_status evidence {evidence_id} must support {status['path']}"
                        )
            for path in BENCHMARK_CRITICAL_PATHS:
                if not _supports_path(benchmark["evidence"], path) and not _field_is_flagged(field_status, path):
                    raise RegistryValidationError(
                        f"{benchmark['id']}: audited critical field {path} lacks evidence or field_status"
                    )
        elif audit.get("audited_date") is not None:
            raise RegistryValidationError(f"{benchmark['id']}: legacy audit must not have audited_date")

    _check_parent_cycles(by_type["benchmark"])

    published_classes = set(meta["published_source_classes"])
    for work in entities["work"]:
        if work["verification"]["status"] == "verified" and work["source_class"] not in published_classes:
            raise RegistryValidationError(
                f"{work['id']}: source class {work['source_class']} is not publishable in v1"
            )

    for entity_type in ("benchmark", "evaluation_run"):
        for entity in entities[entity_type]:
            audited = (
                entity_type == "benchmark"
                and entity.get("audit", {}).get("status") in AUDITED_STATUSES
            ) or (
                entity_type == "evaluation_run"
                and by_type["benchmark"][entity["benchmark_id"]].get("audit", {}).get("status")
                in AUDITED_STATUSES
            )
            for evidence in entity["evidence"]:
                if _is_structured_evidence(evidence):
                    source_type = evidence["source_type"]
                    source_id = evidence["source_id"]
                    if source_type == "work" and source_id not in by_type["work"]:
                        raise RegistryValidationError(
                            f"{entity['id']}: evidence references missing work {source_id}"
                        )
                    if source_type == "resource" and source_id not in resource_by_id:
                        raise RegistryValidationError(
                            f"{entity['id']}: evidence references missing resource {source_id}"
                        )
                    if source_type == "resource":
                        expected_benchmark = entity["id"] if entity_type == "benchmark" else entity["benchmark_id"]
                        if resource_by_id[source_id][0] != expected_benchmark:
                            raise RegistryValidationError(
                                f"{entity['id']}: evidence resource {source_id} belongs to another benchmark"
                            )
                    for path in evidence["supports"]:
                        if not _resolve_pointer(without_internal_fields(entity), path):
                            raise RegistryValidationError(
                                f"{entity['id']}: evidence path does not resolve: {path}"
                            )
                elif audited:
                    raise RegistryValidationError(
                        f"{entity['id']}: audited records require structured evidence"
                    )
                else:
                    work_id = evidence.get("work_id")
                    if work_id not in by_type["work"]:
                        raise RegistryValidationError(
                            f"{entity['id']}: evidence references missing work {work_id}"
                        )

    comparability: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in entities["evaluation_run"]:
        if run["work_id"] not in by_type["work"]:
            raise RegistryValidationError(f"{run['id']}: missing work {run['work_id']}")
        if run["benchmark_id"] not in by_type["benchmark"]:
            raise RegistryValidationError(f"{run['id']}: missing benchmark {run['benchmark_id']}")
        metric_ids = {metric["metric_id"] for metric in run["metrics"]}
        if len(metric_ids) != len(run["metrics"]):
            raise RegistryValidationError(f"{run['id']}: duplicate metric IDs")
        benchmark = by_type["benchmark"][run["benchmark_id"]]
        benchmark_audited = benchmark.get("audit", {}).get("status") in AUDITED_STATUSES
        for model_id in run.get("model_ids", []):
            if model_id not in by_type["model"]:
                raise RegistryValidationError(f"{run['id']}: missing evaluated model {model_id}")
        run_evidence_ids = {item["id"] for item in run["evidence"] if item.get("id")}
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
            if benchmark_audited:
                if not all(key in result for key in ("status", "confidence", "evidence_ids")):
                    raise RegistryValidationError(
                        f"{run['id']}: audited result requires status, confidence, and evidence_ids"
                    )
                missing_result_evidence = set(result["evidence_ids"]) - run_evidence_ids
                if missing_result_evidence:
                    raise RegistryValidationError(
                        f"{run['id']}: result references missing run evidence {sorted(missing_result_evidence)}"
                    )
                if result["status"] == "verified" and result["confidence"] == "low":
                    raise RegistryValidationError(
                        f"{run['id']}: a verified result cannot have low confidence"
                    )

        for field_name in ("shots", "turns", "system_prompt_public", "reasoning", "token_budget", "time_budget", "temperature", "seed", "repeats", "statistical", "contamination"):
            reported_value = run["protocol"][field_name]
            if reported_value["value"] is None and reported_value["reporting_status"] == "reported":
                raise RegistryValidationError(f"{run['id']}: protocol.{field_name} reports a null value")
        for field_name, reported_value in run["protocol"]["tools"].items():
            if reported_value["value"] is None and reported_value["reporting_status"] == "reported":
                raise RegistryValidationError(f"{run['id']}: protocol.tools.{field_name} reports a null value")

        scope = run["scope"]
        run_version = None
        version_index = None
        if benchmark_audited and run["benchmark_version"] is not None:
            matches = [
                (index, version)
                for index, version in enumerate(benchmark.get("versions", []))
                if version["label"] == run["benchmark_version"]
            ]
            if len(matches) != 1:
                raise RegistryValidationError(
                    f"{run['id']}: benchmark_version must match exactly one registered version label"
                )
            version_index, run_version = matches[0]
        benchmark_total = (
            run_version["task_counts"]["total"] if run_version is not None else benchmark["task_counts"]["total"]
        )
        if scope["type"] == "full":
            if scope["n"] is None or scope["reporting_status"] != "reported":
                raise RegistryValidationError(f"{run['id']}: full scope requires reported n")
            if benchmark_total is not None and scope["n"] != benchmark_total:
                raise RegistryValidationError(
                    f"{run['id']}: full scope n={scope['n']} does not equal total={benchmark_total}"
                )
            if benchmark_audited:
                if run_version is None or benchmark_total is None:
                    raise RegistryValidationError(
                        f"{run['id']}: audited full scope requires a registered version with a reported total"
                    )
                if benchmark["verification"]["status"] != "verified":
                    raise RegistryValidationError(f"{run['id']}: full scope requires a verified benchmark")
                version_total_path = f"/versions/{version_index}/task_counts/total"
                total_paths = {version_total_path}
                if run_version["label"] == benchmark["latest_version"]:
                    total_paths.add("/task_counts/total")
                if any(
                    _field_is_flagged(benchmark.get("field_status", []), path)
                    for path in total_paths
                ):
                    raise RegistryValidationError(
                        f"{run['id']}: full scope cannot rely on a provisional/conflicted total"
                    )
                if not any(_supports_path(benchmark["evidence"], path) for path in total_paths):
                    raise RegistryValidationError(
                        f"{run['id']}: full scope requires field-level total evidence"
                    )
        if scope["n"] is not None and benchmark_total is not None and scope["n"] > benchmark_total:
            raise RegistryValidationError(f"{run['id']}: scope n exceeds benchmark total")
        subset_counts = run_version["task_counts"] if run_version is not None else benchmark["task_counts"]
        subset_ids = {item["id"] for item in subset_counts["subsets"]}
        if scope["subset_id"] is not None and scope["subset_id"] not in subset_ids:
            raise RegistryValidationError(f"{run['id']}: scope references missing subset {scope['subset_id']}")
        if scope["type"] == "subset" and (scope["subset_id"] is None or scope["n"] is None):
            raise RegistryValidationError(f"{run['id']}: subset scope requires subset_id and realized n")

        supports = {path for evidence in run["evidence"] for path in evidence["supports"]}
        if not any(path in {"scope", "/scope"} or path.startswith(("scope.", "/scope/")) for path in supports):
            raise RegistryValidationError(f"{run['id']}: scope lacks evidence support")
        if not any(path in {"metrics", "/metrics"} or path.startswith(("metrics.", "/metrics/")) for path in supports):
            raise RegistryValidationError(f"{run['id']}: metrics lack evidence support")
        if run["results"] and not any(path in {"results", "/results"} or path.startswith(("results.", "/results/")) for path in supports):
            raise RegistryValidationError(f"{run['id']}: public results lack evidence support")
        comparability[run["comparability_group"]].append(run)

    for group, runs in comparability.items():
        signatures = {
            (
                run["benchmark_id"],
                run["benchmark_version"],
                json.dumps(run["scope"], sort_keys=True),
                json.dumps(run["metrics"], sort_keys=True),
                json.dumps(run["protocol"], sort_keys=True),
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
