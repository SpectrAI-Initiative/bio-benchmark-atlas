from __future__ import annotations

import io
import csv
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from discover_papers import (  # noqa: E402
    Candidate,
    AREA_QUOTAS,
    _request,
    close_stale_candidates,
    deduplicate_candidates,
    existing_candidate_fingerprints,
    fetch_europe_pmc,
    score_candidate,
    select_by_quota,
)
from generate_paper_records import (  # noqa: E402
    GenerationBlocked,
    _materialize_benchmark_metadata,
    _scope,
    _use_scope,
    build_records,
    stable_work_id,
    write_records,
)
from extract_paper import (  # noqa: E402
    EXTRACTOR_PROMPT,
    CodexExecutionError,
    PaperExtractionError,
    VERIFIER_PROMPT,
    _child_environment,
    _codex_failure_diagnostic,
    _focused_pdf_text_source,
    _normalize_temporary_claim_ids,
    _page_anchored_pdf_text_source,
    _pdf_pages_for_visual_review,
    _prepare_local_text_source,
    _render_pdf_pages,
    _run_stage,
    _structured_output_diagnostic,
    _verifier_pdf_context_pages,
    _verifier_source_images,
    heartbeat_path,
    review_source_sha256,
    run_double_pass,
)
from paper_models import (  # noqa: E402
    EvidenceClaimDraft,
    LocatorDraft,
    PaperEvidenceDraft,
    PaperEvidenceVerification,
    accepted_claims,
    effective_blocking_conflicts,
)
from paper_extraction_eval import (  # noqa: E402
    GoldenSource,
    _checkpoint_case_current,
    _codex_cli_major,
    _golden_source_fingerprint,
    _has_count_value,
    _has_evaluation_size,
)
from local_paper_intake import (  # noqa: E402
    BatchWorktree,
    LocalIntakeError,
    MAX_PARALLEL_RUNS,
    _active_run_states,
    _ensure_labels,
    _existing_pr,
    _owner_conflict_resolution,
    _reserve_run,
    _run,
    heartbeat_status,
    run_batch,
)
from paper_source import (  # noqa: E402
    MAX_SOURCE_BYTES,
    SourceAcquisitionError,
    is_automatic_source_allowed,
    retrieve_source,
)
from registry_io import load_entities  # noqa: E402
from run_paper_intake import (  # noqa: E402
    _focus_pdf_pages,
    _github_json_request,
    resolve_resource_pins,
)
from triage_paper import resolve_crossref  # noqa: E402
from validate_registry import validate_registry  # noqa: E402
from build_registry import main as build_registry  # noqa: E402


def locator(excerpt: str = "The source reports this value in Table 1.") -> dict[str, Any]:
    return {
        "locator_type": "table",
        "value": "Table 1",
        "document_page": 3,
        "printed_page": "2",
        "excerpt": excerpt,
    }


def draft_payload(claims: list[dict[str, Any]], mention: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper": {
            "title": "Synthetic benchmark evaluation paper",
            "authors": ["Ada Researcher"],
            "organizations": ["Example Institute"],
            "publication_date": "2026-07-01",
            "doi": "10.9999/synthetic.1",
            "arxiv": None,
            "canonical_url": "https://doi.org/10.9999/synthetic.1",
            "version_label": "version-of-record",
        },
        "benchmark_mentions": [mention],
        "claims": claims,
        "reporting_gaps": [],
        "conflicts": [],
    }


def claim(
    claim_id: str,
    claim_type: str,
    value: Any,
    *,
    mention_id: str | None = "mention-1",
    field_path: str | None = None,
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "mention_id": mention_id,
        "claim_type": claim_type,
        "field_path": field_path or f"/claims/{claim_id}",
        "value_json": json.dumps(value),
        "confidence": "high",
        "locators": [locator()],
    }


def verified_result(claims: list[dict[str, Any]], mention: dict[str, Any]) -> dict[str, Any]:
    draft = draft_payload(claims, mention)
    return {
        "pipeline_version": "1.4.0",
        "prompt_version": "paper-evidence-v1",
        "extractor_model_requested": "gpt-5.6-sol",
        "extractor_model_resolved": "gpt-5.6-sol-2026-07-01",
        "verifier_model_requested": "gpt-5.6-sol",
        "verifier_model_resolved": "gpt-5.6-sol-2026-07-01",
        "draft": draft,
        "verification": {
            "source_parseable": True,
            "conflicts": [],
            "blocking_conflicts": [],
            "claims": [{
                "claim_id": item["claim_id"], "verdict": "supported", "confidence": "high",
                "locator": item["locators"][0], "notes": None,
            } for item in claims],
        },
        "accepted_claim_ids": [item["claim_id"] for item in claims],
    }


def local_verified_result(claims: list[dict[str, Any]], mention: dict[str, Any]) -> dict[str, Any]:
    payload = verified_result(claims, mention)
    payload.update({
        "review_method": "local-codex-double-pass",
        "execution_surface": "local-codex-cli",
        "prompt_version": "paper-evidence-local-v1",
        "extractor_model_resolved": None,
        "verifier_model_resolved": None,
        "model_resolution_status": "not-reported",
        "codex_cli_version": "codex-cli 1.2.3",
        "local_run_id": "11111111-1111-4111-8111-111111111111",
    })
    return payload


SOURCE = {
    "url": "https://example.org/paper.pdf",
    "source_access": "open-url",
    "content_sha256": "a" * 64,
    "content_type": "application/pdf",
    "retrieved_at": "2026-07-22T00:00:00+00:00",
}


