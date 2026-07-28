from __future__ import annotations

import io
import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
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
    deduplicate_candidates,
    fetch_europe_pmc,
    score_candidate,
    select_by_quota,
)
from generate_paper_records import (  # noqa: E402
    GenerationBlocked,
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
    _pdf_pages_for_visual_review,
    _prepare_local_text_source,
    _render_pdf_pages,
    _run_stage,
    _structured_output_diagnostic,
    _verifier_source_images,
    review_source_sha256,
    run_double_pass,
)
from paper_models import (  # noqa: E402
    EvidenceClaimDraft,
    LocatorDraft,
    PaperEvidenceDraft,
    PaperEvidenceVerification,
    accepted_claims,
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
    LocalIntakeError,
    _ensure_labels,
    _owner_conflict_resolution,
    _run,
    heartbeat_status,
)
from paper_source import (  # noqa: E402
    MAX_SOURCE_BYTES,
    SourceAcquisitionError,
    is_automatic_source_allowed,
    retrieve_source,
)
from registry_io import load_entities  # noqa: E402
from run_paper_intake import _focus_pdf_pages  # noqa: E402
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


def claim(claim_id: str, claim_type: str, value: Any, *, mention_id: str | None = "mention-1") -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "mention_id": mention_id,
        "claim_type": claim_type,
        "field_path": f"/claims/{claim_id}",
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
    from extract_paper import EXTRACTOR_PROMPT, PROMPT_VERSION, VERIFIER_PROMPT

    assert PROMPT_VERSION == "paper-evidence-local-v7"
    assert "Normalize arXiv identifiers to the base numeric ID" in EXTRACTOR_PROMPT
    assert "the suffix belongs to the paper version" in VERIFIER_PROMPT
    assert "Benchmark-creation and evaluation are compatible" in VERIFIER_PROMPT
    assert "Their coexistence is not a conflict" in VERIFIER_PROMPT
    assert "keep benchmark-metadata count-, version-, protocol-, and" in EXTRACTOR_PROMPT
    assert "Do not duplicate those creator-only claims" in EXTRACTOR_PROMPT
    assert "explicitly inspect the abstract, introduction" in EXTRACTOR_PROMPT
    assert "not permission to sum" in EXTRACTOR_PROMPT
    assert "must not be copied into benchmark-metadata" in VERIFIER_PROMPT


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


def test_new_benchmark_requires_creator_repo_pin_and_builds_same_pr_entities() -> None:
    metadata = {
        "name": "SyntheticBioBench", "aliases": [],
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
    assert records.benchmarks[0]["resources"][1]["pin"]["value"] == "b" * 40
    assert records.classifications["syntheticbiobench"]["entries"][0]["task_type_id"] == "protein-fitness-prediction"
    assert records.uses[0]["relation_type"] == "benchmark-creation"

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
        if item["claim_id"] in {"claim-10", "claim-14", "claim-15", "claim-17", "claim-18", "claim-19"}:
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
    verification = {
        "source_parseable": True,
        "blocking_conflicts": [],
        "claims": [{
            "claim_id": claim_id,
            "verdict": "supported",
            "confidence": "high",
            "locator": locator(),
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
        match = re.search(r"Read the source at (\S+)", prompt)
        assert match is not None
        focused_path = Path(match.group(1).rstrip("."))
        focused_text = focused_path.read_text(encoding="utf-8")
        assert "=== DOCUMENT PAGE 1 ===" in focused_text
        assert "=== DOCUMENT PAGE 151 ===" in focused_text
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
    state = heartbeat_status(now=datetime.fromisoformat("2026-07-23T10:01:00+00:00"))
    assert state["process_alive"] is False
    assert state["stale"] is True
    assert state["heartbeat_age_seconds"] == 60


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

    with pytest.raises(ValidationError):
        EvidenceClaimDraft.model_validate({
            "claim_id": "claim-3",
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


def test_local_codex_stage_rejects_structurally_incomplete_draft_before_verification(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "draft.json"
    invalid_draft = draft_payload([
        claim(
            "claim-0",
            "paper-identity",
            {"title": "Synthetic benchmark evaluation paper", "doi": None, "arxiv": None},
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


def test_work_ids_are_deterministic_and_workflows_have_required_guards() -> None:
    assert stable_work_id("A Test Paper", "10.1/x", set()) == "a-test-paper"
    assert stable_work_id("A Test Paper", "10.1/x", {"a-test-paper"}).startswith("a-test-paper-")
    assert not (ROOT / ".github/workflows/paper-intake.yml").exists()
    assert not (ROOT / ".github/workflows/paper-extraction-eval.yml").exists()
    owner = (ROOT / ".github/workflows/paper-owner-gate.yml").read_text(encoding="utf-8")
    discovery = (ROOT / ".github/workflows/discover-papers.yml").read_text(encoding="utf-8")
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
    assert "comment_pages_json" in owner
    assert "jq 'add' \"$comment_pages_json\"" in owner
    assert "--slurp \\\n              --jq" not in owner
    assert "ready-for-local-intake" in discovery
    assert "local-intake-in-progress" in discovery
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
