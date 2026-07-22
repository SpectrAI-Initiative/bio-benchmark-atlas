#!/usr/bin/env python3
"""Deterministically turn independently verified paper claims into Registry YAML."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from extract_paper import PIPELINE_VERSION, PROMPT_VERSION
from paper_models import PaperEvidenceDraft, PaperEvidenceVerification, accepted_claims
from registry_io import ROOT, load_entities, load_taxonomies
from triage_paper import duplicate_work_candidates, normalize_arxiv, normalize_doi, normalize_url, title_fingerprint


class GenerationBlocked(RuntimeError):
    pass


@dataclass
class GeneratedRecords:
    work: dict[str, Any] | None = None
    benchmarks: list[dict[str, Any]] = field(default_factory=list)
    classifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    uses: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    skipped_background_mentions: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)


def slugify(value: str, *, maximum: int = 72) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug[:maximum].rstrip("-") or "record"


def stable_work_id(title: str, doi: str | None, existing_ids: set[str]) -> str:
    base = slugify(title, maximum=54)
    if base not in existing_ids:
        return base
    suffix = hashlib.sha256((doi or title).encode()).hexdigest()[:8]
    return f"{base}-{suffix}"


def _fragment_hash(excerpt: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", excerpt).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _reported(value: Any, notes: str | None = None) -> dict[str, Any]:
    return {"value": value, "reporting_status": "reported", "notes": notes}


def _not_reported(notes: str | None = None) -> dict[str, Any]:
    return {"value": None, "reporting_status": "not_reported", "notes": notes}


def _claim_value(claim: Any) -> Any:
    return json.loads(claim.value_json)


def _source_locator(verification_item: Any) -> dict[str, Any]:
    locator = verification_item.locator
    assert locator is not None
    return {
        "type": locator.locator_type,
        "value": locator.value,
        "note": None,
        "document_page": locator.document_page,
        "printed_page": locator.printed_page,
        "source_fragment_sha256": _fragment_hash(locator.excerpt),
    }


def _support_for_claim(claim_type: str, *, run: bool) -> str:
    if not run:
        return {
            "relation": "/relation_type",
            "benchmark-identity": "/benchmark_id",
            "benchmark-version": "/benchmark_version",
            "scope-type": "/scope",
            "scope-n": "/scope",
            "subset-id": "/scope",
            "selection": "/scope",
            "selection-method": "/scope",
            "model": "/model_ids",
            "metric": "/metric_labels",
        }.get(claim_type, "/notes")
    return {
        "benchmark-version": "/benchmark_version",
        "scope-type": "/scope",
        "scope-n": "/scope",
        "subset-id": "/scope",
        "selection": "/scope",
        "selection-method": "/scope",
        "model": "/model_ids",
        "prompt": "/protocol/system_prompt_public",
        "shots": "/protocol/shots",
        "reasoning": "/protocol/reasoning",
        "tools": "/protocol/tools",
        "internet": "/protocol/tools/internet",
        "code-execution": "/protocol/tools/code_execution",
        "container": "/protocol/tools/container",
        "budget": "/protocol/token_budget",
        "seed": "/protocol/seed",
        "repeats": "/protocol/repeats",
        "grader": "/protocol/grader",
        "human-review": "/protocol/grader/human_review",
        "metric": "/metrics",
        "result": "/results",
    }.get(claim_type, "/protocol")


def _evidence_for_claims(
    claims: list[Any],
    verdicts: dict[str, Any],
    *,
    owner_id: str,
    work_id: str,
    accessed_date: str,
    run: bool,
) -> list[dict[str, Any]]:
    evidence = []
    for index, claim in enumerate(claims, 1):
        evidence.append({
            "id": f"{owner_id}-evidence-{index}",
            "source_type": "work",
            "source_id": work_id,
            "accessed_date": accessed_date,
            "locator": _source_locator(verdicts[claim.claim_id]),
            "supports": [_support_for_claim(claim.claim_type, run=run)],
        })
    return evidence


def _model_lookup(entities: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for model in entities["model"]:
        for label in [model["id"], model["name"], *model.get("aliases", [])]:
            lookup[slugify(label)] = model
    return lookup


def _materialize_model(
    payload: dict[str, Any],
    *,
    lookup: dict[str, dict[str, Any]],
    new_models: dict[str, dict[str, Any]],
    verified_on: str,
) -> str:
    name = str(payload.get("name") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    version_string = payload.get("version_string")
    if not name or not provider:
        raise GenerationBlocked("a model claim lacks an exact name or provider")
    for key in (slugify(name), slugify(f"{provider}-{name}")):
        if key in lookup:
            return lookup[key]["id"]
    model_id = slugify(f"{provider}-{name}")
    if model_id in new_models:
        return model_id
    new_models[model_id] = {
        "entity_type": "model",
        "id": model_id,
        "name": name,
        "provider": provider,
        "release_date": payload.get("release_date"),
        "version_string": version_string,
        "version_status": "reported" if version_string else "not_reported",
        "aliases": [],
        "verification": {
            "status": "verified",
            "last_verified": verified_on,
            "notes": "Exact model identity accepted by the automated double-pass paper review and pending owner PR approval.",
        },
    }
    return model_id


def _protocol(claims: list[Any]) -> dict[str, Any]:
    by_type: dict[str, list[Any]] = {}
    for claim in claims:
        by_type.setdefault(claim.claim_type, []).append(claim)

    def first_value(kind: str) -> Any | None:
        items = by_type.get(kind, [])
        return _claim_value(items[0]) if items else None

    tool_payload = first_value("tools") or {}
    for claim_type, key in (("internet", "internet"), ("code-execution", "code_execution"), ("container", "container")):
        value = first_value(claim_type)
        if value is not None:
            tool_payload[key] = value
    tools = {
        key: _reported(tool_payload[key]) if key in tool_payload else _not_reported()
        for key in ("browser", "internet", "databases", "code_execution", "container", "external_tools")
    }
    grader_payload = first_value("grader") or {}
    human_review = first_value("human-review")
    if human_review is not None:
        grader_payload["human_review"] = human_review
    grader_reported = any(value is not None for value in grader_payload.values())
    budget = first_value("budget")
    token_budget = budget.get("token") if isinstance(budget, dict) else budget
    time_budget = budget.get("time") if isinstance(budget, dict) else None
    return {
        "shots": _reported(first_value("shots")) if by_type.get("shots") else _not_reported(),
        "turns": _not_reported(),
        "system_prompt_public": _reported(first_value("prompt")) if by_type.get("prompt") else _not_reported(),
        "reasoning": _reported(first_value("reasoning")) if by_type.get("reasoning") else _not_reported(),
        "tools": tools,
        "token_budget": _reported(token_budget) if token_budget is not None else _not_reported(),
        "time_budget": _reported(time_budget) if time_budget is not None else _not_reported(),
        "temperature": _not_reported(),
        "seed": _reported(first_value("seed")) if by_type.get("seed") else _not_reported(),
        "repeats": _reported(first_value("repeats")) if by_type.get("repeats") else _not_reported(),
        "grader": {
            "type": grader_payload.get("type"),
            "model": grader_payload.get("model"),
            "human_review": grader_payload.get("human_review"),
            "reporting_status": "reported" if grader_reported else "not_reported",
        },
        "statistical": _reported(first_value("metric").get("statistical"))
        if by_type.get("metric") and isinstance(first_value("metric"), dict) and first_value("metric").get("statistical")
        else _not_reported(),
        "contamination": _not_reported(),
    }


def _scope(claims: list[Any]) -> dict[str, Any]:
    values = {claim.claim_type: _claim_value(claim) for claim in claims if claim.claim_type in {
        "scope-type", "scope-n", "subset-id", "selection", "selection-method"
    }}
    scope_type = values.get("scope-type", "unknown")
    n = values.get("scope-n")
    selection = values.get("selection")
    subset_id = values.get("subset-id")
    selection_method = values.get("selection-method")
    return {
        "type": scope_type,
        "n": n,
        "subset_id": subset_id,
        "filter": selection_method,
        "selection": selection,
        "reporting_status": "reported" if scope_type != "unknown" and (n is not None or scope_type == "track") else "not_reported",
    }


def _use_scope(run_scope: dict[str, Any]) -> dict[str, Any]:
    if run_scope["type"] == "subset":
        paper_specific = run_scope["selection"] not in {None, "formal-subset"}
        subset_kind = "paper-specific" if paper_specific else "formal-subset"
    elif run_scope["type"] == "unknown":
        subset_kind = "not-reported"
    else:
        subset_kind = "not-applicable"
    return {
        "type": run_scope["type"],
        "subset_kind": subset_kind,
        "n": run_scope["n"],
        "subset_id": run_scope["subset_id"],
        "selection": run_scope["selection"],
        "selection_method": run_scope["filter"],
        "reporting_status": run_scope["reporting_status"],
    }


def _metrics_and_results(
    claims: list[Any],
    model_ids_by_name: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    metric_id_by_label: dict[str, str] = {}
    metric_payloads: dict[str, Any] = {}
    for claim in [item for item in claims if item.claim_type == "metric"]:
        payload = _claim_value(claim)
        label = str(payload["source_label"])
        metric_id = slugify(label)
        metric_id_by_label[label.casefold()] = metric_id
        metric_payloads[metric_id] = payload
        baseline_name = payload.get("baseline_model_name")
        baseline_id = model_ids_by_name.get(str(baseline_name).casefold()) if baseline_name else None
        metrics.append({
            "metric_id": metric_id,
            "source_label": label,
            "unit": str(payload.get("unit") or "not reported"),
            "range": payload.get("range"),
            "higher_is_better": bool(payload.get("higher_is_better", True)),
            "aggregation": payload.get("aggregation"),
            "pass_threshold": payload.get("pass_threshold"),
            "tolerance": payload.get("tolerance"),
            "kind": payload.get("kind", "absolute"),
            "baseline_model_id": baseline_id,
        })
    results: list[dict[str, Any]] = []
    result_claim_by_index: dict[str, Any] = {}
    for claim in [item for item in claims if item.claim_type == "result"]:
        payload = _claim_value(claim)
        model_id = model_ids_by_name.get(str(payload.get("model_name", "")).casefold())
        metric_id = metric_id_by_label.get(str(payload.get("metric_source_label", "")).casefold())
        if model_id is None or metric_id is None:
            continue
        index = len(results)
        results.append({
            "model_id": model_id,
            "metric_id": metric_id,
            "value": payload["value"],
            "ci_low": payload.get("ci_low"),
            "ci_high": payload.get("ci_high"),
            "n": payload.get("n"),
            "notes": payload.get("notes"),
            "status": "verified",
            "confidence": "high",
            "evidence_ids": [],
        })
        result_claim_by_index[str(index)] = claim
    return metrics, results, result_claim_by_index


def _build_new_benchmark(
    *,
    benchmark_id: str,
    claims: list[Any],
    verdicts: dict[str, Any],
    work_id: str,
    source: dict[str, Any],
    verified_on: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_type: dict[str, list[Any]] = {}
    for claim in claims:
        by_type.setdefault(claim.claim_type, []).append(claim)
    required = {
        "benchmark-metadata", "benchmark-version", "benchmark-count", "creator-source",
        "official-repository", "scientific-task",
    }
    missing = sorted(required - set(by_type))
    if missing:
        raise GenerationBlocked(
            f"{benchmark_id}: new benchmark lacks verified claims: {', '.join(missing)}"
        )
    metadata = _claim_value(by_type["benchmark-metadata"][0])
    version_label = str(_claim_value(by_type["benchmark-version"][0]))
    creator = _claim_value(by_type["creator-source"][0])
    repository = _claim_value(by_type["official-repository"][0])
    repository_url = normalize_url(repository.get("url"))
    repository_pins = source.get("repository_pins", {})
    pin = repository_pins.get(repository_url or "")
    if repository_url is None or pin is None:
        raise GenerationBlocked(f"{benchmark_id}: official repository could not be commit-pinned")

    taxonomies = load_taxonomies()
    controlled = {
        axis: {item["id"] for item in taxonomies[axis]}
        for axis in ("domains", "capabilities", "modalities", "access_levels", "scientific_tasks")
    }
    for axis in ("domains", "capabilities", "modalities"):
        values = metadata.get(axis) or []
        if not values or not set(values) <= controlled[axis]:
            raise GenerationBlocked(f"{benchmark_id}: new benchmark has invalid {axis} taxonomy IDs")
    access = metadata.get("access") or {}
    if access.get("level") not in controlled["access_levels"]:
        raise GenerationBlocked(f"{benchmark_id}: new benchmark has an invalid access level")
    if len(str(metadata.get("summary") or "")) < 20:
        raise GenerationBlocked(f"{benchmark_id}: new benchmark summary is too short")

    total_claim = next(
        (claim for claim in by_type["benchmark-count"] if _claim_value(claim).get("subset_id") is None),
        None,
    )
    if total_claim is None:
        raise GenerationBlocked(f"{benchmark_id}: new benchmark has no verified total-count claim")
    total_payload = _claim_value(total_claim)
    total = total_payload.get("count")
    reporting_status = total_payload.get("reporting_status")
    if (total is None) != (reporting_status == "not_reported"):
        raise GenerationBlocked(f"{benchmark_id}: total count/reporting status is inconsistent")
    subsets = []
    for claim in by_type["benchmark-count"]:
        payload = _claim_value(claim)
        if payload.get("subset_id") is None:
            continue
        subsets.append({
            "id": slugify(str(payload["subset_id"])),
            "label": str(payload.get("label") or payload["subset_id"]),
            "count": payload.get("count"),
            "basis": str(payload.get("basis") or "Not reported"),
            "exclusive": bool(payload.get("exclusive", False)),
            "exhaustive": bool(payload.get("exhaustive", False)),
            "partition_group": slugify(str(payload["partition_group"])) if payload.get("partition_group") else None,
            "reporting_status": payload.get("reporting_status", "reported"),
            "notes": None,
        })
    task_counts = {
        "total": total,
        "basis": str(total_payload.get("basis") or "Not reported by the source"),
        "reporting_status": reporting_status,
        "subsets": subsets,
    }

    metadata_claim = by_type["benchmark-metadata"][0]
    repository_claim = by_type["official-repository"][0]
    creator_claim = by_type["creator-source"][0]
    version_claim = by_type["benchmark-version"][0]
    evidence = [
        {
            "id": f"{benchmark_id}-automated-metadata-evidence",
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[metadata_claim.claim_id]),
            "supports": [
                "/name", "/aliases", "/summary", "/kind", "/organizations", "/release_date",
                "/latest_version", "/domains", "/capabilities", "/modalities", "/task_formats",
                "/access/level", "/access/license",
            ],
        },
        {
            "id": f"{benchmark_id}-automated-count-evidence",
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[total_claim.claim_id]),
            "supports": ["/task_counts/total", "/task_counts/basis", "/task_counts/subsets", "/versions/0/task_counts"],
        },
        {
            "id": f"{benchmark_id}-automated-resource-evidence",
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[repository_claim.claim_id]),
            "supports": ["/resources", "/implementations"],
        },
        {
            "id": f"{benchmark_id}-automated-creator-evidence",
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[creator_claim.claim_id]),
            "supports": ["/resources/0"],
        },
        {
            "id": f"{benchmark_id}-automated-version-evidence",
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[version_claim.claim_id]),
            "supports": ["/latest_version", "/versions/0"],
        },
    ]
    classification_entries = []
    for index, task_claim in enumerate(by_type["scientific-task"]):
        payload = _claim_value(task_claim)
        task_id = payload.get("task_type_id")
        if task_id not in controlled["scientific_tasks"]:
            raise GenerationBlocked(f"{benchmark_id}: invalid Scientific Task ID {task_id}")
        entry_evidence_id = f"{benchmark_id}-automated-task-{index + 1}-evidence"
        evidence.append({
            "id": entry_evidence_id,
            "source_type": "work", "source_id": work_id, "accessed_date": verified_on,
            "locator": _source_locator(verdicts[task_claim.claim_id]),
            "supports": [f"/scientific_task_classification/entries/{index}"],
        })
        count = payload.get("count")
        task_reporting = payload.get("reporting_status")
        if (count is None) != (task_reporting == "not_reported"):
            raise GenerationBlocked(f"{benchmark_id}: Scientific Task count/reporting status is inconsistent")
        classification_entries.append({
            "task_type_id": task_id,
            "coverage": payload.get("coverage", "explicitly-in-scope"),
            "mapping_method": payload.get("mapping_method", "official-taxonomy"),
            "confidence": "high",
            "count": count,
            "count_unit": payload.get("count_unit", "tasks"),
            "count_basis": str(payload.get("count_basis") or "Benchmark items"),
            "count_ref": "/task_counts/total" if count is not None and count == total else None,
            "reporting_status": task_reporting,
            "evidence_ids": [entry_evidence_id],
            "notes": payload.get("notes"),
        })

    creator_url = normalize_url(creator.get("url")) or source["url"]
    benchmark = {
        "entity_type": "benchmark",
        "id": benchmark_id,
        "name": str(metadata["name"]),
        "aliases": list(dict.fromkeys(str(item) for item in metadata.get("aliases", []))),
        "summary": str(metadata["summary"]),
        "kind": metadata["kind"],
        "parent_id": None,
        "organizations": metadata["organizations"],
        "release_date": metadata["release_date"],
        "latest_version": version_label,
        "domains": metadata["domains"],
        "capabilities": metadata["capabilities"],
        "modalities": metadata["modalities"],
        "task_formats": metadata["task_formats"],
        "task_counts": task_counts,
        "coverage_notes": [],
        "access": {
            "level": access["level"], "tasks": access["tasks"], "artifacts": access["artifacts"],
            "grader": access["grader"], "license": access.get("license"),
            "biosafety_notes": access.get("biosafety_notes"),
        },
        "resources": [
            {
                "id": f"{benchmark_id}-creator-paper-resource", "type": "paper", "url": creator_url,
                "license": None, "access_notes": "Versioned creator source; full text is not mirrored.",
                "last_checked": verified_on, "pin": None,
            },
            {
                "id": f"{benchmark_id}-official-repository-resource", "type": "repository", "url": repository_url,
                "license": repository.get("license"), "access_notes": "Official repository pinned during intake.",
                "last_checked": verified_on,
                "pin": {"kind": pin["kind"], "value": pin["value"], "url": pin["url"]},
            },
        ],
        "implementations": [{
            "framework": "official repository", "status": "official", "url": repository_url,
            "commit": pin["value"], "notes": "Commit resolved deterministically during paper intake.",
        }],
        "versions": [{
            "id": f"{benchmark_id}-{slugify(version_label, maximum=30)}-version",
            "label": version_label, "status": "current", "release_date": metadata["release_date"],
            "as_of": None, "task_counts": task_counts, "formal_tracks": [], "notes": None,
            "evidence_ids": [f"{benchmark_id}-automated-count-evidence", f"{benchmark_id}-automated-version-evidence"],
        }],
        "audit": {"status": "audited", "audited_date": verified_on, "unresolved_fields": 0,
                  "notes": "Automated double-pass extraction plus deterministic repository pin; owner approval required."},
        "field_status": [],
        "verification": {"status": "verified", "last_verified": verified_on,
                         "notes": "New family admitted only after creator source, official repository, and owner PR review."},
        "evidence": evidence,
    }
    classification = {
        "status": "partial",
        "benchmark_version": version_label,
        "as_of": None,
        "notes": "Only high-confidence Scientific Tasks explicitly supported by the creator source are mapped.",
        "entries": classification_entries,
    }
    return benchmark, classification


def build_records(
    result_payload: dict[str, Any],
    *,
    source: dict[str, Any],
    generated_at: str,
    verified_on: str,
) -> GeneratedRecords:
    draft = PaperEvidenceDraft.model_validate(result_payload["draft"])
    verification = PaperEvidenceVerification.model_validate(result_payload["verification"])
    if verification.blocking_conflicts:
        raise GenerationBlocked("blocking source conflicts: " + "; ".join(verification.blocking_conflicts))
    accepted = accepted_claims(draft, verification)
    identity_claims = [claim for claim in accepted if claim.claim_type == "paper-identity"]
    if not identity_claims:
        raise GenerationBlocked("paper identity was not independently verified at high confidence")
    identity_payload = _claim_value(identity_claims[0])
    if title_fingerprint(identity_payload.get("title")) != title_fingerprint(draft.paper.title):
        raise GenerationBlocked("verified paper title conflicts with the structured paper identity")
    for key, normalizer in (("doi", normalize_doi), ("arxiv", lambda value: normalize_arxiv(value)[0])):
        claimed = normalizer(identity_payload.get(key))
        drafted = normalizer(getattr(draft.paper, key))
        if claimed and drafted and claimed != drafted:
            raise GenerationBlocked(f"verified paper {key} conflicts with the structured paper identity")
    verdicts = {item.claim_id: item for item in verification.claims}
    accepted_by_mention: dict[str, list[Any]] = {}
    for claim in accepted:
        if claim.mention_id:
            accepted_by_mention.setdefault(claim.mention_id, []).append(claim)

    entities = load_entities()
    existing_work_ids = {item["id"] for item in entities["work"]}
    identity = {
        "doi": normalize_doi(draft.paper.doi),
        "arxiv": normalize_arxiv(draft.paper.arxiv)[0],
        "canonical_url": normalize_url(draft.paper.canonical_url),
        "title_fingerprint": title_fingerprint(draft.paper.title),
    }
    duplicates = duplicate_work_candidates(identity, entities["work"])
    existing_work = next((item for item in entities["work"] if duplicates and item["id"] == duplicates[0]["work_id"]), None)
    work_id = existing_work["id"] if existing_work else stable_work_id(draft.paper.title, identity["doi"], existing_work_ids)
    publication_date = draft.paper.publication_date or verified_on
    version_suffix = slugify(draft.paper.version_label or publication_date, maximum=24)
    work_version_id = existing_work["current_version_id"] if existing_work else f"{work_id}-{version_suffix}"

    output = GeneratedRecords()
    creates_new_benchmark = any(
        mention.is_new_benchmark and mention.relation_type == "benchmark-creation"
        and not mention.background_only
        for mention in draft.benchmark_mentions
    )
    if existing_work is None:
        output.work = {
            "entity_type": "work",
            "id": work_id,
            "title": draft.paper.title,
            "authors": draft.paper.authors,
            "organizations": draft.paper.organizations,
            "work_type": "paper" if identity["doi"] else "preprint",
            "source_class": "benchmark_creator" if creates_new_benchmark else "independent_reproduction",
            "publication_date": publication_date,
            "canonical_url": identity["canonical_url"] or source["url"],
            "doi": identity["doi"],
            "arxiv": identity["arxiv"],
            "status": "published" if identity["doi"] else "preprint",
            "source_versions": [{
                "id": work_version_id,
                "label": draft.paper.version_label or "Reviewed source",
                "status": "version-of-record" if identity["doi"] else "current",
                "publication_date": publication_date,
                "canonical_url": identity["canonical_url"] or source["url"],
                "doi": identity["doi"],
                "arxiv": identity["arxiv"],
                "source_access": source["source_access"],
                "content_sha256": source["content_sha256"],
                "content_type": source["content_type"],
                "retrieved_at": source["retrieved_at"],
            }],
            "current_version_id": work_version_id,
            "review_provenance": {
                "method": "automated-double-pass",
                "pipeline_version": result_payload.get("pipeline_version", PIPELINE_VERSION),
                "prompt_version": result_payload.get("prompt_version", PROMPT_VERSION),
                "source_version_id": work_version_id,
                "extractor_model_requested": result_payload["extractor_model_requested"],
                "extractor_model_resolved": result_payload["extractor_model_resolved"],
                "verifier_model_requested": result_payload["verifier_model_requested"],
                "verifier_model_resolved": result_payload["verifier_model_resolved"],
                "generated_at": generated_at,
            },
            "verification": {
                "status": "verified",
                "last_verified": verified_on,
                "notes": "AI-assisted double-pass extraction; production inclusion still requires wang422003 PR approval.",
            },
        }

    benchmarks = {item["id"]: item for item in entities["benchmark"]}
    new_benchmark_ids_by_name: dict[str, str] = {}
    for mention in draft.benchmark_mentions:
        if not mention.is_new_benchmark or mention.relation_type != "benchmark-creation" or mention.background_only:
            continue
        claims = [
            claim for claim in accepted_by_mention.get(mention.mention_id, [])
            if claim.claim_id in set(mention.claim_ids)
        ]
        relation_claim = next((item for item in claims if item.claim_type == "relation"), None)
        if relation_claim is None or _claim_value(relation_claim) != "benchmark-creation":
            output.blocked_reasons.append(f"{mention.benchmark_name}: creation relation was not independently verified")
            continue
        benchmark_id = slugify(mention.benchmark_name)
        if benchmark_id in benchmarks:
            output.blocked_reasons.append(f"{mention.benchmark_name}: generated benchmark ID already exists")
            continue
        try:
            benchmark, classification = _build_new_benchmark(
                benchmark_id=benchmark_id, claims=claims, verdicts=verdicts, work_id=work_id,
                source=source, verified_on=verified_on,
            )
        except GenerationBlocked as error:
            output.blocked_reasons.append(str(error))
            continue
        output.benchmarks.append(benchmark)
        output.classifications[benchmark_id] = classification
        benchmarks[benchmark_id] = benchmark
        new_benchmark_ids_by_name[slugify(mention.benchmark_name)] = benchmark_id
        new_benchmark_ids_by_name[slugify(benchmark["name"])] = benchmark_id
    model_lookup = _model_lookup(entities)
    new_models: dict[str, dict[str, Any]] = {}
    for mention_index, mention in enumerate(draft.benchmark_mentions, 1):
        if mention.background_only or mention.relation_type == "background-citation":
            output.skipped_background_mentions.append(mention.benchmark_name)
            continue
        claims = accepted_by_mention.get(mention.mention_id, [])
        declared_claim_ids = set(mention.claim_ids)
        claims = [claim for claim in claims if claim.claim_id in declared_claim_ids]
        if not claims or not any(item.claim_type == "relation" for item in claims):
            output.blocked_reasons.append(f"{mention.benchmark_name}: relation was not independently verified")
            continue
        relation_claim = next(item for item in claims if item.claim_type == "relation")
        if _claim_value(relation_claim) != mention.relation_type:
            output.blocked_reasons.append(f"{mention.benchmark_name}: relation claim conflicts with the mention")
            continue
        benchmark_id = (
            new_benchmark_ids_by_name.get(slugify(mention.benchmark_name))
            if mention.is_new_benchmark else mention.registry_benchmark_id
        )
        if benchmark_id not in benchmarks:
            output.blocked_reasons.append(f"{mention.benchmark_name}: Registry benchmark identity is unresolved")
            continue
        identity_claim = next((item for item in claims if item.claim_type == "benchmark-identity"), None)
        identity_value = _claim_value(identity_claim) if identity_claim else None
        identity_matches = (
            identity_value == benchmark_id
            or (mention.is_new_benchmark and slugify(str(identity_value)) in {
                slugify(mention.benchmark_name), slugify(benchmarks[benchmark_id]["name"])
            })
        )
        if identity_claim is None or not identity_matches:
            output.blocked_reasons.append(f"{mention.benchmark_name}: benchmark identity was not independently verified")
            continue

        model_ids: list[str] = []
        model_ids_by_name: dict[str, str] = {}
        for claim in [item for item in claims if item.claim_type == "model"]:
            payload = _claim_value(claim)
            model_id = _materialize_model(payload, lookup=model_lookup, new_models=new_models, verified_on=verified_on)
            model_ids.append(model_id)
            model_ids_by_name[str(payload["name"]).casefold()] = model_id

        scope = _scope(claims)
        version_claim = next((item for item in claims if item.claim_type == "benchmark-version"), None)
        benchmark_version = _claim_value(version_claim) if version_claim else None
        metric_claims = [item for item in claims if item.claim_type == "metric"]
        metrics, results, result_claims = _metrics_and_results(claims, model_ids_by_name)
        known_versions = {item["label"] for item in benchmarks[benchmark_id].get("versions", [])}
        version_is_registered = benchmark_version in known_versions
        full_count_is_verified = True
        if scope["type"] == "full":
            version_record = next((item for item in benchmarks[benchmark_id].get("versions", []) if item["label"] == benchmark_version), None)
            full_count_is_verified = bool(
                version_record
                and version_record["task_counts"]["total"] is not None
                and version_record["task_counts"]["total"] == scope["n"]
            )
        subset_is_valid = True
        if scope["type"] == "subset":
            version_record = next((item for item in benchmarks[benchmark_id].get("versions", []) if item["label"] == benchmark_version), None)
            registered_subsets = {
                item["id"] for item in (version_record or {}).get("task_counts", {}).get("subsets", [])
            }
            subset_is_valid = (
                scope["subset_id"] in registered_subsets
                or (scope["selection"] not in {None, "formal-subset"} and bool(scope["filter"]))
            )
        delta_baselines_are_known = all(
            metric["kind"] != "delta" or metric["baseline_model_id"] is not None
            for metric in metrics
        )
        can_normalize = (
            mention.relation_type == "evaluation"
            and benchmark_version is not None
            and version_is_registered
            and scope["type"] != "unknown"
            and (scope["type"] == "track" or scope["n"] is not None)
            and full_count_is_verified
            and subset_is_valid
            and bool(model_ids)
            and bool(metrics)
            and bool(results)
            and delta_baselines_are_known
        )
        run_id = f"{work_id}-{benchmark_id}-{mention_index}"
        use_id = f"{work_id}-{benchmark_id}-{mention_index}-use"
        if can_normalize:
            run_evidence_claims = [claim for claim in claims if claim.claim_type not in {
                "relation", "benchmark-identity", "creator-source", "official-repository", "scientific-task"
            }]
            run_evidence = _evidence_for_claims(
                run_evidence_claims, verdicts, owner_id=run_id, work_id=work_id,
                accessed_date=verified_on, run=True,
            )
            evidence_id_by_claim = {
                claim.claim_id: run_evidence[index]["id"]
                for index, claim in enumerate(run_evidence_claims)
            }
            for index, result in enumerate(results):
                claim = result_claims[str(index)]
                result["evidence_ids"] = [evidence_id_by_claim[claim.claim_id]]
            run = {
                "entity_type": "evaluation_run",
                "id": run_id,
                "work_id": work_id,
                "work_version_id": work_version_id,
                "benchmark_id": benchmark_id,
                "benchmark_version": benchmark_version,
                "model_ids": sorted(set(model_ids)),
                "scope": scope,
                "protocol": _protocol(claims),
                "metrics": metrics,
                "results": results,
                "comparability_group": run_id,
                "verification": {
                    "status": "verified",
                    "last_verified": verified_on,
                    "notes": "All normalized claim values passed independent high-confidence verification and owner review remains required.",
                },
                "evidence": run_evidence,
            }
            output.runs.append(run)

        use_evidence_claims = [claim for claim in claims if claim.claim_type in {
            "relation", "benchmark-identity", "benchmark-version", "scope-type", "scope-n",
            "subset-id", "selection", "selection-method", "model", "metric",
        }]
        if not use_evidence_claims:
            output.blocked_reasons.append(f"{mention.benchmark_name}: no publishable relationship evidence")
            continue
        gaps = list(dict.fromkeys(mention.reporting_gaps))
        if not can_normalize and mention.relation_type in {"evaluation", "external-result-summary"}:
            requirements = {
                "benchmark version": benchmark_version,
                "realized n/scope": scope["n"] if scope["type"] != "track" else scope["type"],
                "exact model": model_ids or None,
                "metric": metrics or None,
                "numeric result": results or None,
            }
            gaps.extend(label for label, value in requirements.items() if value is None or value == [])
        use_status = "normalized" if can_normalize else (
            "non-evaluation" if mention.relation_type in {
                "benchmark-creation", "training", "fine-tuning", "validation", "model-selection"
            } else "partial"
        )
        use_scope = _use_scope(scope)
        if use_status == "non-evaluation":
            use_scope = {
                "type": "unknown", "subset_kind": "not-applicable", "n": None,
                "subset_id": None, "selection": None, "selection_method": None,
                "reporting_status": "not_applicable",
            }
        output.uses.append({
            "entity_type": "benchmark_use",
            "id": use_id,
            "work_id": work_id,
            "work_version_id": work_version_id,
            "benchmark_id": benchmark_id,
            "benchmark_version": benchmark_version,
            "relation_type": mention.relation_type,
            "status": use_status,
            "model_ids": sorted(set(model_ids)),
            "scope": use_scope,
            "metric_labels": [item["source_label"] for item in metrics],
            "evaluation_run_ids": [run_id] if can_normalize else [],
            "reporting_gaps": [] if use_status == "normalized" else list(dict.fromkeys(gaps)),
            "notes": "AI-assisted double-pass extraction; values are limited to independently supported claims.",
            "verification": {
                "status": "verified",
                "last_verified": verified_on,
                "notes": "Ready for production only after owner approval of this paper-intake PR.",
            },
            "evidence": _evidence_for_claims(
                use_evidence_claims, verdicts, owner_id=use_id, work_id=work_id,
                accessed_date=verified_on, run=False,
            ),
        })

    output.models = sorted(new_models.values(), key=lambda item: item["id"])
    if not output.uses and not output.blocked_reasons:
        raise GenerationBlocked("the paper contains no actual benchmark use")
    return output


def _write_entity(path: Path, payload: dict[str, Any]) -> None:
    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)
    if path.exists() and path.read_text(encoding="utf-8") != serialized:
        raise GenerationBlocked(f"idempotency conflict: {path.relative_to(ROOT)} already exists with different content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def write_records(records: GeneratedRecords) -> list[Path]:
    paths: list[Path] = []
    if records.work:
        path = ROOT / "registry" / "works" / f"{records.work['id']}.yaml"
        _write_entity(path, records.work); paths.append(path)
    for benchmark in records.benchmarks:
        path = ROOT / "registry" / "benchmarks" / f"{benchmark['id']}.yaml"
        _write_entity(path, benchmark); paths.append(path)
    for benchmark_id, classification in records.classifications.items():
        path = ROOT / "registry" / "scientific_task_classifications" / f"automated-{benchmark_id}.yaml"
        payload = {"classifications": {benchmark_id: classification}}
        serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise GenerationBlocked(f"idempotency conflict: {path.relative_to(ROOT)} already exists with different content")
        path.write_text(serialized, encoding="utf-8")
        paths.append(path)
    for model in records.models:
        path = ROOT / "registry" / "models" / f"{model['id']}.yaml"
        _write_entity(path, model); paths.append(path)
    for use in records.uses:
        path = ROOT / "registry" / "benchmark_uses" / f"{use['id']}.yaml"
        _write_entity(path, use); paths.append(path)
    for run in records.runs:
        path = ROOT / "registry" / "evaluations" / f"{run['id']}.yaml"
        _write_entity(path, run); paths.append(path)
    entity_ids = [
        *([records.work["id"]] if records.work else []),
        *(item["id"] for item in records.benchmarks),
        *(item["id"] for item in records.models),
        *(item["id"] for item in records.uses),
        *(item["id"] for item in records.runs),
    ]
    if entity_ids:
        changelog_path = ROOT / "registry" / "changelog.yaml"
        existing = changelog_path.read_text(encoding="utf-8")
        work_id = records.work["id"] if records.work else records.uses[0]["work_id"]
        marker = f"paper-intake:{work_id}"
        if marker not in existing:
            verified_on = (
                records.work or records.uses[0]
            )["verification"]["last_verified"]
            entry = {
                "date": verified_on,
                "version": "1.4.0-dev",
                "type": "paper-intake",
                "summary": f"AI-assisted double-pass paper intake ({marker}); production inclusion required owner approval after the final bot push.",
                "entity_ids": entity_ids,
            }
            prefix = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=120)
            changelog_path.write_text(prefix + existing, encoding="utf-8")
            paths.append(changelog_path)
    return paths


def chinese_summary(records: GeneratedRecords) -> str:
    lines = ["## 自动论文审计摘要", ""]
    if records.work:
        lines.append(f"- 新增 Work：`{records.work['id']}`（双阶段 AI 辅助抽取，仍需 owner 审阅）")
    if records.benchmarks:
        lines.append("- 新增 root benchmark：" + "、".join(f"`{item['id']}`" for item in records.benchmarks) + "；creator source、official repository 与 commit pin 已同时生成。")
    lines.append(f"- BenchmarkUse：{len(records.uses)} 条；normalized run：{len(records.runs)} 条；新增模型：{len(records.models)} 个。")
    for use in records.uses:
        gaps = "、".join(use["reporting_gaps"]) if use["reporting_gaps"] else "无"
        lines.append(
            f"- `{use['benchmark_id']}`：{use['relation_type']} / {use['status']}；"
            f"scope={use['scope']['type']}，n={use['scope']['n'] if use['scope']['n'] is not None else 'Not reported'}；"
            f"metrics={', '.join(use['metric_labels']) or 'Not reported'}；缺口：{gaps}。"
        )
    if records.skipped_background_mentions:
        lines.append("- 已排除纯 related-work 引用：" + "、".join(records.skipped_background_mentions) + "。")
    if records.blocked_reasons:
        lines.append("- 需要人工处理：" + "；".join(records.blocked_reasons) + "。")
    lines.append("")
    lines.append("合并前必须由 `wang422003` 提交 APPROVED review；bot 后续 push 会使旧批准失效。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--source-metadata", type=Path, required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--verified-on", default=date.today().isoformat())
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    records = build_records(
        json.loads(args.result.read_text(encoding="utf-8")),
        source=json.loads(args.source_metadata.read_text(encoding="utf-8")),
        generated_at=args.generated_at,
        verified_on=args.verified_on,
    )
    if records.blocked_reasons:
        raise GenerationBlocked("; ".join(records.blocked_reasons))
    if args.write:
        write_records(records)
    args.summary.write_text(chinese_summary(records), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
