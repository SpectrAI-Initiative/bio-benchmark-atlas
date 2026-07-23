from __future__ import annotations

import io
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
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
from generate_paper_records import build_records, stable_work_id, write_records  # noqa: E402
from extract_paper import (  # noqa: E402
    EXTRACTOR_PROMPT,
    VERIFIER_PROMPT,
    _child_environment,
    run_double_pass,
)
from paper_models import (  # noqa: E402
    LocatorDraft,
    PaperEvidenceDraft,
    PaperEvidenceVerification,
    accepted_claims,
)
from paper_source import (  # noqa: E402
    MAX_SOURCE_BYTES,
    SourceAcquisitionError,
    is_automatic_source_allowed,
    retrieve_source,
)
from registry_io import load_entities  # noqa: E402
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


def test_structured_output_rejects_long_quotes_and_unlabeled_graph_values() -> None:
    with pytest.raises(ValidationError):
        LocatorDraft(**locator(" ".join(["word"] * 21)))
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
        "claim_ids": [item["claim_id"] for item in claims if item["mention_id"]], "reporting_gaps": [],
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
        "claim_ids": ["claim-1"],
        "reporting_gaps": [],
    }
    claims = [claim("claim-1", "relation", "evaluation")]
    draft = draft_payload(claims, mention)
    verification = {
        "source_parseable": True,
        "blocking_conflicts": [],
        "claims": [{
            "claim_id": "claim-1",
            "verdict": "supported",
            "confidence": "high",
            "locator": locator(),
            "notes": None,
        }],
    }
    source = tmp_path / "paper.txt"
    source.write_text("Synthetic evidence source.", encoding="utf-8")
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    stage = 0

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal stage
        commands.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3\n", "")
        stage += 1
        environments.append(kwargs["env"])
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
    monkeypatch.setattr("extract_paper.LOCAL_TMP_ROOT", temporary_root)
    secret_name = "OPENAI" + "_API_KEY"
    monkeypatch.setenv(secret_name, "must-not-propagate")
    result = run_double_pass(
        source,
        registry_context={"benchmarks": [], "models": [], "taxonomy_ids": {}},
        binary="codex",
        runner=runner,
    )
    assert result.extractor_thread_id == "thread-1"
    assert result.verifier_thread_id == "thread-2"
    assert result.accepted_claim_ids == ["claim-1"]
    assert len(environments) == 2
    assert all(secret_name not in environment for environment in environments)
    for command in commands[:2]:
        assert {"--ephemeral", "--ignore-user-config", "--output-schema"} <= set(command)
        assert command[command.index("--sandbox") + 1] == "read-only"
    assert not temporary_root.exists()
    assert "untrusted" in EXTRACTOR_PROMPT
    assert "Do not use the network" in EXTRACTOR_PROMPT
    assert "independent verifier" in VERIFIER_PROMPT


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