def test_structured_output_bounds_long_quotes_and_rejects_unlabeled_graph_values() -> None:
    bounded = LocatorDraft(**locator(" ".join(f"word-{index}" for index in range(21))))
    assert len(bounded.excerpt.split()) == 20
    assert "word-20" not in bounded.excerpt
    claims = [
        claim("claim-1", "paper-identity", {"title": "x"}, mention_id=None),
        claim("claim-2", "result", {
            "model_name": "x", "metric_source_label": "Accuracy", "value": 0.8,
            "ci_low": None, "ci_high": None, "n": 10, "notes": None,
            "numeric_source": "unlabeled-figure",
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench", "relation_type": "evaluation",
        "is_new_benchmark": False, "background_only": False,
        "claim_ids": ["claim-2"], "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    draft = PaperEvidenceDraft.model_validate(payload["draft"])
    verification = PaperEvidenceVerification.model_validate(payload["verification"])
    assert [item.claim_id for item in accepted_claims(draft, verification)] == ["claim-1"]


def test_structured_conflicts_distinguish_extractor_errors_from_source_conflicts() -> None:
    claim_verification = {
        "claim_id": "claim-1",
        "verdict": "unsupported",
        "confidence": "high",
        "locator": locator(),
        "notes": "The extractor read a repeat count as sample n.",
    }
    extractor_error = PaperEvidenceVerification.model_validate({
        "claims": [claim_verification],
        "source_parseable": True,
        "conflicts": [{
            "kind": "extractor-error",
            "claim_ids": ["claim-1"],
            "summary": "Five independent runs are repeats, not realized sample n.",
        }],
        "blocking_conflicts": [],
    })
    assert effective_blocking_conflicts(extractor_error) == []

    source_conflict = PaperEvidenceVerification.model_validate({
        "claims": [{**claim_verification, "verdict": "conflicted"}],
        "source_parseable": True,
        "conflicts": [{
            "kind": "source-internal",
            "claim_ids": ["claim-1"],
            "summary": "The source prints incompatible inventory totals.",
        }],
        "blocking_conflicts": [],
    })
    assert effective_blocking_conflicts(source_conflict) == [
        "claim-1: The source prints incompatible inventory totals."
    ]

    with pytest.raises(ValidationError, match="cannot both be populated"):
        PaperEvidenceVerification.model_validate({
            **source_conflict.model_dump(mode="json"),
            "blocking_conflicts": ["legacy duplicate"],
        })

    legacy = PaperEvidenceVerification.model_validate({
        "claims": [],
        "source_parseable": True,
        "blocking_conflicts": ["Legacy source conflict."],
    })
    assert legacy.conflicts == []
    assert effective_blocking_conflicts(legacy) == ["Legacy source conflict."]


def test_model_facing_schemas_require_every_declared_property() -> None:
    for model in (PaperEvidenceDraft, PaperEvidenceVerification):
        schema = model.model_json_schema()
        objects = [schema, *schema.get("$defs", {}).values()]
        for definition in objects:
            properties = definition.get("properties")
            if properties is not None:
                assert set(definition.get("required", [])) == set(properties)
                assert definition.get("additionalProperties") is False


def test_verifier_prompt_treats_creator_and_evaluation_relations_as_compatible() -> None:
    from extract_paper import (
        EXTRACTOR_PROMPT,
        PROMPT_VERSION,
        SOURCE_INPUT_PROTOCOL_VERSION,
        VERIFIER_PROMPT,
    )

    assert PROMPT_VERSION == "paper-evidence-local-v17"
    assert SOURCE_INPUT_PROTOCOL_VERSION == "page-anchored-pdf-v4"
    assert "Normalize arXiv identifiers to the base numeric ID" in EXTRACTOR_PROMPT
    assert "the suffix belongs to the paper version" in VERIFIER_PROMPT
    assert "Benchmark-creation and evaluation are compatible" in VERIFIER_PROMPT
    assert "Their coexistence is not a conflict" in VERIFIER_PROMPT
    assert "provider-qualified printed label" in VERIFIER_PROMPT
    assert "does not verify a benchmark version" in VERIFIER_PROMPT
    assert "complete leading label" in EXTRACTOR_PROMPT
    assert "keep every atomic benchmark-metadata claim count-, version-" in EXTRACTOR_PROMPT
    assert "Never bundle multiple metadata" in EXTRACTOR_PROMPT
    assert "verify each atomic benchmark-metadata field claim" in VERIFIER_PROMPT
    assert "Do not duplicate those creator-only claims" in EXTRACTOR_PROMPT
    assert "explicitly accepts\nkind=dataset" in EXTRACTOR_PROMPT
    assert "registry_benchmark_id must be null" in EXTRACTOR_PROMPT
    assert "no Registry ID or alias can exist yet" in VERIFIER_PROMPT
    assert '"count_role": "root-total"|"formal-subset"|"auxiliary"' in EXTRACTOR_PROMPT
    assert "Independently verify count_role" in VERIFIER_PROMPT
    assert "Different measurements of the same subset" in VERIFIER_PROMPT
    assert "official-repository or official-resource" in EXTRACTOR_PROMPT
    assert "versioned dataset artifact" in VERIFIER_PROMPT
    assert "permits kind=dataset" in VERIFIER_PROMPT
    assert "Do not promote a source-" in VERIFIER_PROMPT
    assert "explicitly inspect the abstract, introduction" in EXTRACTOR_PROMPT
    assert "not permission to sum" in EXTRACTOR_PROMPT
    assert "must have exactly one root-total claim" in EXTRACTOR_PROMPT
    assert "count=null, reporting_status=not_reported, unit=other" in EXTRACTOR_PROMPT
    assert "independently inspect the complete source for a finite inventory" in VERIFIER_PROMPT
    assert "Do not replace the null with a sum" in VERIFIER_PROMPT
    assert "be copied into benchmark-metadata" in VERIFIER_PROMPT
    assert "printed author-affiliation mapping" in EXTRACTOR_PROMPT
    assert "paper's printed author-affiliation" in VERIFIER_PROMPT
    assert "funders, acknowledgements" in EXTRACTOR_PROMPT
    assert "can only become a partial BenchmarkUse" in EXTRACTOR_PROMPT
    assert "requires a partial BenchmarkUse" in VERIFIER_PROMPT
    assert "does not support a metric or result claim" in EXTRACTOR_PROMPT
    assert "extractor-error" in VERIFIER_PROMPT
    assert "source-internal" in VERIFIER_PROMPT
    assert "legacy blocking_conflicts field must always be" in VERIFIER_PROMPT


def test_generator_downgrades_incomplete_evaluation_to_partial_use() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench", "relation_type": "evaluation",
        "is_new_benchmark": False, "background_only": False,
        "claim_ids": ["claim-2", "claim-3"],
        "reporting_gaps": ["benchmark version", "realized n", "metric", "numeric result"],
    }
    records = build_records(
        verified_result(claims, mention), source=SOURCE,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    assert len(records.uses) == 1 and records.uses[0]["status"] == "partial"
    assert records.runs == []
    assert records.uses[0]["scope"]["type"] == "unknown"
    assert "numeric result" in records.uses[0]["reporting_gaps"]
    assert records.work["review_provenance"]["method"] == "automated-double-pass"
    assert records.work["source_versions"][0]["content_sha256"] == "a" * 64
    assert records.normalization_readiness == [{
        "mention_id": "mention-1",
        "benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "status": "partial-only",
        "blockers": [
            "benchmark version not reported",
            "scope is unknown",
            "exact model is not reported",
            "metric is not reported",
            "numeric result is not reported",
        ],
    }]


def test_generator_rejects_extractor_error_claim_without_blocking_partial_use() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
        claim("claim-4", "scope-n", 5),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench", "relation_type": "evaluation",
        "is_new_benchmark": False, "background_only": False,
        "claim_ids": ["claim-2", "claim-3", "claim-4"],
        "reporting_gaps": ["benchmark version", "scope", "metric", "numeric result"],
    }
    payload = verified_result(claims, mention)
    payload["verification"]["conflicts"] = [{
        "kind": "extractor-error",
        "claim_ids": ["claim-4"],
        "summary": "Five independent runs are repeats, not realized sample n.",
    }]
    next(
        item for item in payload["verification"]["claims"]
        if item["claim_id"] == "claim-4"
    )["verdict"] = "unsupported"
    records = build_records(
        payload,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-08-05",
    )
    assert records.uses[0]["status"] == "partial"
    assert records.runs == []

    source_conflict = json.loads(json.dumps(payload))
    source_conflict["verification"]["conflicts"] = [{
        "kind": "source-internal",
        "claim_ids": ["claim-3"],
        "summary": "The source contradicts the benchmark identity.",
    }]
    for item in source_conflict["verification"]["claims"]:
        if item["claim_id"] == "claim-3":
            item["verdict"] = "conflicted"
        if item["claim_id"] == "claim-4":
            item["verdict"] = "unsupported"
    with pytest.raises(GenerationBlocked, match="blocking source conflicts"):
        build_records(
            source_conflict,
            source=SOURCE,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-08-05",
        )


def test_generator_classifies_official_provider_system_card_from_source_domain() -> None:
    title = "Synthetic Provider System Card"
    claims = [
        claim("claim-1", "paper-identity", {"title": title}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
    ]
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-2", "claim-3"],
        "reporting_gaps": ["benchmark version", "realized n", "metric"],
    }
    payload = local_verified_result(claims, mention)
    payload["draft"]["paper"].update({
        "title": title,
        "authors": [],
        "organizations": ["Anthropic"],
        "doi": None,
        "canonical_url": "https://www-cdn.anthropic.com/system-card.pdf",
        "version_label": "2026-07-24",
    })
    source = {
        **SOURCE,
        "url": "https://www-cdn.anthropic.com/system-card.pdf",
    }

    records = build_records(
        payload,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )

    assert records.work["work_type"] == "system-card"
    assert records.work["source_class"] == "official_model_provider"
    assert records.work["status"] == "published"

    untrusted_source = {**source, "url": "https://example.org/system-card.pdf"}
    payload["draft"]["paper"]["canonical_url"] = untrusted_source["url"]
    untrusted_records = build_records(
        payload,
        source=untrusted_source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert untrusted_records.work["work_type"] == "preprint"
    assert untrusted_records.work["source_class"] == "independent_reproduction"


def test_generator_uses_claim_mention_id_when_redundant_claim_ids_omit_relation() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
    ]
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-3"],
        "reporting_gaps": ["benchmark version", "realized n", "metric"],
    }
    records = build_records(
        verified_result(claims, mention),
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
    )
    assert records.blocked_reasons == []
    assert records.uses[0]["relation_type"] == "evaluation"
    assert records.uses[0]["status"] == "partial"


def test_generator_creates_normalized_run_only_from_supported_numeric_claims() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
        claim("claim-4", "benchmark-version", "initial-release"),
        claim("claim-5", "scope-type", "full"),
        claim("claim-6", "scope-n", 750),
        claim("claim-7", "model", {"name": "gpt-5-2-pro", "provider": "OpenAI", "version_string": None, "release_date": None}),
        claim("claim-8", "metric", {
            "source_label": "Accuracy", "unit": "fraction", "range": [0, 1],
            "higher_is_better": True, "aggregation": "macro", "pass_threshold": None,
            "tolerance": None, "kind": "absolute", "baseline_model_name": None,
            "statistical": "95% bootstrap CI",
        }),
        claim("claim-9", "result", {
            "model_name": "gpt-5-2-pro", "metric_source_label": "Accuracy", "value": 0.72,
            "ci_low": 0.70, "ci_high": 0.74, "n": 750, "notes": None,
            "numeric_source": "table",
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench", "relation_type": "evaluation",
        "is_new_benchmark": False, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]], "reporting_gaps": [],
    }
    records = build_records(
        verified_result(claims, mention), source=SOURCE,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    assert records.uses[0]["status"] == "normalized"
    assert records.runs[0]["scope"]["n"] == 750
    assert records.runs[0]["metrics"][0]["kind"] == "absolute"
    assert records.runs[0]["results"][0]["value"] == 0.72
    assert records.runs[0]["results"][0]["evidence_ids"]
    assert records.normalization_readiness[0]["status"] == "normalized-ready"
    assert records.normalization_readiness[0]["blockers"] == []


def test_new_benchmark_requires_creator_repo_pin_and_builds_same_pr_entities() -> None:
    metadata = {
        "name": "SyntheticBioBench",
        "aliases": [
            "SYNTHETICBIOBENCH",
            "Synthetic fitness suite",
            "synthetic_fitness_suite",
        ],
        "summary": "A synthetic test-only benchmark for protein fitness prediction evaluation.",
        "kind": "dataset", "organizations": ["Example Institute"], "release_date": "2026-07-01",
        "domains": ["protein-sequence"], "capabilities": ["prediction"],
        "modalities": ["protein-sequence"], "task_formats": ["regression"],
        "access": {
            "level": "fully-open", "tasks": "All ten examples are public.",
            "artifacts": "Sequences and labels are released.", "grader": "Deterministic scorer",
            "license": "CC BY 4.0", "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "SyntheticBioBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "v1"),
        claim("claim-6", "benchmark-count", {
            "label": "total examples", "count": 10, "unit": "examples", "basis": "Released examples",
            "reporting_status": "reported", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-repository", {"url": "https://github.com/example/syntheticbiobench", "license": "CC BY 4.0"}),
        claim("claim-9", "scientific-task", {
            "task_type_id": "protein-fitness-prediction", "coverage": "explicitly-in-scope",
            "mapping_method": "official-taxonomy", "count": 10, "count_unit": "examples",
            "count_basis": "Released examples", "reporting_status": "reported", "notes": None,
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "SyntheticBioBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        # The claim's mention_id is authoritative; the model-facing claim_ids
        # summary is redundant and may omit an otherwise verified relation.
        "claim_ids": [
            item["claim_id"] for item in claims
            if item["mention_id"] and item["claim_id"] != "claim-2"
        ],
        "reporting_gaps": [],
    }
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/syntheticbiobench": {
            "kind": "commit", "value": "b" * 40,
            "url": "https://github.com/example/syntheticbiobench/commit/" + "b" * 40,
        }
    }}
    records = build_records(
        verified_result(claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    assert records.blocked_reasons == []
    assert records.work["source_class"] == "benchmark_creator"
    assert [item["id"] for item in records.benchmarks] == ["syntheticbiobench"]
    assert records.benchmarks[0]["aliases"] == ["Synthetic fitness suite"]
    assert records.benchmarks[0]["resources"][1]["pin"]["value"] == "b" * 40
    task_entry = records.classifications["syntheticbiobench"]["entries"][0]
    assert task_entry["task_type_id"] == "protein-fitness-prediction"
    assert task_entry["count_ref"] == "/task_counts/total"
    assert records.uses[0]["relation_type"] == "benchmark-creation"
    subset_status = next(
        item for item in records.benchmarks[0]["field_status"]
        if item["path"] == "/task_counts/subsets"
    )
    assert subset_status["status"] == "provisional"
    assert "did not establish an exhaustive formal-subset inventory" in subset_status["reason"]

    different_basis_claims = json.loads(json.dumps(claims))
    task_payload = json.loads(different_basis_claims[8]["value_json"])
    task_payload["count_basis"] = "Protein landscape examples"
    different_basis_claims[8]["value_json"] = json.dumps(task_payload)
    different_basis_records = build_records(
        verified_result(different_basis_claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    assert different_basis_records.blocked_reasons == []
    assert (
        different_basis_records.classifications["syntheticbiobench"]["entries"][0]["count_ref"]
        is None
    )

    conservative_claims = [
        item for item in claims
        if item["claim_type"] not in {"benchmark-version", "scientific-task"}
    ]
    conservative_records = build_records(
        verified_result(conservative_claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    conservative_benchmark = conservative_records.benchmarks[0]
    assert conservative_benchmark["latest_version"] == "initial-release"
    assert "does not report a formal benchmark version" in conservative_benchmark["versions"][0]["notes"]
    assert conservative_records.classifications["syntheticbiobench"]["entries"] == []
    assert "pending a targeted official-source audit" in (
        conservative_records.classifications["syntheticbiobench"]["notes"]
    )
    changelog = ROOT / "registry" / "changelog.yaml"
    original_changelog = changelog.read_text(encoding="utf-8")
    written: list[Path] = []
    try:
        written = write_records(records)
        validated = validate_registry()
        assert any(item["id"] == "syntheticbiobench" for item in validated["benchmark"])
    finally:
        for path in written:
            if path != changelog:
                path.unlink(missing_ok=True)
        changelog.write_text(original_changelog, encoding="utf-8")


def test_new_benchmark_omits_artifact_task_mapping_without_artifact_locator() -> None:
    metadata = {
        "name": "SyntheticWorkflowBench", "aliases": [],
        "summary": "A synthetic test-only benchmark for end-to-end scientific workflow evaluation.",
        "kind": "agentic-eval", "organizations": ["Example Institute"],
        "release_date": "2026-07-01", "domains": ["bioinformatics"],
        "capabilities": ["data-analysis", "tool-use"], "modalities": ["raw-omics"],
        "task_formats": ["agent episode"],
        "access": {
            "level": "partially-open", "tasks": "Representative examples are public.",
            "artifacts": "The full benchmark is restricted.", "grader": "Deterministic scorer",
            "license": None, "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "SyntheticWorkflowBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "v1"),
        claim("claim-6", "benchmark-count", {
            "label": "total tasks", "count": 100, "unit": "tasks", "basis": "Complete inventory",
            "reporting_status": "reported", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-repository", {
            "url": "https://github.com/example/syntheticworkflowbench", "license": None,
        }),
        claim("claim-9", "scientific-task", {
            "task_type_id": "end-to-end-computational-analysis",
            "coverage": "explicitly-in-scope", "mapping_method": "artifact-derived",
            "count": 100, "count_unit": "tasks", "count_basis": "Complete inventory",
            "reporting_status": "reported", "notes": None,
        }),
    ]
    claims[-1]["locators"] = [{
        "locator_type": "section", "value": "Benchmark construction",
        "document_page": 2, "printed_page": "2",
        "excerpt": "The paper describes workflow evaluations.",
    }]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "SyntheticWorkflowBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/syntheticworkflowbench": {
            "kind": "commit", "value": "c" * 40,
            "url": "https://github.com/example/syntheticworkflowbench/commit/" + "c" * 40,
        }
    }}

    records = build_records(
        verified_result(claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )

    assert records.blocked_reasons == []
    classification = records.classifications["syntheticworkflowbench"]
    assert classification["status"] == "partial"
    assert classification["entries"] == []
    assert "artifact-level locator" in classification["notes"]
    assert not any(
        evidence["id"].startswith("syntheticworkflowbench-automated-task-")
        for evidence in records.benchmarks[0]["evidence"]
    )


def test_new_benchmark_count_roles_filter_auxiliary_and_enforce_subset_semantics() -> None:
    metadata = {
        "name": "CountRoleBench", "aliases": [],
        "summary": "A synthetic benchmark used to validate root, subset, and auxiliary counts.",
        "kind": "dataset", "organizations": ["Example Institute"],
        "release_date": "2026-07-01", "domains": ["genomics"],
        "capabilities": ["prediction"], "modalities": ["dna-rna-sequence"],
        "task_formats": ["regression"],
        "access": {
            "level": "fully-open", "tasks": "All examples are public.",
            "artifacts": "Examples and labels are released.",
            "grader": "Deterministic scorer", "license": "CC BY 4.0",
            "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "CountRoleBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-count", {
            "label": "complete released examples", "count": 10, "unit": "examples",
            "basis": "Complete released inventory", "reporting_status": "reported",
            "count_role": "root-total", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
        claim("claim-6", "benchmark-count", {
            "label": "public examples", "count": 4, "unit": "examples",
            "basis": "Source-defined public subset", "reporting_status": "reported",
            "count_role": "formal-subset", "subset_id": "public", "exclusive": True,
            "exhaustive": False, "partition_group": "access",
        }),
        claim("claim-7", "benchmark-count", {
            "label": "target genes", "count": 4, "unit": "genes",
            "basis": "Auxiliary target annotation", "reporting_status": "reported",
            "count_role": "auxiliary", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
        claim("claim-8", "creator-source", {"url": "https://doi.org/10.9999/count-role.1"}),
        claim("claim-9", "official-repository", {
            "url": "https://github.com/example/countrolebench", "license": "CC BY 4.0",
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "CountRoleBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/countrolebench": {
            "kind": "commit", "value": "e" * 40,
            "url": "https://github.com/example/countrolebench/commit/" + "e" * 40,
        }
    }}

    records = build_records(
        verified_result(claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )
    benchmark = records.benchmarks[0]
    assert benchmark["task_counts"]["total"] == 10
    assert [item["id"] for item in benchmark["task_counts"]["subsets"]] == ["public"]
    assert all(item["count"] != 4 or item["id"] == "public" for item in benchmark["task_counts"]["subsets"])
    subset_evidence = next(
        item for item in benchmark["evidence"]
        if item["id"].endswith("automated-subset-1-evidence")
    )
    assert subset_evidence["supports"] == [
        "/task_counts/subsets", "/versions/0/task_counts/subsets",
        "/task_counts/subsets/0", "/versions/0/task_counts/subsets/0",
    ]
    assert subset_evidence["id"] in benchmark["versions"][0]["evidence_ids"]

    duplicate = json.loads(json.dumps(claims))
    duplicate.append(claim("claim-10", "benchmark-count", {
        "label": "public examples repeated", "count": 4, "unit": "examples",
        "basis": "Duplicate measurement", "reporting_status": "reported",
        "count_role": "formal-subset", "subset_id": "public", "exclusive": True,
        "exhaustive": False, "partition_group": "access",
    }))
    duplicate_records = build_records(
        verified_result(duplicate, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )
    assert "duplicate formal subset ID" in "; ".join(duplicate_records.blocked_reasons)

    second_root = json.loads(json.dumps(claims))
    second_root.append(claim("claim-10", "benchmark-count", {
        "label": "another claimed total", "count": 4, "unit": "genes",
        "basis": "Wrong entity total", "reporting_status": "reported",
        "count_role": "root-total", "subset_id": None, "exclusive": False,
        "exhaustive": False, "partition_group": None,
    }))
    second_root_records = build_records(
        verified_result(second_root, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )
    assert "exactly one verified root-total" in "; ".join(second_root_records.blocked_reasons)

    mixed_unit = json.loads(json.dumps(claims))
    mixed_payload = json.loads(mixed_unit[5]["value_json"])
    mixed_payload["unit"] = "genes"
    mixed_unit[5]["value_json"] = json.dumps(mixed_payload)
    mixed_unit_records = build_records(
        verified_result(mixed_unit, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )
    assert "formal subset count unit differs" in "; ".join(mixed_unit_records.blocked_reasons)


def test_new_benchmark_rejects_uncontrolled_scientific_task_count_unit() -> None:
    from extract_paper import _validate_draft_structure

    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "CountUnitBench"),
        claim("claim-4", "benchmark-count", {
            "label": "complete examples", "count": 10, "unit": "examples",
            "basis": "Complete inventory", "reporting_status": "reported",
            "count_role": "root-total", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
        claim("claim-5", "scientific-task", {
            "task_type_id": "crispr-guide-activity-prediction",
            "coverage": "explicitly-in-scope", "mapping_method": "artifact-derived",
            "count": None, "count_unit": "genes", "count_basis": "Target genes",
            "reporting_status": "not_reported", "notes": None,
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "CountUnitBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    with pytest.raises(PaperExtractionError, match="count_unit must use the controlled"):
        _validate_draft_structure(PaperEvidenceDraft.model_validate(draft_payload(claims, mention)))


def test_new_scenario_matrix_benchmark_accepts_verified_unreported_root_total() -> None:
    metadata = {
        "name": "ScenarioMatrixBench", "aliases": [],
        "summary": "A reusable simulation matrix without one finite benchmark item inventory.",
        "kind": "dataset", "organizations": ["Example Institute"],
        "release_date": "2026-07-01", "domains": ["transcriptomics"],
        "capabilities": ["data-analysis"], "modalities": ["raw-omics"],
        "task_formats": ["simulation"],
        "access": {
            "level": "fully-open", "tasks": "Scenarios are generated from public code.",
            "artifacts": "The simulator and configurations are public.",
            "grader": "Scenario-specific deterministic metrics", "license": "GPL-3.0",
            "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "ScenarioMatrixBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-count", {
            "label": "finite root item inventory", "count": None, "unit": "other",
            "basis": "The source defines a scenario matrix and reports no single finite item inventory.",
            "reporting_status": "not_reported", "count_role": "root-total",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-6", "creator-source", {"url": "https://doi.org/10.9999/scenario.1"}),
        claim("claim-7", "official-repository", {
            "url": "https://github.com/example/scenariomatrixbench", "license": "GPL-3.0",
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "ScenarioMatrixBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": ["finite root benchmark item total"],
    }
    source = {**SOURCE, "bibliographic_metadata": {
        "metadata_source": "Crossref",
        "publication_date": "2025-09-11",
    }, "repository_pins": {
        "https://github.com/example/scenariomatrixbench": {
            "kind": "commit", "value": "f" * 40,
            "url": "https://github.com/example/scenariomatrixbench/commit/" + "f" * 40,
        }
    }}

    result = verified_result(claims, mention)
    result["draft"]["paper"]["publication_date"] = "2025"
    records = build_records(
        result, source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )

    assert records.blocked_reasons == []
    benchmark = records.benchmarks[0]
    assert benchmark["task_counts"] == {
        "total": None,
        "basis": "The source defines a scenario matrix and reports no single finite item inventory.",
        "reporting_status": "not_reported",
        "subsets": [],
    }
    assert benchmark["versions"][0]["task_counts"] == benchmark["task_counts"]
    assert records.work["publication_date"] == "2025-09-11"
    assert records.work["source_versions"][0]["publication_date"] == "2025-09-11"


def test_extractor_requires_explicit_consistent_benchmark_count_roles() -> None:
    from extract_paper import _validate_draft_structure

    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "CountRoleBench"),
        claim("claim-4", "benchmark-count", {
            "label": "complete examples", "count": 10, "unit": "examples",
            "basis": "Complete inventory", "reporting_status": "reported",
            "count_role": "root-total", "subset_id": None, "exclusive": False,
            "exhaustive": False, "partition_group": None,
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "CountRoleBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    _validate_draft_structure(PaperEvidenceDraft.model_validate(draft_payload(claims, mention)))

    missing_role = json.loads(json.dumps(claims))
    payload = json.loads(missing_role[3]["value_json"])
    payload.pop("count_role")
    missing_role[3]["value_json"] = json.dumps(payload)
    with pytest.raises(PaperExtractionError, match="must declare root-total"):
        _validate_draft_structure(
            PaperEvidenceDraft.model_validate(draft_payload(missing_role, mention))
        )

    inconsistent_subset = json.loads(json.dumps(claims))
    payload = json.loads(inconsistent_subset[3]["value_json"])
    payload.update({"count_role": "formal-subset", "subset_id": None})
    inconsistent_subset[3]["value_json"] = json.dumps(payload)
    with pytest.raises(PaperExtractionError, match="requires subset_id"):
        _validate_draft_structure(
            PaperEvidenceDraft.model_validate(draft_payload(inconsistent_subset, mention))
        )


def test_new_benchmark_accepts_versioned_official_dataset_and_maps_alias_use() -> None:
    metadata = {
        "name": "SyntheticGuideBench", "aliases": ["Synthetic guide benchmark"],
        "summary": "A synthetic test-only guide-library benchmark with a versioned data release.",
        "kind": "dataset", "organizations": ["Example Institute"],
        "release_date": "2026-07-01", "domains": ["genomics"],
        "capabilities": ["prediction"], "modalities": ["dna-rna-sequence"],
        "task_formats": ["regression"],
        "access": {
            "tasks": "All guide records are public.",
            "artifacts": "Guide sequences and labels are released.",
            "grader": "Not reported", "license": None, "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "SyntheticGuideBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "v1.0.0"),
        claim("claim-6", "benchmark-count", {
            "label": "released guides", "count": 100, "unit": "records",
            "basis": "Released guide records", "reporting_status": "reported",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-resource", {
            "url": "https://doi.org/10.5281/zenodo.1234567",
            "resource_type": "dataset", "license": None, "version": None,
        }),
        claim("claim-9", "relation", "evaluation", mention_id="mention-2"),
        claim("claim-10", "benchmark-identity", "Synthetic guide benchmark", mention_id="mention-2"),
        claim("claim-11", "scope-type", "unknown", mention_id="mention-2"),
        claim("claim-12", "relation", "evaluation", mention_id="mention-3"),
        claim("claim-13", "benchmark-identity", "Unresolved guide collection", mention_id="mention-3"),
    ]
    creation = {
        "mention_id": "mention-1", "benchmark_name": "SyntheticGuideBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"] == "mention-1"],
        "reporting_gaps": [],
    }
    evaluation = {
        "mention_id": "mention-2", "benchmark_name": "Synthetic guide benchmark",
        "registry_benchmark_id": None, "relation_type": "evaluation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"] == "mention-2"],
        "reporting_gaps": ["benchmark version", "realized n", "model", "metric"],
    }
    unresolved_evaluation = {
        "mention_id": "mention-3", "benchmark_name": "Unresolved guide collection",
        "registry_benchmark_id": None, "relation_type": "evaluation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"] == "mention-3"],
        "reporting_gaps": ["benchmark identity"],
    }
    payload = verified_result(claims, creation)
    payload["draft"]["benchmark_mentions"] = [creation, evaluation, unresolved_evaluation]
    source = {**SOURCE, "resource_pins": {
        "https://doi.org/10.5281/zenodo.1234567": {
            "resource_type": "dataset", "kind": "version", "value": "1.0.0",
            "url": "https://zenodo.org/records/1234567",
            "resolved_url": "https://zenodo.org/records/1234567",
            "license": "cc-by-4.0",
        }
    }}
    records = build_records(
        payload, source=source, generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-30",
    )
    assert records.blocked_reasons == []
    assert records.omitted_unresolved_mentions == ["Unresolved guide collection"]
    benchmark = records.benchmarks[0]
    assert benchmark["resources"][1]["type"] == "dataset"
    assert benchmark["resources"][1]["pin"] == {
        "kind": "version", "value": "1.0.0",
        "url": "https://zenodo.org/records/1234567",
    }
    assert benchmark["implementations"] == []
    assert benchmark["access"]["level"] == "partially-open"
    assert benchmark["access"]["license"] is None
    license_status = next(
        item for item in benchmark["field_status"]
        if item["path"] == "/access/license"
    )
    assert license_status["status"] == "provisional"
    assert [item["relation_type"] for item in records.uses] == [
        "benchmark-creation", "evaluation",
    ]
    assert records.uses[1]["status"] == "partial"


def test_new_benchmark_evaluation_scope_count_conflict_downgrades_to_partial_use() -> None:
    metadata = {
        "name": "SyntheticGuideBench", "aliases": [],
        "summary": "A synthetic test-only guide-library benchmark.",
        "kind": "dataset", "organizations": ["Example Institute"],
        "release_date": "2026-07-01", "domains": ["genomics"],
        "capabilities": ["prediction"], "modalities": ["dna-rna-sequence"],
        "task_formats": ["regression"],
        "access": {
            "level": "fully-open", "tasks": "The library is public.",
            "artifacts": "Guide sequences and labels are released.",
            "grader": "Not reported", "license": "CC BY 4.0",
            "biosafety_notes": None,
        },
    }
    creation_claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "SyntheticGuideBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "v1"),
        claim("claim-6", "benchmark-count", {
            "label": "released guides", "count": 100, "unit": "records",
            "basis": "Released guide records", "reporting_status": "reported",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-repository", {
            "url": "https://github.com/example/syntheticguidebench",
            "license": "CC BY 4.0",
        }),
    ]
    evaluation_claims = [
        claim("claim-9", "relation", "evaluation", mention_id="mention-2"),
        claim("claim-10", "benchmark-identity", "SyntheticGuideBench", mention_id="mention-2"),
        # The extractor mistakes the number of cell lines for the realized
        # benchmark size; the verifier must remove it without weakening the
        # independently verified creator record.
        claim("claim-11", "scope-n", 4, mention_id="mention-2"),
    ]
    creation_mention = {
        "mention_id": "mention-1", "benchmark_name": "SyntheticGuideBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in creation_claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    evaluation_mention = {
        "mention_id": "mention-2", "benchmark_name": "SyntheticGuideBench",
        "registry_benchmark_id": None, "relation_type": "evaluation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in evaluation_claims],
        "reporting_gaps": [],
    }
    claims = [*creation_claims, *evaluation_claims]
    payload = verified_result(claims, creation_mention)
    payload["draft"]["benchmark_mentions"] = [creation_mention, evaluation_mention]
    payload["verification"]["blocking_conflicts"] = [
        "claim-11: The printed value 4 counts cell lines, not the realized benchmark scope n."
    ]
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-11":
            item.update({"verdict": "conflicted", "confidence": "high"})
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/syntheticguidebench": {
            "kind": "commit", "value": "d" * 40,
            "url": "https://github.com/example/syntheticguidebench/commit/" + "d" * 40,
        }
    }}

    records = build_records(
        payload, source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )

    assert records.blocked_reasons == []
    assert [benchmark["id"] for benchmark in records.benchmarks] == ["syntheticguidebench"]
    evaluation_use = next(use for use in records.uses if use["relation_type"] == "evaluation")
    assert evaluation_use["status"] == "partial"
    assert evaluation_use["scope"]["n"] is None
    assert evaluation_use["evaluation_run_ids"] == []
    assert any("Conflicted scope-n claim omitted" in gap for gap in evaluation_use["reporting_gaps"])


def test_new_benchmark_atomic_metadata_keeps_independently_supported_fields() -> None:
    atomic_values = {
        "/name": "AtomicBioBench",
        "/aliases": [],
        "/summary": "A synthetic benchmark for independently verified protein fitness prediction.",
        "/kind": "dataset",
        "/organizations": ["Example Institute"],
        "/release_date": "2026-07-01",
        "/domains": ["protein-sequence"],
        "/capabilities": ["prediction"],
        "/modalities": ["protein-sequence"],
        "/task_formats": ["regression"],
        "/access/level": "fully-open",
        "/access/tasks": "All examples are public.",
        "/access/artifacts": "Sequences and labels are released.",
        "/access/grader": "Deterministic scorer",
        "/access/license": "CC BY 4.0",
        "/access/biosafety_notes": None,
    }
    metadata_claims = [
        claim(
            f"claim-{index}",
            "benchmark-metadata",
            value,
            field_path=f"/benchmark-metadata{path}",
        )
        for index, (path, value) in enumerate(atomic_values.items(), 4)
    ]
    next_id = 4 + len(metadata_claims)
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "AtomicBioBench"),
        *metadata_claims,
        claim(f"claim-{next_id}", "benchmark-count", {
            "label": "total examples", "count": 10, "unit": "examples",
            "basis": "Released examples", "reporting_status": "reported",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim(f"claim-{next_id + 1}", "creator-source", {"url": "https://doi.org/10.9999/atomic.1"}),
        claim(f"claim-{next_id + 2}", "official-repository", {
            "url": "https://github.com/example/atomicbiobench", "license": "CC BY 4.0",
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "AtomicBioBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    optional_claim = next(
        item for item in payload["draft"]["claims"]
        if item["field_path"] == "/benchmark-metadata/access/biosafety_notes"
    )
    optional_claim["confidence"] = "medium"
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == optional_claim["claim_id"]:
            item.update({"verdict": "not-verifiable", "confidence": "high"})
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/atomicbiobench": {
            "kind": "commit", "value": "d" * 40,
            "url": "https://github.com/example/atomicbiobench/commit/" + "d" * 40,
        }
    }}
    records = build_records(
        payload, source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-29",
    )
    assert records.blocked_reasons == []
    benchmark = records.benchmarks[0]
    assert benchmark["name"] == "AtomicBioBench"
    assert benchmark["access"]["biosafety_notes"] is None
    metadata_evidence = [
        item for item in benchmark["evidence"]
        if "-automated-metadata-" in item["id"]
    ]
    assert metadata_evidence
    assert all(len(item["supports"]) == 1 for item in metadata_evidence)
    assert {item["supports"][0] for item in metadata_evidence} >= {
        "/name", "/summary", "/kind", "/organizations", "/release_date",
    }
    assert "/access/biosafety_notes" not in {
        item["supports"][0] for item in metadata_evidence
    }

    missing_license_payload = json.loads(json.dumps(payload))
    license_claim_id = next(
        item["claim_id"] for item in missing_license_payload["draft"]["claims"]
        if item["field_path"] == "/benchmark-metadata/access/license"
    )
    for item in missing_license_payload["verification"]["claims"]:
        if item["claim_id"] == license_claim_id:
            item.update({"verdict": "not-verifiable", "confidence": "high"})
    repository_claim = next(
        item for item in missing_license_payload["draft"]["claims"]
        if item["claim_type"] == "official-repository"
    )
    repository_value = json.loads(repository_claim["value_json"])
    repository_value["license"] = None
    repository_claim["value_json"] = json.dumps(repository_value)
    missing_license_records = build_records(
        missing_license_payload,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-29",
    )
    missing_license_benchmark = missing_license_records.benchmarks[0]
    assert missing_license_benchmark["access"]["license"] is None
    assert missing_license_benchmark["audit"]["status"] == "audited-with-caveats"
    license_status = next(
        item for item in missing_license_benchmark["field_status"]
        if item["path"] == "/access/license"
    )
    assert license_status["status"] == "provisional"
    license_evidence = next(
        item for item in missing_license_benchmark["evidence"]
        if item["id"] == "atomicbiobench-automated-resource-evidence"
    )
    assert "/access/license" in license_evidence["supports"]


@pytest.mark.parametrize("metadata_source", ["Crossref", "arXiv API"])
def test_atomic_metadata_uses_canonical_bibliographic_date_and_unclassified_format(
    metadata_source: str,
) -> None:
    values = {
        "/name": "BibliographicDateBench",
        "/summary": "A synthetic benchmark whose release date comes from canonical metadata.",
        "/kind": "dataset",
        "/organizations": ["Example Institute"],
        "/domains": ["protein-sequence"],
        "/capabilities": ["prediction"],
        "/modalities": ["protein-sequence"],
        "/access/level": "fully-open",
    }
    claims = [
        EvidenceClaimDraft.model_validate(claim(
            f"claim-{index}", "benchmark-metadata", value,
            field_path=f"/benchmark-metadata{path}",
        ))
        for index, (path, value) in enumerate(values.items(), 1)
    ]
    metadata, evidence_claims, bibliographic_supports = _materialize_benchmark_metadata(
        claims,
        bibliographic_metadata={
            "metadata_source": metadata_source,
            "publication_date": "2024-12-03",
        },
    )
    assert metadata["release_date"] == "2024-12-03"
    assert metadata["task_formats"] == ["unclassified"]
    assert bibliographic_supports == ["/release_date"]
    assert "/release_date" not in {
        support
        for _claim, supports in evidence_claims
        for support in supports
    }


def test_atomic_metadata_replaces_year_only_claim_with_complete_bibliographic_date() -> None:
    values = {
        "/name": "YearOnlyDateBench",
        "/summary": "A synthetic benchmark with only a year printed in its PDF header.",
        "/kind": "dataset",
        "/organizations": ["Example Institute"],
        "/release_date": "2025",
        "/domains": ["transcriptomics"],
        "/capabilities": ["data-analysis"],
        "/modalities": ["raw-omics"],
        "/access/level": "fully-open",
    }
    claims = [
        EvidenceClaimDraft.model_validate(claim(
            f"claim-{index}", "benchmark-metadata", value,
            field_path=f"/benchmark-metadata{path}",
        ))
        for index, (path, value) in enumerate(values.items(), 1)
    ]

    metadata, evidence_claims, bibliographic_supports = _materialize_benchmark_metadata(
        claims,
        bibliographic_metadata={
            "metadata_source": "Crossref",
            "publication_date": "2025-09-11",
        },
    )

    assert metadata["release_date"] == "2025-09-11"
    assert bibliographic_supports == ["/release_date"]
    assert "/release_date" not in {
        support
        for _claim, supports in evidence_claims
        for support in supports
    }


def test_new_benchmark_normalizes_verified_access_text_lists() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "AccessListBench"),
    ]
    metadata_values = {
        "/name": "AccessListBench",
        "/summary": "A synthetic benchmark used to test access description normalization.",
        "/kind": "suite",
        "/organizations": ["Example Institute"],
        "/release_date": "2026-07-01",
        "/domains": ["transcriptomics"],
        "/capabilities": ["data-analysis"],
        "/modalities": ["raw-omics"],
        "/access/level": "fully-open",
        "/access/tasks": ["simulation code", "analysis code"],
        "/access/artifacts": ["configurations", "plot scripts"],
        "/access/grader": "Deterministic scenario metrics",
    }
    for path, value in metadata_values.items():
        claims.append(claim(
            f"claim-{len(claims) + 1}", "benchmark-metadata", value,
            field_path=f"/benchmark-metadata{path}",
        ))
    claims.extend([
        claim(f"claim-{len(claims) + 1}", "benchmark-count", {
            "label": "finite root item inventory", "count": None, "unit": "other",
            "basis": "The source reports no single finite item inventory.",
            "reporting_status": "not_reported", "count_role": "root-total",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim(f"claim-{len(claims) + 2}", "creator-source", {
            "url": "https://doi.org/10.9999/access-list.1",
        }),
        claim(f"claim-{len(claims) + 3}", "official-repository", {
            "url": "https://github.com/example/access-list-bench", "license": "GPL-3.0",
        }),
    ])
    mention = {
        "mention_id": "mention-1", "benchmark_name": "AccessListBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": ["finite root benchmark item total"],
    }
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/access-list-bench": {
            "kind": "commit", "value": "d" * 40,
            "url": "https://github.com/example/access-list-bench/commit/" + "d" * 40,
        }
    }}

    records = build_records(
        verified_result(claims, mention), source=source,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-30",
    )

    assert records.blocked_reasons == []
    access = records.benchmarks[0]["access"]
    assert access["tasks"] == "simulation code; analysis code"
    assert access["artifacts"] == "configurations; plot scripts"


def test_atomic_dataset_metadata_can_use_conservative_resolved_access_floor() -> None:
    values = {
        "/name": "DatasetAccessBench",
        "/summary": "A synthetic benchmark with a publicly resolved official dataset artifact.",
        "/kind": "dataset",
        "/organizations": ["Example Institute"],
        "/release_date": "2026-07-30",
        "/domains": ["genomics"],
        "/capabilities": ["prediction"],
        "/modalities": ["dna-rna-sequence"],
    }
    claims = [
        EvidenceClaimDraft.model_validate(claim(
            f"claim-{index}", "benchmark-metadata", value,
            field_path=f"/benchmark-metadata{path}",
        ))
        for index, (path, value) in enumerate(values.items(), 1)
    ]
    metadata, evidence_claims, _ = _materialize_benchmark_metadata(
        claims,
        default_access_level="partially-open",
    )
    assert metadata["access"]["level"] == "partially-open"
    assert "/access/level" not in {
        support
        for _claim, supports in evidence_claims
        for support in supports
    }


def test_new_benchmark_atomic_metadata_rejects_missing_required_field_only() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "AtomicMissingBench"),
    ]
    values = {
        "/name": "AtomicMissingBench",
        "/summary": "A synthetic benchmark summary whose rejection must not hide other fields.",
        "/kind": "dataset",
        "/organizations": ["Example Institute"],
        "/release_date": "2026-07-01",
        "/domains": ["protein-sequence"],
        "/capabilities": ["prediction"],
        "/modalities": ["protein-sequence"],
        "/task_formats": ["regression"],
        "/access/level": "fully-open",
    }
    for index, (path, value) in enumerate(values.items(), 4):
        claims.append(claim(
            f"claim-{index}", "benchmark-metadata", value,
            field_path=f"/benchmark-metadata{path}",
        ))
    offset = 4 + len(values)
    claims.extend([
        claim(f"claim-{offset}", "benchmark-count", {
            "label": "total examples", "count": 10, "unit": "examples",
            "basis": "Released examples", "reporting_status": "reported",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim(f"claim-{offset + 1}", "creator-source", {"url": "https://doi.org/10.9999/atomic.2"}),
        claim(f"claim-{offset + 2}", "official-repository", {
            "url": "https://github.com/example/atomicmissingbench", "license": None,
        }),
    ])
    mention = {
        "mention_id": "mention-1", "benchmark_name": "AtomicMissingBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    summary_claim = next(
        item for item in payload["draft"]["claims"]
        if item["field_path"] == "/benchmark-metadata/summary"
    )
    summary_claim["confidence"] = "medium"
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == summary_claim["claim_id"]:
            item.update({"verdict": "not-verifiable", "confidence": "high"})
    records = build_records(
        payload, source=SOURCE,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-29",
    )
    assert records.benchmarks == []
    reason = next(item for item in records.blocked_reasons if "metadata fields" in item)
    assert "/summary" in reason
    assert "/name" not in reason
    assert "AtomicMissingBench" not in reason.split("claim gate:")[-1]


def test_new_benchmark_without_creator_repository_pin_stops_production() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "UnpinnedBench"),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "UnpinnedBench", "registry_benchmark_id": None,
        "relation_type": "benchmark-creation", "is_new_benchmark": True, "background_only": False,
        "claim_ids": ["claim-2", "claim-3"], "reporting_gaps": [],
    }
    records = build_records(
        verified_result(claims, mention), source=SOURCE,
        generated_at=SOURCE["retrieved_at"], verified_on="2026-07-22",
    )
    assert records.benchmarks == []
    assert any("lacks verified claims" in reason for reason in records.blocked_reasons)
    diagnostic = next(reason for reason in records.blocked_reasons if "claim gate:" in reason)
    assert "benchmark-count[absent]" in diagnostic
    assert "benchmark-metadata[absent]" in diagnostic
    assert "creator-source[absent]" in diagnostic
    assert "official-repository[absent]" in diagnostic
    assert "Synthetic benchmark" not in diagnostic


def test_new_benchmark_claim_gate_diagnostic_contains_status_not_values() -> None:
    claims = [
        claim(
            "claim-1",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper"},
            mention_id=None,
        ),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "PrivateValueBench"),
        claim("claim-4", "benchmark-metadata", {
            "name": "PrivateValueBench",
            "aliases": [],
            "summary": "Sensitive synthesized summary that must not appear in diagnostics.",
            "kind": "dataset",
            "organizations": ["Example"],
            "release_date": "2026-07-01",
            "domains": ["single-cell"],
            "capabilities": ["data-analysis"],
            "modalities": ["raw-omics"],
            "task_formats": ["analysis"],
            "access": {
                "level": "partially-open", "tasks": "Sensitive task description",
                "artifacts": "Sensitive artifact description", "grader": "Not reported",
                "license": None, "biosafety_notes": None,
            },
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "PrivateValueBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-4":
            item.update({"verdict": "not-verifiable", "confidence": "medium", "locator": None})
    records = build_records(
        payload,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-25",
    )
    diagnostic = next(reason for reason in records.blocked_reasons if "claim gate:" in reason)
    assert (
        "benchmark-metadata[extractor=high/verdict=not-verifiable/"
        "verifier=medium/locator=unresolved]"
    ) in diagnostic
    assert "Sensitive" not in diagnostic
    assert "PrivateValueBench" not in diagnostic


def test_owner_conflict_resolution_requires_exact_owner_command() -> None:
    issue = {
        "comments": [
            {
                "author": {"login": "outside-contributor"},
                "body": "/resolve-paper-conflict benchmark-total=12 exclude=benchmark-subcounts",
                "createdAt": "2026-07-25T00:00:00Z",
            },
            {
                "author": {"login": "wang422003"},
                "body": "/resolve-paper-conflict benchmark-total=394 exclude=benchmark-subcounts",
                "createdAt": "2026-07-25T01:00:00Z",
            },
        ],
    }
    assert _owner_conflict_resolution(issue) == {
        "benchmark_total": 394,
        "exclude": "benchmark-subcounts",
        "exclude_creator_evaluation": False,
        "approved_by": "wang422003",
        "approved_at": "2026-07-25T01:00:00Z",
    }
    issue["comments"][1]["body"] = (
        "/resolve-paper-conflict benchmark-total=394 "
        "exclude=benchmark-subcounts,creator-evaluation"
    )
    assert _owner_conflict_resolution(issue)["exclude_creator_evaluation"] is True
    issue["comments"][1]["body"] = "/resolve-paper-conflict benchmark-total=394 exclude=anything"
    assert _owner_conflict_resolution(issue) is None
    issue["comments"] = [{
        "author": {"login": "wang422003"},
        "body": (
            "/resolve-paper-conflict benchmark-total=not-reported "
            "exclude=creator-evaluation"
        ),
        "createdAt": "2026-07-25T02:00:00Z",
    }]
    assert _owner_conflict_resolution(issue) == {
        "benchmark_total": None,
        "exclude": "creator-evaluation",
        "exclude_creator_evaluation": True,
        "approved_by": "wang422003",
        "approved_at": "2026-07-25T02:00:00Z",
    }
    issue["comments"].append({
        "author": {"login": "wang422003"},
        "body": "/resolve-paper-metadata benchmark-kind=suite status=provisional",
        "createdAt": "2026-07-25T03:00:00Z",
    })
    assert _owner_conflict_resolution(issue) == {
        "benchmark_total": None,
        "exclude": "creator-evaluation",
        "exclude_creator_evaluation": True,
        "approved_by": "wang422003",
        "approved_at": "2026-07-25T02:00:00Z",
        "provisional_benchmark_kind": "suite",
        "provisional_kind_status": "provisional",
        "provisional_kind_approved_at": "2026-07-25T03:00:00Z",
    }
    issue["comments"].append({
        "author": {"login": "wang422003"},
        "body": "/resolve-paper-metadata benchmark-access=fully-open status=provisional",
        "createdAt": "2026-07-25T04:00:00Z",
    })
    assert _owner_conflict_resolution(issue) == {
        "benchmark_total": None,
        "exclude": "creator-evaluation",
        "exclude_creator_evaluation": True,
        "approved_by": "wang422003",
        "approved_at": "2026-07-25T02:00:00Z",
        "provisional_benchmark_kind": "suite",
        "provisional_kind_status": "provisional",
        "provisional_kind_approved_at": "2026-07-25T03:00:00Z",
        "provisional_access_level": "fully-open",
        "provisional_access_status": "provisional",
        "provisional_access_approved_at": "2026-07-25T04:00:00Z",
    }
    issue["comments"].pop()
    issue["comments"].pop()
    issue["comments"][0]["body"] = (
        "/resolve-paper-conflict benchmark-total=not-reported "
        "exclude=benchmark-subcounts,creator-evaluation"
    )
    assert _owner_conflict_resolution(issue) is None

    with pytest.raises(LocalIntakeError, match="requires a conflict-resolution command"):
        _owner_conflict_resolution({"comments": [{
            "author": {"login": "wang422003"},
            "body": "/resolve-paper-metadata benchmark-access=fully-open status=provisional",
            "createdAt": "2026-07-25T05:00:00Z",
        }]})

    with pytest.raises(LocalIntakeError, match="requires the not-reported"):
        _owner_conflict_resolution({"comments": [
            {
                "author": {"login": "wang422003"},
                "body": "/resolve-paper-conflict benchmark-total=12 exclude=benchmark-subcounts",
                "createdAt": "2026-07-25T06:00:00Z",
            },
            {
                "author": {"login": "wang422003"},
                "body": "/resolve-paper-metadata benchmark-access=fully-open status=provisional",
                "createdAt": "2026-07-25T07:00:00Z",
            },
        ]})


def test_existing_pr_dedup_only_queries_open_pull_requests() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps([{
                "headRefName": "paper-intake/example-paper-142",
                "url": "https://github.com/SpectrAI-Initiative/bio-benchmark-atlas/pull/999",
            }]),
            "",
        )

    assert _existing_pr(142, runner=runner) == (
        "https://github.com/SpectrAI-Initiative/bio-benchmark-atlas/pull/999"
    )
    assert "--state" in commands[0]
    assert commands[0][commands[0].index("--state") + 1] == "open"


def test_local_intake_only_creates_missing_issue_labels() -> None:
    existing = [
        "paper-candidate",
        "ready-for-local-intake",
        "local-intake-in-progress",
        "needs-human-review",
        "intake-failed",
    ]
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps([{"name": name} for name in existing]), "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    _ensure_labels(runner=runner)

    mutations = [command for command in calls if command[:3] == ["gh", "label", "create"]]
    assert len(mutations) == 1
    assert mutations[0][3] == "paper-intake-pr"
    assert "--force" not in mutations[0]


def test_local_intake_retries_transient_gh_failure_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return subprocess.CompletedProcess(
                command, 1, "", 'Post "https://api.github.com/graphql": EOF',
            )
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr("local_paper_intake.time.sleep", sleeps.append)
    completed = _run(["gh", "issue", "view", "69"], runner=runner)
    assert completed.stdout == "ok\n"
    assert attempts == 3
    assert sleeps == [1, 2]

    auth_attempts = 0

    def auth_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal auth_attempts
        auth_attempts += 1
        return subprocess.CompletedProcess(command, 1, "", "authentication failed")

    with pytest.raises(LocalIntakeError, match="authentication failed"):
        _run(["gh", "auth", "status"], runner=auth_runner)
    assert auth_attempts == 1


def test_owner_can_preserve_supported_root_total_but_not_conflicted_subcounts() -> None:
    metadata = {
        "name": "ConflictCountBench",
        "aliases": [],
        "summary": "A synthetic benchmark whose appendix inventory conflicts with its supported root total.",
        "kind": "dataset",
        "organizations": ["Example Institute"],
        "release_date": "2026-07-01",
        "domains": ["single-cell"],
        "capabilities": ["data-analysis"],
        "modalities": ["raw-omics"],
        "task_formats": ["agent episode"],
        "access": {
            "level": "partially-open",
            "tasks": "Representative examples are public.",
            "artifacts": "The full benchmark is withheld.",
            "grader": "Deterministic grader",
            "license": "Apache-2.0",
            "biosafety_notes": None,
        },
    }
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "ConflictCountBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "paper-v1"),
        claim("claim-6", "benchmark-count", {
            "label": "total problems",
            "count": 394,
            "unit": "problems",
            "basis": "Problems used in the creator evaluation",
            "reporting_status": "reported",
            "subset_id": None,
            "exclusive": False,
            "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-repository", {
            "url": "https://github.com/example/conflictcountbench",
            "license": "Apache-2.0",
        }),
        claim("claim-9", "scientific-task", {
            "task_type_id": "cell-type-annotation",
            "coverage": "explicitly-in-scope",
            "mapping_method": "official-taxonomy",
            "count": None,
            "count_unit": "problems",
            "count_basis": "Appendix inventory is conflicted",
            "reporting_status": "not_reported",
            "notes": "Coverage is explicit; its count is intentionally omitted.",
        }),
        claim("claim-10", "benchmark-count", {
            "label": "appendix platform inventory",
            "count": 390,
            "unit": "problems",
            "basis": "Conflicted appendix inventory",
            "reporting_status": "reported",
            "subset_id": "appendix-inventory",
            "exclusive": False,
            "exhaustive": False,
            "partition_group": None,
        }),
    ]
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "ConflictCountBench",
        "registry_benchmark_id": None,
        "relation_type": "benchmark-creation",
        "is_new_benchmark": True,
        "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": ["appendix inventory subcounts conflict with the supported root total"],
    }
    payload = verified_result(claims, mention)
    payload["verification"]["blocking_conflicts"] = ["Root total and appendix inventory disagree."]
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-10":
            item.update({
                "verdict": "conflicted",
                "confidence": "high",
                "locator": locator() | {"value": "Appendix Table 7"},
            })
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/conflictcountbench": {
            "kind": "commit",
            "value": "c" * 40,
            "url": "https://github.com/example/conflictcountbench/commit/" + "c" * 40,
        }
    }}
    with pytest.raises(GenerationBlocked, match="blocking source conflicts"):
        build_records(
            payload,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
        )
    records = build_records(
        payload,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-25",
        owner_conflict_resolution={
            "benchmark_total": 394,
            "exclude": "benchmark-subcounts",
            "approved_by": "wang422003",
            "approved_at": "2026-07-25T01:00:00Z",
        },
    )
    benchmark = records.benchmarks[0]
    assert benchmark["task_counts"]["total"] == 394
    assert benchmark["task_counts"]["subsets"] == []
    assert benchmark["audit"]["status"] == "audited-with-caveats"
    assert benchmark["field_status"][0]["path"] == "/task_counts/subsets"
    assert benchmark["field_status"][0]["status"] == "conflicted"
    assert records.classifications["conflictcountbench"]["entries"][0]["count"] is None

    root_basis_conflicted = json.loads(json.dumps(payload))
    for item in root_basis_conflicted["verification"]["claims"]:
        if item["claim_id"] == "claim-6":
            item.update({
                "verdict": "conflicted",
                "confidence": "high",
                "locator": locator() | {"value": "Metadata paragraph 43"},
            })
    root_basis_records = build_records(
        root_basis_conflicted,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-25",
        owner_conflict_resolution={
            "benchmark_total": 394,
            "exclude": "benchmark-subcounts",
            "approved_by": "wang422003",
            "approved_at": "2026-07-25T01:00:00Z",
        },
    )
    root_basis_benchmark = root_basis_records.benchmarks[0]
    assert root_basis_benchmark["task_counts"]["total"] == 394
    assert root_basis_benchmark["task_counts"]["subsets"] == []
    assert root_basis_benchmark["task_counts"]["basis"] == (
        "Whole-dataset unique-sample total explicitly reported by the source; "
        "the detailed uniqueness basis remains conflicted."
    )
    assert root_basis_benchmark["field_status"][0]["path"] == "/task_counts/basis"
    assert root_basis_benchmark["field_status"][1]["path"] == "/task_counts/subsets"
    root_basis_conflict_evidence = next(
        item for item in root_basis_benchmark["evidence"]
        if item["id"] == "conflictcountbench-automated-count-conflict-evidence"
    )
    assert root_basis_conflict_evidence["supports"] == [
        "/task_counts/basis",
        "/task_counts/subsets",
    ]
    assert "verifier independently located it at high confidence" in (
        root_basis_benchmark["field_status"][0]["reason"]
    )

    insufficient_root_confidence = json.loads(json.dumps(root_basis_conflicted))
    for item in insufficient_root_confidence["verification"]["claims"]:
        if item["claim_id"] == "claim-6":
            item["confidence"] = "medium"
    with pytest.raises(
        GenerationBlocked,
        match="root total is not independently supported at high confidence",
    ):
        build_records(
            insufficient_root_confidence,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
            owner_conflict_resolution={
                "benchmark_total": 394,
                "exclude": "benchmark-subcounts",
                "approved_by": "wang422003",
                "approved_at": "2026-07-25T01:00:00Z",
            },
        )

    unsafe_root_basis_conflict = json.loads(json.dumps(root_basis_conflicted))
    unsafe_root_basis_conflict["verification"]["blocking_conflicts"].append(
        "Benchmark identity and repository identity disagree."
    )
    with pytest.raises(
        GenerationBlocked,
        match="cannot override an unanchored identity or license conflict",
    ):
        build_records(
            unsafe_root_basis_conflict,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
            owner_conflict_resolution={
                "benchmark_total": 394,
                "exclude": "benchmark-subcounts",
                "approved_by": "wang422003",
                "approved_at": "2026-07-25T01:00:00Z",
            },
        )

    unresolved_root_locator = json.loads(json.dumps(root_basis_conflicted))
    for item in unresolved_root_locator["verification"]["claims"]:
        if item["claim_id"] == "claim-6":
            item["locator"] = None
    with pytest.raises(
        GenerationBlocked,
        match="root total is not independently supported at high confidence",
    ):
        build_records(
            unresolved_root_locator,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
            owner_conflict_resolution={
                "benchmark_total": 394,
                "exclude": "benchmark-subcounts",
                "approved_by": "wang422003",
                "approved_at": "2026-07-25T01:00:00Z",
            },
        )

    unanchored = json.loads(json.dumps(payload))
    for item in unanchored["verification"]["claims"]:
        if item["claim_id"] == "claim-10":
            item.update({"verdict": "supported", "confidence": "high"})
    unanchored_records = build_records(
        unanchored,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-25",
        owner_conflict_resolution={
            "benchmark_total": 394,
            "exclude": "benchmark-subcounts",
            "approved_by": "wang422003",
            "approved_at": "2026-07-25T01:00:00Z",
        },
    )
    unanchored_benchmark = unanchored_records.benchmarks[0]
    assert unanchored_benchmark["task_counts"] == benchmark["task_counts"]
    assert "without binding it to a claim-level" in unanchored_benchmark["field_status"][0]["reason"]

    unsafe_unanchored = json.loads(json.dumps(unanchored))
    unsafe_unanchored["verification"]["blocking_conflicts"] = [
        "Benchmark version and repository identity disagree."
    ]
    with pytest.raises(
        GenerationBlocked,
        match=(
            "not count/inventory-only: "
            "Benchmark version and repository identity disagree"
        ),
    ):
        build_records(
            unsafe_unanchored,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
            owner_conflict_resolution={
                "benchmark_total": 394,
                "exclude": "benchmark-subcounts",
                "approved_by": "wang422003",
                "approved_at": "2026-07-25T01:00:00Z",
            },
        )

    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-5":
            item.update({"verdict": "conflicted", "confidence": "high"})
    with pytest.raises(GenerationBlocked, match="cannot override conflicted claim types"):
        build_records(
            payload,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-25",
            owner_conflict_resolution={
                "benchmark_total": 394,
                "exclude": "benchmark-subcounts",
                "approved_by": "wang422003",
                "approved_at": "2026-07-25T01:00:00Z",
            },
        )


def test_paper_use_scope_normalizes_source_descriptions_and_subset_labels() -> None:
    claims = [
        EvidenceClaimDraft.model_validate(
            claim("claim-1", "scope-type", "subset")
        ),
        EvidenceClaimDraft.model_validate(
            claim("claim-2", "subset-id", "Human Solvable")
        ),
        EvidenceClaimDraft.model_validate(
            claim(
                "claim-3",
                "selection",
                "Problems that independent human experts were able to solve",
            )
        ),
    ]

    scope = _use_scope(_scope(claims))

    assert scope["subset_id"] == "human-solvable"
    assert scope["selection"] == "filtered"
    assert scope["selection_method"] == (
        "Problems that independent human experts were able to solve"
    )

    partial_scope = _use_scope(_scope(claims), partial_use=True)
    assert partial_scope == {
        "type": "unknown",
        "subset_kind": "not-reported",
        "n": None,
        "subset_id": "human-solvable",
        "selection": "filtered",
        "selection_method": (
            "Problems that independent human experts were able to solve"
        ),
        "reporting_status": "not_reported",
    }

    complete_claims = [
        *claims,
        EvidenceClaimDraft.model_validate(
            claim("claim-4", "scope-n", 17)
        ),
    ]
    complete_partial_scope = _use_scope(
        _scope(complete_claims), partial_use=True
    )
    assert complete_partial_scope["type"] == "subset"
    assert complete_partial_scope["subset_kind"] == "paper-specific"
    assert complete_partial_scope["n"] == 17

    joined_formal_claims = [
        EvidenceClaimDraft.model_validate(
            claim("claim-1", "scope-type", "subset")
        ),
        EvidenceClaimDraft.model_validate(
            claim(
                "claim-2",
                "subset-id",
                "Human Solvable and Human Difficult",
            )
        ),
        EvidenceClaimDraft.model_validate(
            claim(
                "claim-3",
                "selection-method",
                "The source reports both official partitions.",
            )
        ),
    ]
    joined_formal_scope = _use_scope(
        _scope(joined_formal_claims),
        partial_use=True,
        formal_subset_ids={"human-solvable", "human-difficult"},
    )
    assert joined_formal_scope["type"] == "unknown"
    assert joined_formal_scope["subset_kind"] == "not-reported"
    assert joined_formal_scope["subset_id"] is None
    assert joined_formal_scope["selection_method"] == (
        "The source reports both official partitions."
    )

    valid_formal_claims = [
        *joined_formal_claims[:1],
        EvidenceClaimDraft.model_validate(
            claim("claim-2", "subset-id", "Human Solvable")
        ),
        *joined_formal_claims[2:],
    ]
    valid_formal_scope = _use_scope(
        _scope(valid_formal_claims),
        partial_use=True,
        formal_subset_ids={"human-solvable", "human-difficult"},
    )
    assert valid_formal_scope["type"] == "subset"
    assert valid_formal_scope["subset_kind"] == "formal-subset"
    assert valid_formal_scope["subset_id"] == "human-solvable"

    unversioned_full_claims = [
        EvidenceClaimDraft.model_validate(
            claim("claim-1", "scope-type", "full")
        ),
        EvidenceClaimDraft.model_validate(
            claim("claim-2", "scope-n", 195)
        ),
    ]
    unversioned_full_scope = _use_scope(
        _scope(unversioned_full_claims),
        partial_use=True,
        benchmark_version_reported=False,
    )
    assert unversioned_full_scope == {
        "type": "unknown",
        "subset_kind": "not-reported",
        "n": 195,
        "subset_id": None,
        "selection": None,
        "selection_method": None,
        "reporting_status": "not_reported",
    }

    versioned_full_scope = _use_scope(
        _scope(unversioned_full_claims),
        partial_use=True,
        benchmark_version_reported=True,
    )
    assert versioned_full_scope["type"] == "full"
    assert versioned_full_scope["n"] == 195

    unknown_claims = [
        EvidenceClaimDraft.model_validate(
            claim("claim-1", "scope-type", "unknown")
        ),
        EvidenceClaimDraft.model_validate(
            claim("claim-2", "scope-n", 195)
        ),
        EvidenceClaimDraft.model_validate(
            claim("claim-3", "selection", "Standard single-cell workflows")
        ),
    ]
    unknown_scope = _use_scope(_scope(unknown_claims))
    assert unknown_scope["selection"] is None
    assert unknown_scope["selection_method"] == "Standard single-cell workflows"


def test_existing_evaluation_setting_conflict_downgrades_to_partial_use() -> None:
    claims = [
        claim(
            "claim-1",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper"},
            mention_id=None,
        ),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
        claim("claim-4", "benchmark-version", "Verified variant"),
        claim("claim-5", "scope-type", "subset"),
        claim("claim-6", "scope-n", 115),
    ]
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    payload["verification"]["blocking_conflicts"] = [
        "claim-4: The source identifies Verified as a variant, not a benchmark version."
    ]
    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-4":
            item.update({"verdict": "conflicted", "confidence": "high"})

    records = build_records(
        payload,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert records.runs == []
    assert len(records.uses) == 1
    use = records.uses[0]
    assert use["status"] == "partial"
    assert use["benchmark_version"] is None
    assert any(
        "Conflicted benchmark-version claim omitted" in gap
        for gap in use["reporting_gaps"]
    )

    multi_metric = json.loads(json.dumps(payload))
    metric_payload = {
        "source_label": "Score",
        "unit": "fraction",
        "range": None,
        "higher_is_better": True,
        "aggregation": None,
        "pass_threshold": None,
        "tolerance": None,
        "kind": "absolute",
        "baseline_model_name": None,
        "statistical": None,
    }
    multi_metric["draft"]["claims"].extend([
        claim("claim-7", "metric", metric_payload),
        claim("claim-8", "metric", metric_payload),
    ])
    multi_metric["draft"]["benchmark_mentions"][0]["claim_ids"].extend([
        "claim-7", "claim-8",
    ])
    multi_metric["verification"]["claims"].extend([
        {
            "claim_id": claim_id,
            "verdict": "conflicted",
            "confidence": "high",
            "locator": locator(),
            "notes": "The printed metric range conflicts with the draft.",
        }
        for claim_id in ("claim-7", "claim-8")
    ])
    multi_metric["verification"]["blocking_conflicts"] = [
        "Metric claims claim-7 and claim-8 conflict with the printed Score (0-1) range."
    ]
    multi_metric_records = build_records(
        multi_metric,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert multi_metric_records.runs == []
    assert multi_metric_records.uses[0]["status"] == "partial"
    assert multi_metric_records.uses[0]["metric_labels"] == []
    assert any(
        "Conflicted metric claim omitted" in gap
        for gap in multi_metric_records.uses[0]["reporting_gaps"]
    )

    inconsistent = json.loads(json.dumps(payload))
    for item in inconsistent["verification"]["claims"]:
        if item["claim_id"] == "claim-4":
            item.update({"verdict": "supported", "confidence": "high"})
    inconsistent_records = build_records(
        inconsistent,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert inconsistent_records.runs == []
    assert inconsistent_records.uses[0]["status"] == "partial"
    assert inconsistent_records.uses[0]["benchmark_version"] is None

    display_name = json.loads(json.dumps(payload))
    for item in display_name["draft"]["claims"]:
        if item["claim_id"] == "claim-3":
            item["value_json"] = json.dumps("LifeSciBench")
    display_name_records = build_records(
        display_name,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert display_name_records.uses[0]["benchmark_id"] == "lifescibench"

    provider_variant = json.loads(json.dumps(payload))
    provider_variant["draft"]["benchmark_mentions"][0]["benchmark_name"] = "LifeSciBench Hard"
    for item in provider_variant["draft"]["claims"]:
        if item["claim_id"] == "claim-3":
            item["value_json"] = json.dumps("LifeSciBench Hard")
    provider_variant_records = build_records(
        provider_variant,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert provider_variant_records.runs == []
    assert provider_variant_records.uses[0]["status"] == "partial"

    unrelated_identity = json.loads(json.dumps(payload))
    for item in unrelated_identity["draft"]["claims"]:
        if item["claim_id"] == "claim-3":
            item["value_json"] = json.dumps("UnrelatedBench")
    unrelated_records = build_records(
        unrelated_identity,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert unrelated_records.blocked_reasons == [
        "LifeSciBench: benchmark identity was not independently verified"
    ]

    unanchored = json.loads(json.dumps(payload))
    unanchored["verification"]["blocking_conflicts"] = [
        "LifeSciBench Verified is a variant, not a registered benchmark version or artifact revision."
    ]
    for item in unanchored["verification"]["claims"]:
        if item["claim_id"] == "claim-4":
            item.update({"verdict": "supported", "confidence": "high"})
    unanchored_records = build_records(
        unanchored,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert unanchored_records.runs == []
    assert unanchored_records.uses[0]["status"] == "partial"
    assert unanchored_records.uses[0]["benchmark_version"] is None

    unresolved = json.loads(json.dumps(payload))
    unresolved["draft"]["benchmark_mentions"].append({
        "mention_id": "mention-2",
        "benchmark_name": "Organic chemistry, V2",
        "registry_benchmark_id": None,
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-7", "claim-8"],
        "reporting_gaps": [],
    })
    unresolved["draft"]["claims"].extend([
        claim("claim-7", "relation", "evaluation", mention_id="mention-2"),
        claim("claim-8", "scope-n", 1260, mention_id="mention-2"),
    ])
    unresolved["verification"]["claims"].extend([
        {
            "claim_id": claim_id,
            "verdict": "supported",
            "confidence": "high",
            "locator": locator(),
            "notes": None,
        }
        for claim_id in ("claim-7", "claim-8")
    ])
    unresolved["verification"]["blocking_conflicts"] = [
        "Organic chemistry attempt counts encode a non-exclusive timeout partition."
    ]
    unresolved_records = build_records(
        unresolved,
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-28",
    )
    assert unresolved_records.uses
    assert unresolved_records.omitted_unresolved_mentions == ["Organic chemistry, V2"]
    assert all(use["benchmark_id"] != "organic-chemistry-v2" for use in unresolved_records.uses)

    unsafe = json.loads(json.dumps(payload))
    unsafe["verification"]["blocking_conflicts"] = [
        "claim-3: The benchmark identity is contradicted by the source."
    ]
    for item in unsafe["verification"]["claims"]:
        item.update({"verdict": "supported", "confidence": "high"})
        if item["claim_id"] == "claim-3":
            item.update({"verdict": "conflicted", "confidence": "high"})
    with pytest.raises(GenerationBlocked, match="blocking source conflicts"):
        build_records(
            unsafe,
            source=SOURCE,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-28",
        )


def test_owner_can_downgrade_conflicted_creator_evaluation_to_partial_use() -> None:
    metadata = {
        "name": "ConservativeBench",
        "aliases": [],
        "summary": "A synthetic benchmark used to verify conservative creator-evaluation publication.",
        "kind": "agentic_eval",
        "organizations": ["Example Institute"],
        "release_date": "2026-07-01",
        "domains": ["single-cell"],
        "capabilities": ["data-analysis"],
        "modalities": ["raw-omics"],
        "task_formats": ["agent episode"],
        "access": {
            "level": "partially-open",
            "tasks": "Representative examples are public.",
            "artifacts": "The full benchmark is withheld.",
            "grader": "Deterministic grader",
            "license": "Apache-2.0",
            "biosafety_notes": None,
        },
    }
    creation_claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "ConservativeBench"),
        claim("claim-4", "benchmark-metadata", metadata),
        claim("claim-5", "benchmark-version", "paper-v1"),
        claim("claim-6", "benchmark-count", {
            "label": "total problems", "count": 394, "unit": "problems",
            "basis": "Problems released by the creators", "reporting_status": "reported",
            "subset_id": None, "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/synthetic.1"}),
        claim("claim-8", "official-repository", {
            "url": "https://github.com/example/conservativebench", "license": "Apache-2.0",
        }),
        claim("claim-9", "scientific-task", {
            "task_type_id": "cell-type-annotation", "coverage": "explicitly-in-scope",
            "mapping_method": "official-taxonomy", "count": None, "count_unit": "problems",
            "count_basis": "Conflicted subcounts are withheld", "reporting_status": "not_reported",
            "notes": None,
        }),
        claim("claim-10", "benchmark-count", {
            "label": "appendix inventory", "count": 390, "unit": "problems",
            "basis": "Conflicted appendix inventory", "reporting_status": "reported",
            "subset_id": "appendix-inventory", "exclusive": False, "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-20", "tools", {
            "browser": False,
            "internet": False,
            "databases": [],
            "code_execution": True,
            "container": True,
            "external_tools": [],
        }),
    ]
    evaluation_claims = [
        claim("claim-11", "relation", "evaluation", mention_id="mention-2"),
        claim("claim-12", "benchmark-identity", "ConservativeBench", mention_id="mention-2"),
        claim("claim-13", "model", {
            "name": "Example Model", "provider": "Example Provider",
            "version_string": "example-model-2026-07-01", "release_date": "2026-07-01",
        }, mention_id="mention-2"),
        claim("claim-14", "benchmark-metadata", metadata, mention_id="mention-2"),
        claim("claim-15", "benchmark-version", "paper-v1-conflicted", mention_id="mention-2"),
        claim("claim-16", "scope-type", "full", mention_id="mention-2"),
        claim("claim-17", "scope-n", 390, mention_id="mention-2"),
        claim("claim-18", "metric", {
            "source_label": "Accuracy", "unit": "fraction", "range": [0, 1],
            "higher_is_better": True, "aggregation": "macro", "pass_threshold": None,
            "tolerance": None, "kind": "absolute", "baseline_model_name": None,
            "statistical": None,
        }, mention_id="mention-2"),
        claim("claim-19", "result", {
            "model_name": "Example Model", "metric_source_label": "Accuracy", "value": 0.5,
            "ci_low": None, "ci_high": None, "n": 390, "notes": None,
            "numeric_source": "table",
        }, mention_id="mention-2"),
    ]
    claims = creation_claims + evaluation_claims
    creation_mention = {
        "mention_id": "mention-1", "benchmark_name": "ConservativeBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in creation_claims if item["mention_id"]],
        "reporting_gaps": ["appendix inventory conflicts with the supported root total"],
    }
    evaluation_mention = {
        "mention_id": "mention-2", "benchmark_name": "ConservativeBench",
        "registry_benchmark_id": None, "relation_type": "evaluation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": [item["claim_id"] for item in evaluation_claims],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, creation_mention)
    payload["draft"]["benchmark_mentions"] = [creation_mention, evaluation_mention]
    payload["verification"]["blocking_conflicts"] = [
        "Appendix counts and creator-evaluation settings disagree."
    ]
    for item in payload["verification"]["claims"]:
        if item["claim_id"] in {
            "claim-10", "claim-14", "claim-15", "claim-17", "claim-18", "claim-19", "claim-20",
        }:
            item.update({"verdict": "conflicted", "confidence": "high"})
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/conservativebench": {
            "kind": "commit", "value": "d" * 40,
            "url": "https://github.com/example/conservativebench/commit/" + "d" * 40,
        }
    }}
    count_only = {
        "benchmark_total": 394,
        "exclude": "benchmark-subcounts",
        "exclude_creator_evaluation": False,
        "approved_by": "wang422003",
        "approved_at": "2026-07-27T01:00:00Z",
    }
    with pytest.raises(GenerationBlocked, match="cannot override conflicted claim types"):
        build_records(
            payload, source=source, generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27", owner_conflict_resolution=count_only,
        )

    records = build_records(
        payload, source=source, generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
        owner_conflict_resolution={
            **count_only,
            "exclude": "benchmark-subcounts,creator-evaluation",
            "exclude_creator_evaluation": True,
        },
    )
    benchmark = records.benchmarks[0]
    assert benchmark["task_counts"]["total"] == 394
    assert benchmark["task_counts"]["subsets"] == []
    evaluation_use = next(item for item in records.uses if item["relation_type"] == "evaluation")
    assert evaluation_use["status"] == "partial"
    assert evaluation_use["benchmark_version"] is None
    assert evaluation_use["scope"]["type"] == "unknown"
    assert evaluation_use["scope"]["n"] is None
    assert evaluation_use["model_ids"] == ["example-provider-example-model"]
    assert evaluation_use["metric_labels"] == []
    assert evaluation_use["evaluation_run_ids"] == []
    assert records.runs == []
    for gap in (
        "benchmark version", "realized n/scope", "metric", "numeric result",
        "prompt and tools", "grader and repeats",
    ):
        assert gap in evaluation_use["reporting_gaps"]

    for item in payload["verification"]["claims"]:
        if item["claim_id"] == "claim-5":
            item.update({"verdict": "conflicted", "confidence": "high"})
    with pytest.raises(GenerationBlocked, match="cannot override conflicted claim types"):
        build_records(
            payload, source=source, generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27",
            owner_conflict_resolution={
                **count_only,
                "exclude": "benchmark-subcounts,creator-evaluation",
                "exclude_creator_evaluation": True,
            },
        )


def test_owner_can_downgrade_creator_evaluation_with_not_reported_root_total() -> None:
    metadata = {
        "name": "ScenarioMatrixBench",
        "aliases": [],
        "summary": "A reusable scenario matrix for conservative creator-evaluation intake tests.",
        "kind": "dataset",
        "organizations": ["Example Institute"],
        "release_date": "2026-07-01",
        "domains": ["transcriptomics"],
        "capabilities": ["data-analysis"],
        "modalities": ["raw-omics"],
        "task_formats": ["analysis"],
        "access": {
            "level": "fully-open",
            "tasks": "Scenario definitions are public.",
            "artifacts": "Simulation and analysis artifacts are public.",
            "grader": "Source-defined statistical metrics.",
            "license": "Apache-2.0",
            "biosafety_notes": None,
        },
    }
    atomic_values = {
        "/name": metadata["name"],
        "/aliases": metadata["aliases"],
        "/summary": metadata["summary"],
        "/kind": "suite",
        "/organizations": metadata["organizations"],
        "/release_date": metadata["release_date"],
        "/domains": metadata["domains"],
        "/capabilities": metadata["capabilities"],
        "/modalities": metadata["modalities"],
        "/task_formats": metadata["task_formats"],
        "/access/tasks": metadata["access"]["tasks"],
        "/access/artifacts": metadata["access"]["artifacts"],
        "/access/grader": metadata["access"]["grader"],
        "/access/license": metadata["access"]["license"],
        "/access/biosafety_notes": metadata["access"]["biosafety_notes"],
    }
    metadata_claims = [
        claim(
            f"claim-{index}",
            "benchmark-metadata",
            value,
            field_path=f"/benchmark-metadata{path}",
        )
        for index, (path, value) in enumerate(atomic_values.items(), 20)
    ]
    claims = [
        claim(
            "claim-1",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper"},
            mention_id=None,
        ),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "ScenarioMatrixBench"),
        *metadata_claims,
        claim("claim-5", "benchmark-version", "paper-v1"),
        claim("claim-6", "benchmark-count", {
            "label": "root scenario-matrix inventory",
            "count": None,
            "unit": "other",
            "basis": "The source defines a reusable scenario matrix without one finite item total.",
            "reporting_status": "not_reported",
            "subset_id": None,
            "exclusive": False,
            "exhaustive": False,
            "partition_group": None,
        }),
        claim("claim-7", "creator-source", {"url": "https://doi.org/10.9999/scenario.1"}),
        claim("claim-8", "official-repository", {
            "url": "https://github.com/example/scenario-matrix-bench",
            "license": "Apache-2.0",
        }),
        claim("claim-9", "tools", {
            "browser": False,
            "internet": False,
            "databases": [],
            "code_execution": True,
            "container": True,
            "external_tools": [],
        }),
        claim("claim-10", "relation", "evaluation", mention_id="mention-2"),
        claim("claim-11", "benchmark-identity", "ScenarioMatrixBench", mention_id="mention-2"),
        claim("claim-12", "model", {
            "name": "Example Tool",
            "provider": "Example Institute",
            "version_string": "example-tool-1.0",
            "release_date": "2026-07-01",
        }, mention_id="mention-2"),
        claim("claim-13", "scope-type", "subset", mention_id="mention-2"),
        claim("claim-14", "scope-n", 30, mention_id="mention-2"),
        claim("claim-15", "metric", {
            "source_label": "F1", "unit": "fraction", "range": [0, 1],
            "higher_is_better": True, "aggregation": "macro", "pass_threshold": None,
            "tolerance": None, "kind": "absolute", "baseline_model_name": None,
            "statistical": None,
        }, mention_id="mention-2"),
        claim("claim-16", "result", {
            "model_name": "Example Tool", "metric_source_label": "F1", "value": 0.5,
            "ci_low": None, "ci_high": None, "n": 30, "notes": None,
            "numeric_source": "table",
        }, mention_id="mention-2"),
    ]
    creation_mention = {
        "mention_id": "mention-1",
        "benchmark_name": "ScenarioMatrixBench",
        "registry_benchmark_id": None,
        "relation_type": "benchmark-creation",
        "is_new_benchmark": True,
        "background_only": False,
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"] == "mention-1"],
        "reporting_gaps": [],
    }
    evaluation_mention = {
        "mention_id": "mention-2",
        "benchmark_name": "ScenarioMatrixBench",
        "registry_benchmark_id": None,
        "relation_type": "evaluation",
        "is_new_benchmark": True,
        "background_only": False,
        "claim_ids": [f"claim-{index}" for index in range(10, 17)],
        "reporting_gaps": [],
    }
    payload = verified_result(claims, creation_mention)
    payload["draft"]["benchmark_mentions"] = [creation_mention, evaluation_mention]
    kind_claim = next(
        item for item in payload["draft"]["claims"]
        if item["field_path"] == "/benchmark-metadata/kind"
    )
    kind_claim["confidence"] = "medium"
    payload["verification"]["blocking_conflicts"] = [
        "The realized evaluation sample n is internally inconsistent.",
        "The body and figure caption identify different evaluated tools.",
    ]
    for item in payload["verification"]["claims"]:
        if item["claim_id"] in {"claim-9", "claim-14", "claim-15", "claim-16"}:
            item.update({"verdict": "conflicted", "confidence": "high"})
    source = {**SOURCE, "repository_pins": {
        "https://github.com/example/scenario-matrix-bench": {
            "kind": "commit",
            "value": "e" * 40,
            "url": "https://github.com/example/scenario-matrix-bench/commit/" + "e" * 40,
        }
    }}
    resolution = {
        "benchmark_total": None,
        "exclude": "creator-evaluation",
        "exclude_creator_evaluation": True,
        "approved_by": "wang422003",
        "approved_at": "2026-07-27T02:00:00Z",
        "provisional_benchmark_kind": "suite",
        "provisional_kind_status": "provisional",
        "provisional_kind_approved_at": "2026-07-27T02:01:00Z",
        "provisional_access_level": "fully-open",
        "provisional_access_status": "provisional",
        "provisional_access_approved_at": "2026-07-27T02:02:00Z",
    }

    without_access_resolution = {
        key: value
        for key, value in resolution.items()
        if not key.startswith("provisional_access")
    }
    blocked_without_access = build_records(
        payload,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
        owner_conflict_resolution=without_access_resolution,
    )
    assert "/access/level" in "; ".join(blocked_without_access.blocked_reasons)

    records = build_records(
        payload,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
        owner_conflict_resolution=resolution,
    )
    benchmark = records.benchmarks[0]
    assert benchmark["task_counts"]["total"] is None
    assert benchmark["task_counts"]["reporting_status"] == "not_reported"
    kind_status = next(item for item in benchmark["field_status"] if item["path"] == "/kind")
    assert kind_status["status"] == "provisional"
    assert kind_status["confidence"] == "medium"
    kind_evidence = next(
        item for item in benchmark["evidence"] if item["id"] in kind_status["evidence_ids"]
    )
    assert kind_evidence["supports"] == ["/kind"]
    access_status = next(
        item for item in benchmark["field_status"] if item["path"] == "/access/level"
    )
    assert access_status["status"] == "provisional"
    assert access_status["confidence"] == "medium"
    assert benchmark["access"]["level"] == "fully-open"
    access_evidence = [
        item for item in benchmark["evidence"]
        if item["id"] in access_status["evidence_ids"]
    ]
    assert len(access_evidence) == 4
    assert all("/access/level" in item["supports"] for item in access_evidence)
    assert "Root total retained after owner review" not in str(benchmark["versions"][0]["notes"])
    evaluation_use = next(item for item in records.uses if item["relation_type"] == "evaluation")
    assert evaluation_use["status"] == "partial"
    assert evaluation_use["scope"]["type"] == "unknown"
    assert evaluation_use["scope"]["n"] is None
    assert evaluation_use["model_ids"] == ["example-institute-example-tool"]
    assert evaluation_use["metric_labels"] == []
    assert evaluation_use["evaluation_run_ids"] == []
    assert records.runs == []

    no_blocking_messages = json.loads(json.dumps(payload))
    no_blocking_messages["verification"]["blocking_conflicts"] = []
    records_without_repeated_conflict = build_records(
        no_blocking_messages,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
        owner_conflict_resolution=resolution,
    )
    benchmark_without_repeated_conflict = records_without_repeated_conflict.benchmarks[0]
    assert benchmark_without_repeated_conflict["kind"] == "suite"
    assert benchmark_without_repeated_conflict["access"]["level"] == "fully-open"
    assert {
        item["path"] for item in benchmark_without_repeated_conflict["field_status"]
    } >= {"/kind", "/access/level"}
    assert records_without_repeated_conflict.runs == []

    conflicting_access = json.loads(json.dumps(payload))
    conflicting_access_claim = claim(
        "claim-99",
        "benchmark-metadata",
        "partially-open",
        field_path="/benchmark-metadata/access/level",
    )
    conflicting_access["draft"]["claims"].append(conflicting_access_claim)
    conflicting_access["draft"]["benchmark_mentions"][0]["claim_ids"].append("claim-99")
    conflicting_access["verification"]["claims"].append({
        "claim_id": "claim-99",
        "verdict": "supported",
        "confidence": "high",
        "locator": conflicting_access_claim["locators"][0],
        "notes": None,
    })
    with pytest.raises(GenerationBlocked, match="conflicts with an extracted access claim"):
        build_records(
            conflicting_access,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27",
            owner_conflict_resolution=resolution,
        )

    missing_access_evidence = json.loads(json.dumps(payload))
    grader_claim_id = next(
        item["claim_id"]
        for item in missing_access_evidence["draft"]["claims"]
        if item["field_path"] == "/benchmark-metadata/access/grader"
    )
    missing_access_evidence["draft"]["claims"] = [
        item for item in missing_access_evidence["draft"]["claims"]
        if item["claim_id"] != grader_claim_id
    ]
    missing_access_evidence["draft"]["benchmark_mentions"][0]["claim_ids"].remove(
        grader_claim_id
    )
    missing_access_evidence["verification"]["claims"] = [
        item for item in missing_access_evidence["verification"]["claims"]
        if item["claim_id"] != grader_claim_id
    ]
    with pytest.raises(GenerationBlocked, match="source claim for /access/grader"):
        build_records(
            missing_access_evidence,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27",
            owner_conflict_resolution=resolution,
        )

    high_confidence_kind = json.loads(json.dumps(payload))
    for item in high_confidence_kind["draft"]["claims"]:
        if item["field_path"] == "/benchmark-metadata/kind":
            item["confidence"] = "high"
    high_confidence_records = build_records(
        high_confidence_kind,
        source=source,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-27",
        owner_conflict_resolution=resolution,
    )
    high_confidence_kind_status = next(
        item
        for item in high_confidence_records.benchmarks[0]["field_status"]
        if item["path"] == "/kind"
    )
    assert high_confidence_kind_status["status"] == "provisional"

    wrong_kind = json.loads(json.dumps(payload))
    for item in wrong_kind["draft"]["claims"]:
        if item["field_path"] == "/benchmark-metadata/kind":
            item["value_json"] = json.dumps("dataset")
    with pytest.raises(GenerationBlocked, match="requires exactly one source-located"):
        build_records(
            wrong_kind,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27",
            owner_conflict_resolution=resolution,
        )

    unsafe = json.loads(json.dumps(payload))
    unsafe["verification"]["blocking_conflicts"].append(
        "The benchmark identity conflicts with the official resource."
    )
    for item in unsafe["verification"]["claims"]:
        if item["claim_id"] == "claim-3":
            item.update({"verdict": "conflicted", "confidence": "high"})
    with pytest.raises(GenerationBlocked, match="cannot override conflicted claim types"):
        build_records(
            unsafe,
            source=source,
            generated_at=SOURCE["retrieved_at"],
            verified_on="2026-07-27",
            owner_conflict_resolution=resolution,
        )


def test_discovery_high_precision_dedup_and_area_quotas() -> None:
    entities = load_entities()
    exact = Candidate(
        source_api="arxiv", source_id="1", title="We evaluate LifeSciBench for protein binding",
        abstract="evaluation", publication_date="2026-01-01", doi=None, arxiv="2601.00001",
        canonical_url="https://arxiv.org/abs/2601.00001", pdf_url="https://arxiv.org/pdf/2601.00001.pdf",
        open_fulltext=True,
    )
    assert score_candidate(exact, entities).matched_benchmark_ids == ["lifescibench"]
    unrelated = Candidate(
        source_api="crossref", source_id="2", title="A descriptive study", abstract="protein expression",
        publication_date="2026-01-01", doi="10.1/no", arxiv=None,
        canonical_url="https://doi.org/10.1/no", pdf_url=None,
    )
    assert score_candidate(unrelated, entities) is None
    duplicate = Candidate(**{**exact.__dict__, "source_api": "europe-pmc", "source_id": "3"})
    exact.score = duplicate.score = 100
    assert len(deduplicate_candidates([exact, duplicate])) == 1
    candidates = []
    for area, quota in AREA_QUOTAS.items():
        for index in range(quota + 3):
            candidates.append(Candidate(
                source_api="x", source_id=f"{area}-{index}", title=f"{area} {index}", abstract="",
                publication_date="2026-01-01", doi=f"10.9/{area}.{index}", arxiv=None,
                canonical_url=f"https://example.org/{area}/{index}", pdf_url=None,
                area=area, score=100-index,
            ))
    selected = select_by_quota(candidates)
    assert len(selected) == 10
    assert {area: sum(item.area == area for item in selected) for area in AREA_QUOTAS} == AREA_QUOTAS


def test_discovery_deduplicates_unlabeled_external_paper_issues() -> None:
    body = """### Paper or preprint URL

https://doi.org/10.9999/example

### DOI (optional)

10.9999/example

### arXiv or preprint ID (optional)

2601.01234

### Title (optional)

An External Protein Benchmark
"""
    session = SequenceSession([JsonResponse([
        {
            "title": "[Paper intake]: An External Protein Benchmark",
            "body": body,
            "labels": [],
            "closed_at": None,
        },
        {
            "title": "[Question]: An External Protein Benchmark",
            "body": body,
            "labels": [],
            "closed_at": None,
        },
        {
            "title": "[Paper intake]: Pull request lookalike",
            "body": body,
            "labels": [],
            "closed_at": None,
            "pull_request": {},
        },
    ])])

    fingerprints = existing_candidate_fingerprints(session, "example/repo", "token")

    assert "doi:10.9999/example" in fingerprints
    assert "arxiv:2601.01234" in fingerprints
    assert "url:https://doi.org/10.9999/example" in fingerprints
    assert "title:anexternalproteinbenchmark" in fingerprints
    assert session.calls[0][2]["params"] == {
        "state": "all", "per_page": 100, "page": 1,
    }


def test_discovery_closes_only_inactive_sixty_day_candidates() -> None:
    old = "2000-01-01T00:00:00Z"
    recent = datetime.now().astimezone().isoformat()
    session = SequenceSession([
        JsonResponse([
            {"number": 1, "created_at": old, "labels": [{"name": "paper-candidate"}]},
            {"number": 2, "created_at": old, "labels": [{"name": "ready-for-local-intake"}]},
            {"number": 3, "created_at": old, "labels": [{"name": "local-intake-in-progress"}]},
            {"number": 4, "created_at": old, "labels": [{"name": "paper-intake-pr"}]},
            {"number": 5, "created_at": recent, "labels": [{"name": "paper-candidate"}]},
        ]),
        JsonResponse({"number": 1, "state": "closed"}),
    ])

    closed = close_stale_candidates(session, "example/repo", "token")

    assert closed == [1]
    patch_calls = [call for call in session.calls if call[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0][2]["json"] == {
        "state": "closed", "state_reason": "not_planned",
    }


class JsonResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self.payload = payload; self.status_code = status_code; self.headers = {}
    def json(self): return self.payload
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(str(self.status_code))


class SequenceSession:
    def __init__(self, responses: list[JsonResponse]): self.responses = responses; self.calls = []
    def request(self, method: str, url: str, **kwargs: Any):
        self.calls.append((method, url, kwargs)); return self.responses.pop(0)


def test_europe_pmc_cursor_pagination_and_transient_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "id": "1", "source": "MED", "title": "A protein benchmark", "abstractText": "evaluation",
        "doi": "10.1/x", "firstPublicationDate": "2026-01-01", "isOpenAccess": "N",
    }
    session = SequenceSession([
        JsonResponse({"resultList": {"result": [item]}, "nextCursorMark": "next"}),
        JsonResponse({"resultList": {"result": [{**item, "id": "2", "doi": "10.1/y"}]}, "nextCursorMark": "next"}),
    ])
    assert len(fetch_europe_pmc(session, max_pages=2)) == 2
    assert session.calls[0][2]["params"]["cursorMark"] == "*"
    assert session.calls[1][2]["params"]["cursorMark"] == "next"
    retry_session = SequenceSession([JsonResponse({}, 429), JsonResponse({"ok": True})])
    monkeypatch.setattr("discover_papers.time.sleep", lambda _: None)
    assert _request(retry_session, "GET", "https://example.org").json() == {"ok": True}


def test_local_codex_double_pass_is_independent_read_only_and_ephemeral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-1", "claim-2"],
        "reporting_gaps": [],
    }
    claims = [
        claim("claim-1", "relation", "evaluation"),
        claim("claim-2", "benchmark-identity", "lifescibench"),
        claim(
            "claim-3",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ]
    draft = draft_payload(claims, mention)
    verification = {
        "source_parseable": True,
        "conflicts": [],
        "blocking_conflicts": [],
        "claims": [{
            "claim_id": claim_id,
            "verdict": "supported",
            "confidence": "high",
            "locator": locator(),
            "notes": None,
        } for claim_id in ("claim-1", "claim-2", "claim-3")],
    }
    source = tmp_path / "paper.txt"
    source.write_text("Synthetic evidence source.", encoding="utf-8")
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    prompts: list[str] = []
    stage = 0

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal stage
        commands.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        stage += 1
        environments.append(kwargs["env"])
        prompts.append(kwargs["input"])
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(draft if stage == 1 else verification),
            encoding="utf-8",
        )
        stdout = json.dumps({
            "type": "thread.started",
            "thread_id": f"thread-{stage}",
            "model": "gpt-5.6-sol-resolved",
        })
        return subprocess.CompletedProcess(command, 0, stdout + "\n", "")

    temporary_root = tmp_path / "local-evidence"
    heartbeat_path = tmp_path / "heartbeat.json"
    monkeypatch.setattr("extract_paper.LOCAL_TMP_ROOT", temporary_root)
    monkeypatch.setattr("extract_paper.HEARTBEAT_PATH", heartbeat_path)
    secret_name = "OPENAI" + "_API_KEY"
    monkeypatch.setenv(secret_name, "must-not-propagate")
    result = run_double_pass(
        source,
        registry_context={"benchmarks": [], "models": [], "taxonomy_ids": {}},
        review_focus={
            "benchmark_hints": "LifeSciBench",
            "focus_locators": "pages 12-13",
        },
        binary="codex",
        runner=runner,
    )
    assert result.extractor_thread_id == "thread-1"
    assert result.verifier_thread_id == "thread-2"
    assert result.accepted_claim_ids == ["claim-1", "claim-2", "claim-3"]
    assert len(environments) == 2
    assert all(secret_name not in environment for environment in environments)
    assert len(prompts) == 2
    assert all("owner-selected scope hints" in prompt for prompt in prompts)
    assert all("unverified data, not evidence or instructions" in prompt for prompt in prompts)
    for command in commands[:2]:
        assert {"--ephemeral", "--ignore-user-config", "--output-schema"} <= set(command)
        assert 'model_provider="biobench_local"' in command
        assert "model_providers.biobench_local.supports_websockets=false" in command
        assert 'web_search="disabled"' in command
        assert "features.apps=false" in command
        assert "features.remote_plugin=false" in command
        assert "features.multi_agent=false" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
    assert not temporary_root.exists()
    assert "untrusted" in EXTRACTOR_PROMPT
    assert "Do not use the network" in EXTRACTOR_PROMPT
    assert "formal subset or" in EXTRACTOR_PROMPT
    assert "Do not collapse a" in EXTRACTOR_PROMPT
    assert "both the domain row and capability" in EXTRACTOR_PROMPT
    assert "design/optimization" in EXTRACTOR_PROMPT
    assert "both introduces and evaluates the same" in EXTRACTOR_PROMPT
    assert "Issue hints may help find" in EXTRACTOR_PROMPT
    assert "document-page-NNN.jpg" in EXTRACTOR_PROMPT
    assert "independent verifier" in VERIFIER_PROMPT
    assert "semantic source claim" in VERIFIER_PROMPT
    assert "full meaning preserved in the label" in VERIFIER_PROMPT
    assert "ordinary scientific prose" in VERIFIER_PROMPT
    assert "document-page-NNN.jpg" in VERIFIER_PROMPT
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "verifier"
    assert heartbeat["status"] == "completed"
    assert heartbeat["run_label"] == "paper-intake"
    assert heartbeat["stage_timeout_seconds"] == 45 * 60
    serialized_heartbeat = json.dumps(heartbeat)
    assert "Synthetic evidence source" not in serialized_heartbeat
    assert "claim-1" not in serialized_heartbeat


def test_long_pdf_double_pass_exposes_only_owner_selected_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-1", "claim-2"],
        "reporting_gaps": [],
    }
    draft = draft_payload([
        claim("claim-1", "relation", "evaluation"),
        claim("claim-2", "benchmark-identity", "lifescibench"),
        claim(
            "claim-3",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ], mention)
    for item in draft["claims"]:
        item["locators"][0]["document_page"] = 1
    verification = {
        "source_parseable": True,
        "conflicts": [],
        "blocking_conflicts": [],
        "claims": [{
            "claim_id": claim_id,
            "verdict": "supported",
            "confidence": "high",
            "locator": locator() | {"document_page": 1},
            "notes": None,
        } for claim_id in ("claim-1", "claim-2", "claim-3")],
    }
    source = tmp_path / "long.pdf"
    source.write_bytes(pdf_bytes(151))
    stage = 0

    def fake_render(
        source_path: Path,
        output_dir: Path,
        *,
        preferred_pages: list[int] | None = None,
        **_: Any,
    ) -> list[Path]:
        assert source_path.exists()
        assert preferred_pages == [1, 151]
        images = [output_dir / "document-page-001.jpg", output_dir / "document-page-151.jpg"]
        for image in images:
            image.write_bytes(b"jpeg")
        return images

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal stage
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        stage += 1
        prompt = kwargs["input"]
        match = re.search(
            r"Read the (?:source at|source claim packet at) (\S+)",
            prompt,
        )
        assert match is not None
        focused_path = Path(match.group(1).rstrip("."))
        focused_text = focused_path.read_text(encoding="utf-8")
        assert "=== DOCUMENT PAGE 1 ===" in focused_text
        if stage == 1:
            assert "=== DOCUMENT PAGE 151 ===" in focused_text
            assert "complete paper evidence extraction" not in focused_text
        else:
            assert "independent claim verification" in focused_text
            assert "=== DOCUMENT PAGE 151 ===" not in focused_text
        assert not (focused_path.parent / "source.pdf").exists()
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(draft if stage == 1 else verification),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"type": "thread.started", "thread_id": f"focused-{stage}"}) + "\n",
            "",
        )

    monkeypatch.setattr("extract_paper.LOCAL_TMP_ROOT", tmp_path / "local-evidence")
    monkeypatch.setattr("extract_paper.HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr("extract_paper._render_pdf_pages", fake_render)
    result = run_double_pass(
        source,
        registry_context={"benchmarks": [], "models": [], "taxonomy_ids": {}},
        preferred_pdf_pages=[1, 151],
        binary="codex",
        runner=runner,
    )
    assert result.extractor_thread_id == "focused-1"
    assert result.verifier_thread_id == "focused-2"


def test_heartbeat_status_marks_dead_running_process_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({
        "run_id": "safe-run-id",
        "run_label": "golden/spatialbench-version-separation/spatialbench-paper-v2",
        "stage": "verifier",
        "status": "running",
        "process_pid": 999_999_999,
        "updated_at": "2026-07-23T10:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr("local_paper_intake.HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr("extract_paper.HEARTBEAT_PATH", heartbeat_path)
    state = heartbeat_status(
        run_id="safe-run-id",
        now=datetime.fromisoformat("2026-07-23T10:01:00+00:00"),
    )
    assert state["process_alive"] is False
    assert state["stale"] is True
    assert state["heartbeat_age_seconds"] == 60


def test_parallel_run_slots_are_capped_and_unique_per_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("local_paper_intake.STATE_ROOT", tmp_path)
    monkeypatch.setattr("local_paper_intake.RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr("local_paper_intake.RUN_LOCK_PATH", tmp_path / "intake.lock")

    for index in range(MAX_PARALLEL_RUNS):
        _reserve_run(
            run_id=f"run-{index}",
            issue_number=100 + index,
            base_sha="a" * 40,
        )
    assert len(_active_run_states()) == MAX_PARALLEL_RUNS
    with pytest.raises(LocalIntakeError, match="concurrency limit"):
        _reserve_run(
            run_id="run-overflow",
            issue_number=999,
            base_sha="a" * 40,
        )
    with pytest.raises(LocalIntakeError, match="already has active local run"):
        _reserve_run(
            run_id="run-duplicate",
            issue_number=100,
            base_sha="a" * 40,
        )


def test_heartbeats_are_scoped_by_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("extract_paper.HEARTBEAT_ROOT", tmp_path)
    assert heartbeat_path("run-one") == tmp_path / "run-one.json"
    assert heartbeat_path("run-two") == tmp_path / "run-two.json"


def test_batch_uses_three_independent_worktrees_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = [
        BatchWorktree(
            issue_number=number,
            run_id=f"run-{number}",
            work_id_hint=f"work-{number}",
            branch=f"paper-intake/work-{number}-{number}",
            path=tmp_path / f"worktree-{number}",
            base_sha="a" * 40,
        )
        for number in (46, 42, 55)
    ]
    created: list[int] = []
    removed: list[int] = []
    active = 0
    peak = 0
    lock = threading.Lock()

    monkeypatch.setattr(
        "local_paper_intake._batch_worktree_plan",
        lambda issue_numbers, runner=subprocess.run: plans,
    )
    monkeypatch.setattr(
        "local_paper_intake._create_batch_worktree",
        lambda plan, runner=subprocess.run: created.append(plan.issue_number),
    )
    monkeypatch.setattr(
        "local_paper_intake._remove_batch_worktree",
        lambda plan, runner=subprocess.run: removed.append(plan.issue_number),
    )

    def worker(plan: BatchWorktree) -> dict[str, Any]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {
            "issue_number": plan.issue_number,
            "run_id": plan.run_id,
            "branch": plan.branch,
            "status": "pr-open",
            "pr_url": f"https://github.example/pull/{plan.issue_number}",
            "error": None,
        }

    monkeypatch.setattr("local_paper_intake._run_batch_worker", worker)
    result = run_batch([46, 42, 55])
    assert result["failed"] == 0
    assert result["max_parallel"] == 3
    assert result["merge_policy"] == "sequential"
    assert peak == 3
    assert created == [46, 42, 55]
    assert sorted(removed) == [42, 46, 55]


def test_golden_checkpoint_cli_compatibility_uses_major_version() -> None:
    assert _codex_cli_major("codex-cli 0.145.0-alpha.30") == "0"
    assert _codex_cli_major("codex-cli 0.146.0-alpha.3.1") == "0"
    assert _codex_cli_major("codex-cli 2.1.0") == "2"


def test_child_codex_environment_drops_remote_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [
        "OPENAI" + "_API_KEY",
        "CODEX_API_KEY",
        "PAPER_EXTRACT_MODEL",
        "PAPER_VERIFY_MODEL",
        "BIOBENCH_APP_ID",
        "BIOBENCH_APP_PRIVATE_KEY",
    ]
    for name in names:
        monkeypatch.setenv(name, "secret")
    environment = _child_environment()
    assert not (set(names) & set(environment))


def test_codex_failure_diagnostic_surfaces_errors_without_agent_content() -> None:
    stdout = "\n".join([
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Sensitive paper excerpt and extracted claim must stay private.",
            },
        }),
        json.dumps({
            "type": "error",
            "message": (
                "Structured output failed in "
                f"{ROOT}/.paper-intake-tmp/run/source.pdf"
            ),
        }),
    ])
    stderr = (
        "WebSocket connection failed with 403\n"
        "authorization: bearer should-not-appear\n"
    )
    diagnostic = _codex_failure_diagnostic(stdout, stderr)
    assert "Structured output failed" in diagnostic
    assert "<repo>/.paper-intake-tmp/run/source.pdf" in diagnostic
    assert "WebSocket connection failed with 403" in diagnostic
    assert "Sensitive paper excerpt" not in diagnostic
    assert "should-not-appear" not in diagnostic


def test_structured_output_diagnostic_omits_claim_values() -> None:
    with pytest.raises(ValidationError) as captured:
        PaperEvidenceVerification.model_validate({
            "claims": [{
                "claim_id": "claim-1",
                "verdict": "supported",
                "confidence": "high",
                "locator": {
                    "locator_type": "table",
                    "value": "Sensitive source text must not appear",
                    "document_page": 0,
                    "printed_page": None,
                    "excerpt": "Sensitive excerpt must not appear",
                },
                "notes": None,
            }],
            "conflicts": [],
            "blocking_conflicts": [],
            "source_parseable": True,
        })
    diagnostic = _structured_output_diagnostic(captured.value)
    assert "claims.0.locator.document_page" in diagnostic
    assert "greater_than_equal" in diagnostic
    assert "Sensitive" not in diagnostic


def test_claim_value_json_normalizes_safe_json_like_output() -> None:
    scalar = EvidenceClaimDraft.model_validate({
        "claim_id": "claim-1",
        "mention_id": "mention-1",
        "claim_type": "scope-type",
        "field_path": "/scope/type",
        "value_json": "full",
        "confidence": "high",
        "locators": [locator()],
    })
    assert json.loads(scalar.value_json) == "full"

    structured = EvidenceClaimDraft.model_validate({
        "claim_id": "claim-2",
        "mention_id": "mention-1",
        "claim_type": "benchmark-count",
        "field_path": "/task_counts/0",
        "value_json": "{'label': 'total', 'count': 146}",
        "confidence": "high",
        "locators": [locator()],
    })
    assert json.loads(structured.value_json) == {"label": "total", "count": 146}

    metadata_scalar = EvidenceClaimDraft.model_validate({
        "claim_id": "claim-3",
        "mention_id": "mention-1",
        "claim_type": "benchmark-metadata",
        "field_path": "/benchmark-metadata/organizations",
        "value_json": "['Example Institute']",
        "confidence": "high",
        "locators": [locator()],
    })
    assert json.loads(metadata_scalar.value_json) == ["Example Institute"]

    metadata_string = EvidenceClaimDraft.model_validate({
        "claim_id": "claim-4",
        "mention_id": "mention-1",
        "claim_type": "benchmark-metadata",
        "field_path": "/benchmark-metadata/name",
        "value_json": "Example benchmark",
        "confidence": "high",
        "locators": [locator()],
    })
    assert json.loads(metadata_string.value_json) == "Example benchmark"

    with pytest.raises(ValidationError):
        EvidenceClaimDraft.model_validate({
            "claim_id": "claim-5",
            "mention_id": "mention-1",
            "claim_type": "benchmark-metadata",
            "field_path": "/benchmark-metadata/organizations",
            "value_json": "Example Institute, Example University",
            "confidence": "high",
            "locators": [locator()],
        })

    with pytest.raises(ValidationError):
        EvidenceClaimDraft.model_validate({
            "claim_id": "claim-6",
            "mention_id": "mention-1",
            "claim_type": "result",
            "field_path": "/results/0",
            "value_json": "not a structured result",
            "confidence": "high",
            "locators": [locator()],
        })


def test_temporary_claim_ids_are_deterministically_rebuilt_from_mention_ownership() -> None:
    raw = draft_payload(
        [
            claim("claim-1", "relation", "evaluation"),
            claim("claim-1", "benchmark-identity", "lifescibench"),
            claim(
                "not-a-valid-claim-id",
                "paper-identity",
                {"title": "Synthetic", "doi": None, "arxiv": None},
                mention_id=None,
            ),
        ],
        {
            "mention_id": "mention-1",
            "benchmark_name": "LifeSciBench",
            "registry_benchmark_id": "lifescibench",
            "relation_type": "evaluation",
            "is_new_benchmark": False,
            "background_only": False,
            "claim_ids": ["claim-1"],
            "reporting_gaps": [],
        },
    )

    normalized = _normalize_temporary_claim_ids(raw)

    assert [item["claim_id"] for item in normalized["claims"]] == [
        "claim-1",
        "claim-2",
        "claim-3",
    ]
    assert normalized["benchmark_mentions"][0]["claim_ids"] == ["claim-1", "claim-2"]
    assert raw["claims"][1]["claim_id"] == "claim-1"
    PaperEvidenceDraft.model_validate(normalized)


def test_local_codex_stage_retries_only_transient_transport_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "draft.json"
    image_path = tmp_path / "document-page-018.jpg"
    image_path.write_bytes(b"synthetic image")
    attempts = 0
    valid_draft = draft_payload([
        claim(
            "duplicate-temporary-id",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ], {
        "mention_id": "mention-1",
        "benchmark_name": "Synthetic",
        "registry_benchmark_id": None,
        "relation_type": "background-citation",
        "is_new_benchmark": False,
        "background_only": True,
        "claim_ids": [],
        "reporting_gaps": [],
    })

    def transient_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        assert command[command.index("--image") + 1] == str(image_path)
        assert command[-2:] == ["--", "-"]
        if attempts == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                json.dumps({"type": "error", "message": "stream disconnected before completion"}),
                "",
            )
        output_path.write_text(json.dumps(valid_draft), encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"type": "thread.started", "thread_id": "thread-retry"}) + "\n",
            "",
        )

    monkeypatch.setattr("extract_paper.time.sleep", lambda _: None)
    result = _run_stage(
        prompt="Synthetic prompt",
        output_type=PaperEvidenceDraft,
        schema_path=tmp_path / "schema.json",
        output_path=output_path,
        model="gpt-5.6-sol",
        reasoning_effort="high",
        binary="codex",
        runner=transient_runner,
        image_paths=[image_path],
    )
    assert attempts == 2
    assert result.thread_id == "thread-retry"
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["claims"][0]["claim_id"] == "claim-1"


def test_temporary_claim_normalization_merges_identical_paper_identities() -> None:
    payload = draft_payload([
        claim(
            "claim-7",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id="mention-1",
        ),
        claim(
            "claim-9",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ], {
        "mention_id": "mention-1",
        "benchmark_name": "Synthetic",
        "registry_benchmark_id": None,
        "relation_type": "background-citation",
        "is_new_benchmark": False,
        "background_only": True,
        "claim_ids": ["claim-7"],
        "reporting_gaps": [],
    })

    normalized = _normalize_temporary_claim_ids(payload)

    identities = [
        item for item in normalized["claims"] if item["claim_type"] == "paper-identity"
    ]
    assert len(identities) == 1
    assert identities[0]["mention_id"] is None
    assert identities[0]["claim_id"] == "claim-1"
    assert normalized["benchmark_mentions"][0]["claim_ids"] == []


def test_local_codex_stage_rejects_conflicting_paper_identities_before_verification(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "draft.json"
    invalid_draft = draft_payload([
        claim(
            "claim-0",
            "paper-identity",
            {"title": "A conflicting paper identity", "doi": None, "arxiv": None},
            mention_id=None,
        ),
        claim(
            "claim-0",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ], {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["c1", "c2"],
        "reporting_gaps": [],
    })

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path.write_text(json.dumps(invalid_draft), encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"type": "thread.started", "thread_id": "thread-invalid"}) + "\n",
            "",
        )

    with pytest.raises(PaperExtractionError, match="exactly one unscoped paper-identity"):
        _run_stage(
            prompt="Synthetic prompt",
            output_type=PaperEvidenceDraft,
            schema_path=tmp_path / "schema.json",
            output_path=output_path,
            model="gpt-5.6-sol",
            reasoning_effort="high",
            binary="codex",
            runner=runner,
        )


def test_local_codex_stage_has_a_non_retrying_wall_clock_limit(
    tmp_path: Path,
) -> None:
    attempts = 0

    def timeout_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        assert kwargs["timeout"] == 45 * 60
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(CodexExecutionError, match="45-minute wall-clock limit"):
        _run_stage(
            prompt="Synthetic prompt",
            output_type=PaperEvidenceDraft,
            schema_path=tmp_path / "schema.json",
            output_path=tmp_path / "draft.json",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            binary="codex",
            runner=timeout_runner,
        )
    assert attempts == 1


class FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "application/pdf", declared: int | None = None):
        self.body = body
        self.headers = {"Content-Type": content_type}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)
        self.url = "https://arxiv.org/pdf/2601.00001.pdf"

    def raise_for_status(self) -> None: pass
    def iter_content(self, chunk_size: int):
        yield self.body


def pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages): writer.add_blank_page(width=72, height=72)
    output = io.BytesIO(); writer.write(output); return output.getvalue()


def test_pdf_visual_pages_are_temporary_numbered_image_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(pdf_bytes(2))
    monkeypatch.setattr("extract_paper.shutil.which", lambda _: "/usr/bin/pdftoppm")

    def render_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(f"{command[-1]}.jpg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, "", "")

    assert _pdf_pages_for_visual_review(source) == [1, 2]
    images = _render_pdf_pages(source, tmp_path, runner=render_runner)
    assert [path.name for path in images] == [
        "document-page-001.jpg",
        "document-page-002.jpg",
    ]
    assert _pdf_pages_for_visual_review(source, preferred_pages=[2, 2]) == [2]
    focused_images = _render_pdf_pages(
        source,
        tmp_path / "focused",
        preferred_pages=[2],
        runner=render_runner,
    )
    assert [path.name for path in focused_images] == ["document-page-002.jpg"]


def test_pdf_focus_parses_only_explicit_page_locators() -> None:
    assert _focus_pdf_pages(
        "Review pages 15-25, page 18, and pages 187\u2013190. "
        "The benchmark has 1260 attempts and 57 participants."
    ) == [*range(15, 26), *range(187, 191)]
    assert _focus_pdf_pages("Section 8.17 and 1260 attempts") is None
    with pytest.raises(GenerationBlocked, match="at most 40 pages"):
        _focus_pdf_pages("pages 1-41")


def test_focused_long_pdf_text_preserves_original_physical_page_markers(tmp_path: Path) -> None:
    source = tmp_path / "long.pdf"
    source.write_bytes(pdf_bytes(151))
    focused = _focused_pdf_text_source(
        source,
        tmp_path / "focused.txt",
        pages=[151, 1, 151],
    )
    text = focused.read_text(encoding="utf-8")
    assert text.count("=== DOCUMENT PAGE 1 ===") == 1
    assert text.count("=== DOCUMENT PAGE 151 ===") == 1
    assert "original PDF physical page (1-based)" in text


def test_pdf_preprocessor_anchors_every_page_and_bounds_verifier_context(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(pdf_bytes(8))
    prepared = _page_anchored_pdf_text_source(
        source,
        tmp_path / "prepared.txt",
    )
    text = prepared.read_text(encoding="utf-8")
    assert "complete paper review" in text
    assert "contains 8 of 8 physical pages" in text
    assert text.count("=== DOCUMENT PAGE ") == 8

    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-1", "claim-2"],
        "reporting_gaps": [],
    }
    claims = [
        claim("claim-1", "relation", "evaluation"),
        claim("claim-2", "benchmark-identity", "lifescibench"),
        claim(
            "claim-3",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
            mention_id=None,
        ),
    ]
    for item in claims:
        item["locators"][0]["document_page"] = 5
    draft = PaperEvidenceDraft.model_validate(draft_payload(claims, mention))
    assert _verifier_pdf_context_pages(draft, page_count=8) == [1, 4, 5, 6]

    verifier_packet = _page_anchored_pdf_text_source(
        source,
        tmp_path / "verifier.txt",
        pages=_verifier_pdf_context_pages(draft, page_count=8),
        purpose="independent claim verification",
    ).read_text(encoding="utf-8")
    assert "=== DOCUMENT PAGE 5 ===" in verifier_packet
    assert "=== DOCUMENT PAGE 8 ===" not in verifier_packet
    assert "contains 4 of 8 physical pages" in verifier_packet


def test_html_source_uses_visible_text_without_scripts(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    visible_sentence = "BioMysteryBench has 99 questions and five trials per task. "
    source.write_text(
        "<!doctype html><html><head><script>private_payload = 'ignore me'</script></head>"
        f"<body><article><h1>Official evaluation</h1><p>{visible_sentence * 20}</p>"
        "</article></body></html>",
        encoding="utf-8",
    )
    visible, original = _prepare_local_text_source(source, tmp_path)
    assert visible.name == "source-visible.txt"
    assert original is not None and original.name == "source-original.html"
    normalized = visible.read_text(encoding="utf-8")
    assert "BioMysteryBench has 99 questions" in normalized
    assert "private_payload" not in normalized
    assert "private_payload" in original.read_text(encoding="utf-8")


def test_html_review_fingerprint_ignores_scripts_but_detects_visible_changes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.html"
    second = tmp_path / "second.html"
    third = tmp_path / "third.html"
    visible = "SpatialBench contains 146 verifiable benchmark problems. " * 12
    first.write_text(
        f"<html><body><p>{visible}</p><script>build='a'</script></body></html>",
        encoding="utf-8",
    )
    second.write_text(
        f"<html><body><p>{visible}</p><script>build='b'</script></body></html>",
        encoding="utf-8",
    )
    third.write_text(
        f"<html><body><p>{visible}Visible revision.</p><script>build='b'</script></body></html>",
        encoding="utf-8",
    )
    assert review_source_sha256(first) == review_source_sha256(second)
    assert review_source_sha256(first) != review_source_sha256(third)


def test_golden_checkpoint_requires_matching_case_and_source_fingerprints() -> None:
    progress = {
        "completed_cases": ["lifescibench"],
        "source_fingerprints": {"lifescibench": "sha256-a"},
    }
    assert _checkpoint_case_current(
        progress,
        "lifescibench",
        {"lifescibench": "sha256-a"},
    )
    assert not _checkpoint_case_current(
        progress,
        "lifescibench",
        {"lifescibench": "sha256-b"},
    )
    assert not _checkpoint_case_current(
        progress,
        "biomysterybench",
        {"biomysterybench": "sha256-a"},
    )


def test_golden_source_fingerprint_includes_owner_selected_review_focus(tmp_path: Path) -> None:
    source_path = tmp_path / "source.txt"
    source_path.write_text("BixBench official benchmark comparison. " * 20, encoding="utf-8")
    plain = GoldenSource("plain", "https://example.test/plain", "bixbench")
    focused = GoldenSource(
        "focused",
        "https://example.test/focused",
        "bixbench",
        review_focus={"benchmark_hints": "BixBench"},
    )

    assert _golden_source_fingerprint(plain, source_path) == review_source_sha256(source_path)
    assert _golden_source_fingerprint(focused, source_path) != review_source_sha256(source_path)


def test_biomystery_golden_focus_excludes_related_work_without_answer_values() -> None:
    from paper_extraction_eval import SOURCES

    source = next(item for item in SOURCES if item.name == "biomysterybench")
    assert source.review_focus is not None
    focus = " ".join(source.review_focus.values())
    assert "other named benchmarks as background" in focus
    assert "Human-solvable and Human-difficult" in focus
    assert not any(value in focus.split() for value in ("99", "76", "23"))


def test_spatialbench_golden_focus_prioritizes_overall_counts_without_answer_values() -> None:
    from paper_extraction_eval import SOURCES

    sources = {
        item.name: item
        for item in SOURCES
        if item.name in {"spatialbench-paper-v2", "spatialbench-repository"}
    }
    assert set(sources) == {"spatialbench-paper-v2", "spatialbench-repository"}
    for source in sources.values():
        assert source.review_focus is not None
        focus = " ".join(source.review_focus.values())
        assert "overall" in focus
        assert "separately" in focus
        assert not any(value in focus.split() for value in ("146", "159"))


def test_golden_total_count_does_not_depend_on_model_generated_label_wording() -> None:
    payloads = [(
        "benchmark-count",
        {
            "label": "Questions in BioMysteryBench",
            "count": 99,
            "unit": "questions",
            "reporting_status": "reported",
        },
    )]
    assert _has_count_value(payloads, 99)
    assert not _has_count_value(payloads, 100)


def test_spatial_golden_accepts_verified_scope_or_result_sample_size() -> None:
    assert _has_evaluation_size([("scope-n", 146)], 146)
    assert _has_evaluation_size([(
        "result",
        {"n": 159, "value": 53.67, "numeric_source": "table"},
    )], 159)
    assert not _has_evaluation_size([("scope-n", 147)], 146)


def test_verifier_receives_only_extractor_cited_visual_pages(tmp_path: Path) -> None:
    images = [
        tmp_path / "document-page-003.jpg",
        tmp_path / "document-page-018.jpg",
        tmp_path / "document-page-020.jpg",
    ]
    count_claim = claim(
        "claim-1",
        "benchmark-count",
        {
            "label": "Protein domain total",
            "count": 136,
            "unit": "tasks",
            "basis": "Figure cell total",
            "reporting_status": "reported",
            "subset_id": "protein",
            "exclusive": False,
            "exhaustive": False,
            "partition_group": None,
        },
    )
    count_claim["locators"] = [locator() | {
        "locator_type": "figure",
        "value": "Figure 13",
        "document_page": 18,
    }]
    draft = PaperEvidenceDraft.model_validate(draft_payload(
        [count_claim],
        {
            "mention_id": "mention-1",
            "benchmark_name": "LifeSciBench",
            "registry_benchmark_id": "lifescibench",
            "relation_type": "evaluation",
            "is_new_benchmark": False,
            "background_only": False,
            "claim_ids": ["claim-1"],
            "reporting_gaps": [],
        },
    ))
    assert _verifier_source_images(images, draft) == [images[1]]


def test_long_image_only_pdf_stops_instead_of_silently_skipping_visual_pages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "long-paper.pdf"
    source.write_bytes(pdf_bytes(41))
    with pytest.raises(PaperExtractionError, match="more than 40 pages requiring visual review"):
        _pdf_pages_for_visual_review(source)


def test_source_rights_mime_size_pages_and_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("paper_source.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, '', ('151.101.1.69', 443))])
    assert is_automatic_source_allowed("https://arxiv.org/pdf/x.pdf", rights_confirmed=False, discovered=True)
    assert not is_automatic_source_allowed("https://publisher.example/x.pdf", rights_confirmed=False, discovered=True)
    body = pdf_bytes(2)
    monkeypatch.setattr("paper_source.requests.get", lambda *args, **kwargs: FakeResponse(body))
    source = retrieve_source("https://arxiv.org/pdf/2601.00001.pdf", rights_confirmed=False, discovered=True)
    try:
        assert source.page_count == 2 and len(source.content_sha256) == 64
    finally:
        source.path.unlink(missing_ok=True)
    monkeypatch.setattr("paper_source.requests.get", lambda *args, **kwargs: FakeResponse(b"", declared=MAX_SOURCE_BYTES + 1))
    with pytest.raises(SourceAcquisitionError, match="45 MiB"):
        retrieve_source("https://arxiv.org/pdf/2601.00001.pdf", rights_confirmed=False, discovered=True)
    monkeypatch.setattr("paper_source.requests.get", lambda *args, **kwargs: FakeResponse(pdf_bytes(151)))
    with pytest.raises(SourceAcquisitionError, match="150-page"):
        retrieve_source("https://arxiv.org/pdf/2601.00001.pdf", rights_confirmed=False, discovered=True)
    focused = retrieve_source(
        "https://arxiv.org/pdf/2601.00001.pdf",
        rights_confirmed=False,
        discovered=True,
        preferred_pdf_pages=[1, 151],
    )
    try:
        assert focused.page_count == 151
    finally:
        focused.path.unlink(missing_ok=True)
    with pytest.raises(SourceAcquisitionError, match="out-of-range"):
        retrieve_source(
            "https://arxiv.org/pdf/2601.00001.pdf",
            rights_confirmed=False,
            discovered=True,
            preferred_pdf_pages=[152],
        )


def test_source_download_retries_transient_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "paper_source.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("151.101.1.69", 443))],
    )
    calls = 0
    body = pdf_bytes(1)

    def transient_get(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise requests.exceptions.SSLError("transient TLS EOF")
        return FakeResponse(body)

    monkeypatch.setattr("paper_source.requests.get", transient_get)
    monkeypatch.setattr("paper_source.time.sleep", lambda _: None)
    source = retrieve_source(
        "https://arxiv.org/pdf/2601.00001.pdf",
        rights_confirmed=False,
        discovered=True,
    )
    try:
        assert calls == 3
        assert source.page_count == 1
    finally:
        source.path.unlink(missing_ok=True)


def test_github_repository_pin_request_retries_transient_tls_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class ApiResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"default_branch": "main"}

    def transient_get(*args: Any, **kwargs: Any) -> ApiResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise requests.exceptions.SSLError("transient GitHub API TLS EOF")
        return ApiResponse()

    monkeypatch.setattr("run_paper_intake.requests.get", transient_get)
    monkeypatch.setattr("run_paper_intake.time.sleep", lambda _: None)
    assert _github_json_request(
        "https://api.github.com/repos/example/benchmark",
        headers={"Accept": "application/vnd.github+json"},
    ) == {"default_branch": "main"}
    assert calls == 3


def test_zenodo_official_dataset_resolves_to_immutable_version_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "benchmark-creation"),
        claim("claim-3", "benchmark-identity", "SyntheticDataBench"),
        claim("claim-4", "official-resource", {
            "url": "https://doi.org/10.5281/zenodo.11164565",
            "resource_type": "dataset", "license": None, "version": None,
        }),
    ]
    mention = {
        "mention_id": "mention-1", "benchmark_name": "SyntheticDataBench",
        "registry_benchmark_id": None, "relation_type": "benchmark-creation",
        "is_new_benchmark": True, "background_only": False,
        "claim_ids": ["claim-2", "claim-3", "claim-4"], "reporting_gaps": [],
    }
    payload = verified_result(claims, mention)
    result = SimpleNamespace(
        draft=PaperEvidenceDraft.model_validate(payload["draft"]),
        verification=PaperEvidenceVerification.model_validate(payload["verification"]),
    )

    class ZenodoResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "id": 11164566,
                "doi": "10.5281/zenodo.11164566",
                "metadata": {"version": "1.0.0", "license": {"id": "cc-by-4.0"}},
                "links": {"self_html": "https://zenodo.org/records/11164566"},
            }

    monkeypatch.setattr("run_paper_intake.requests.get", lambda *args, **kwargs: ZenodoResponse())
    assert resolve_resource_pins(result) == {
        "https://doi.org/10.5281/zenodo.11164565": {
            "resource_type": "dataset", "kind": "version", "value": "1.0.0",
            "url": "https://zenodo.org/records/11164566",
            "resolved_url": "https://zenodo.org/records/11164566",
            "license": "cc-by-4.0",
        }
    }


def test_crossref_resolution_retries_transient_tls_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class CrossrefResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "message": {
                    "title": ["PPB-Affinity"],
                    "author": [{"given": "A.", "family": "Researcher"}],
                    "published-print": {"date-parts": [[2024]]},
                    "published-online": {"date-parts": [[2024, 12, 3]]},
                    "DOI": "10.1038/s41597-024-03997-4",
                    "URL": "https://doi.org/10.1038/s41597-024-03997-4",
                    "publisher": "Springer Science and Business Media LLC",
                }
            }

    def transient_get(*args: Any, **kwargs: Any) -> CrossrefResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise requests.exceptions.SSLError("transient Crossref TLS EOF")
        return CrossrefResponse()

    monkeypatch.setattr("triage_paper.requests.get", transient_get)
    monkeypatch.setattr("triage_paper.time.sleep", lambda _: None)
    metadata = resolve_crossref("10.1038/s41597-024-03997-4")
    assert calls == 3
    assert metadata["publication_date"] == "2024-12-03"
    assert metadata["source"] == "Crossref"


def test_crossref_date_parts_does_not_invent_a_day_for_year_only_metadata() -> None:
    from triage_paper import _date_parts

    assert _date_parts({"published": {"date-parts": [[2025]]}}) is None


def test_work_ids_are_deterministic_and_workflows_have_required_guards() -> None:
    assert stable_work_id("A Test Paper", "10.1/x", set()) == "a-test-paper"
    assert stable_work_id("A Test Paper", "10.1/x", {"a-test-paper"}).startswith("a-test-paper-")
    assert not (ROOT / ".github/workflows/paper-intake.yml").exists()
    assert not (ROOT / ".github/workflows/paper-extraction-eval.yml").exists()
    owner = (ROOT / ".github/workflows/paper-owner-gate.yml").read_text(encoding="utf-8")
    discovery = (ROOT / ".github/workflows/discover-papers.yml").read_text(encoding="utf-8")
    issue_triage = (ROOT / ".github/workflows/triage-paper-issues.yml").read_text(encoding="utf-8")
    validate_workflow = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    paper_form = (ROOT / ".github/ISSUE_TEMPLATE/review-paper.yml").read_text(encoding="utf-8")
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    )
    production_scripts = "\n".join(
        (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "extract_paper.py",
            "local_paper_intake.py",
            "paper_extraction_eval.py",
            "run_paper_intake.py",
        )
    )
    assert "issue_comment:" in owner
    assert "/approve-paper-intake" in owner
    assert "checks: write" in owner
    assert "statuses: write" in owner
    assert 'statuses/${head_sha}' in owner
    assert "context='paper-owner-gate'" in owner
    assert "comment_pages_json" in owner
    assert "jq 'add' \"$comment_pages_json\"" in owner
    assert "--slurp \\\n              --jq" not in owner
    assert "ready-for-local-intake" in discovery
    assert "local-intake-in-progress" in discovery
    assert "types: [opened, edited, reopened]" in issue_triage
    assert "--add-label paper-candidate" in issue_triage
    assert "ready-for-local-intake" not in issue_triage
    assert "local-intake-in-progress" not in issue_triage
    assert "labels: [paper-candidate]" in paper_form
    assert "labels: [paper-intake, paper-candidate]" not in paper_form
    assert "registry-tests:" in validate_workflow
    assert "paper-tests:" in validate_workflow
    assert "results-tests:" in validate_workflow
    assert "build:" in validate_workflow
    assert "needs: [registry-tests, paper-tests, results-tests, build]" in validate_workflow
    assert "\n  validate:\n" in validate_workflow
    assert "OPENAI" + "_API_KEY" not in workflows
    assert "create-github-app-token" not in workflows
    assert "OPENAI" + "_API_KEY" not in production_scripts
    assert "api." + "openai.com" not in production_scripts
    assert "responses." + "create" not in production_scripts
    assert "files." + "create" not in production_scripts


def test_schema_and_work_export_publish_review_provenance_contract() -> None:
    schema = json.loads((ROOT / "schema" / "registry.schema.json").read_text())
    assert "review_provenance" in schema["$defs"]["Work"]["properties"]
    locator_fields = schema["$defs"]["Locator"]["properties"]
    assert {"document_page", "printed_page", "source_fragment_sha256"} <= set(locator_fields)
    build_registry()
    with (ROOT / "exports" / "works.csv").open(newline="", encoding="utf-8") as handle:
        fields = next(csv.reader(handle))
    assert {
        "review_method", "ai_assisted", "owner_reviewed", "pipeline_version",
        "source_content_sha256", "review_surface", "codex_cli_version",
        "model_resolution_status", "local_run_id",
    } <= set(fields)


def test_generator_emits_local_codex_provenance_without_claiming_resolved_models() -> None:
    claims = [
        claim("claim-1", "paper-identity", {"title": "Synthetic benchmark evaluation paper"}, mention_id=None),
        claim("claim-2", "relation", "evaluation"),
        claim("claim-3", "benchmark-identity", "lifescibench"),
    ]
    mention = {
        "mention_id": "mention-1",
        "benchmark_name": "LifeSciBench",
        "registry_benchmark_id": "lifescibench",
        "relation_type": "evaluation",
        "is_new_benchmark": False,
        "background_only": False,
        "claim_ids": ["claim-2", "claim-3"],
        "reporting_gaps": ["benchmark version", "realized n", "metric"],
    }
    records = build_records(
        local_verified_result(claims, mention),
        source=SOURCE,
        generated_at=SOURCE["retrieved_at"],
        verified_on="2026-07-22",
    )
    provenance = records.work["review_provenance"]
    assert provenance["method"] == "local-codex-double-pass"
    assert provenance["execution_surface"] == "local-codex-cli"
    assert provenance["model_resolution_status"] == "not-reported"
    assert provenance["extractor_model_resolved"] is None
    assert provenance["verifier_model_resolved"] is None
