#!/usr/bin/env python3
"""Local Codex golden evaluation for the pinned paper evidence prompt/model pair."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extract_paper import (
    DEFAULT_MODEL,
    EXTRACTOR_PROMPT,
    PROMPT_VERSION,
    SOURCE_INPUT_PROTOCOL_VERSION,
    VERIFIER_PROMPT,
    codex_version,
    review_source_sha256,
    run_double_pass,
)
from paper_models import PaperEvidenceDraft, PaperEvidenceVerification, accepted_claims
from paper_source import retrieve_source
from run_paper_intake import registry_context


@dataclass(frozen=True)
class GoldenSource:
    name: str
    url: str
    benchmark_id: str


SOURCES = [
    GoldenSource(
        "lifescibench",
        "https://cdn.openai.com/pdf/b4299379-0a97-4ffa-8b9b-c3fbb299caa9/lifescibench_preprint.pdf",
        "lifescibench",
    ),
    GoldenSource(
        "biomysterybench",
        "https://www.anthropic.com/research/Evaluating-Claude-For-Bioinformatics-With-BioMysteryBench",
        "biomysterybench",
    ),
    GoldenSource(
        "spatialbench-paper-v2",
        "https://arxiv.org/pdf/2512.21907v2.pdf",
        "spatialbench",
    ),
    GoldenSource(
        "spatialbench-repository",
        "https://raw.githubusercontent.com/latchbio/spatialbench/5042c4f3ee597da1590650c7b894d068ae968e26/README.md",
        "spatialbench",
    ),
    GoldenSource(
        "anthropic-bixbench",
        "https://www.anthropic.com/news/claude-for-life-sciences",
        "bixbench",
    ),
]


class GoldenFailure(RuntimeError):
    pass


def _claim_payloads(result: Any, benchmark_id: str, claim_type: str | None = None) -> list[tuple[str, Any]]:
    accepted = {claim.claim_id: claim for claim in accepted_claims(result.draft, result.verification)}
    mention_ids = {
        mention.mention_id for mention in result.draft.benchmark_mentions
        if mention.registry_benchmark_id == benchmark_id
    }
    payloads = []
    for claim in accepted.values():
        if claim.mention_id not in mention_ids or (claim_type and claim.claim_type != claim_type):
            continue
        payloads.append((claim.claim_type, json.loads(claim.value_json)))
    return payloads


def _count(payloads: list[tuple[str, Any]], expected: int, label_pattern: str) -> bool:
    label_pattern = label_pattern.casefold()
    for kind, payload in payloads:
        if kind != "benchmark-count" or not isinstance(payload, dict):
            continue
        if payload.get("count") == expected and label_pattern in str(payload.get("label", "")).casefold():
            return True
    return False


def _has_count_value(payloads: list[tuple[str, Any]], expected: int) -> bool:
    return any(
        kind == "benchmark-count"
        and isinstance(payload, dict)
        and payload.get("count") == expected
        for kind, payload in payloads
    )


def _has_evaluation_size(payloads: list[tuple[str, Any]], expected: int) -> bool:
    for kind, payload in payloads:
        if kind == "benchmark-count" and isinstance(payload, dict):
            if payload.get("count") == expected:
                return True
        elif kind == "scope-n" and payload == expected:
            return True
        elif kind == "result" and isinstance(payload, dict):
            if payload.get("n") == expected:
                return True
    return False


def _observed_evaluation_sizes(payloads: list[tuple[str, Any]]) -> list[int]:
    values = set(_observed_count_values(payloads))
    for kind, payload in payloads:
        if kind == "scope-n" and isinstance(payload, int):
            values.add(payload)
        elif kind == "result" and isinstance(payload, dict) and isinstance(payload.get("n"), int):
            values.add(payload["n"])
    return sorted(values)


def _observed_count_values(payloads: list[tuple[str, Any]]) -> list[int]:
    return sorted({
        payload["count"]
        for kind, payload in payloads
        if (
            kind == "benchmark-count"
            and isinstance(payload, dict)
            and isinstance(payload.get("count"), int)
        )
    })


def _evaluate_lifescibench(result: Any) -> None:
    life = _claim_payloads(result, "lifescibench")
    for expected, label in ((750, "total"), (136, "protein"), (62, "design")):
        if not _count(life, expected, label):
            raise GoldenFailure(
                f"LifeSciBench missing verified {label} count {expected}; "
                f"observed verified counts={_observed_count_values(life)}"
            )
    for kind, payload in life:
        if kind == "benchmark-count" and isinstance(payload, dict):
            if "binding" in str(payload.get("label", "")).casefold() and payload.get("count") is not None:
                raise GoldenFailure("LifeSciBench invented a binding count")


def _evaluate_biomysterybench(result: Any) -> None:
    mystery = _claim_payloads(result, "biomysterybench")
    if not _has_count_value(mystery, 99):
        raise GoldenFailure(
            "BioMysteryBench missing verified benchmark count 99; "
            f"observed verified counts={_observed_count_values(mystery)}"
        )
    for expected, label in ((76, "human-solvable"), (23, "human-difficult")):
        if not _count(mystery, expected, label):
            raise GoldenFailure(
                f"BioMysteryBench missing verified {label} count {expected}; "
                f"observed verified counts={_observed_count_values(mystery)}"
            )
    if not any(kind == "repeats" and payload == 5 for kind, payload in mystery):
        raise GoldenFailure("BioMysteryBench missing five verified repeats")


def _evaluate_spatialbench(paper_result: Any, repository_result: Any) -> None:
    spatial_paper = _claim_payloads(paper_result, "spatialbench")
    spatial_repo = _claim_payloads(repository_result, "spatialbench")
    if not _has_evaluation_size(spatial_paper, 146):
        raise GoldenFailure(
            "SpatialBench paper-v2 evaluation size 146 is missing; "
            f"observed verified sizes={_observed_evaluation_sizes(spatial_paper)}"
        )
    if not _has_evaluation_size(spatial_repo, 159):
        raise GoldenFailure(
            "SpatialBench repository snapshot size 159 is missing; "
            f"observed verified sizes={_observed_evaluation_sizes(spatial_repo)}"
        )
    paper_versions = {str(payload) for kind, payload in spatial_paper if kind == "benchmark-version"}
    repo_versions = {str(payload) for kind, payload in spatial_repo if kind == "benchmark-version"}
    if paper_versions & repo_versions:
        raise GoldenFailure("SpatialBench 146 and 159 snapshots were assigned the same version")


def _evaluate_anthropic_bixbench(result: Any) -> None:
    bix = _claim_payloads(result, "bixbench")
    if any(kind == "result" for kind, _ in bix):
        raise GoldenFailure("Anthropic × BixBench produced a numeric result claim")
    if not any(kind == "relation" and payload == "evaluation" for kind, payload in bix):
        raise GoldenFailure("Anthropic × BixBench evaluation relation is missing")


def evaluate_results(results: dict[str, Any]) -> dict[str, Any]:
    _evaluate_lifescibench(results["lifescibench"])
    _evaluate_biomysterybench(results["biomysterybench"])
    _evaluate_spatialbench(
        results["spatialbench-paper-v2"],
        results["spatialbench-repository"],
    )
    _evaluate_anthropic_bixbench(results["anthropic-bixbench"])
    return {
        "passed": True,
        "cases": 4,
        "sources": len(SOURCES),
        "assertions": 13,
        "note": "Only aggregate pass/fail metadata is retained; paper excerpts are not persisted.",
    }


def golden_input_hash(extractor_model: str, verifier_model: str) -> str:
    payload = {
        "prompt_version": PROMPT_VERSION,
        "source_input_protocol_version": SOURCE_INPUT_PROTOCOL_VERSION,
        "extractor_prompt": EXTRACTOR_PROMPT,
        "verifier_prompt": VERIFIER_PROMPT,
        "extractor_schema": PaperEvidenceDraft.model_json_schema(),
        "verifier_schema": PaperEvidenceVerification.model_json_schema(),
        "extractor_model": extractor_model,
        "verifier_model": verifier_model,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_case_current(
    progress: dict[str, Any],
    case_name: str,
    current_fingerprints: dict[str, str],
) -> bool:
    completed_cases = set(progress.get("completed_cases", []))
    fingerprints = progress.get("source_fingerprints", {})
    return (
        case_name in completed_cases
        and isinstance(fingerprints, dict)
        and all(fingerprints.get(name) == digest for name, digest in current_fingerprints.items())
    )


def run_golden(
    *,
    output: Path,
    extractor_model: str = DEFAULT_MODEL,
    verifier_model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    progress_path = output.with_name("golden-progress.json")
    input_hash = golden_input_hash(extractor_model, verifier_model)
    cli_version = codex_version()
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        progress = {}
    if (
        progress.get("input_hash") != input_hash
        or progress.get("codex_cli_version") != cli_version
    ):
        progress = {
            "input_hash": input_hash,
            "codex_cli_version": cli_version,
            "completed_cases": [],
            "source_fingerprints": {},
        }

    sources_by_name = {source.name: source for source in SOURCES}
    case_sources = {
        "lifescibench": ["lifescibench"],
        "biomysterybench": ["biomysterybench"],
        "spatialbench-version-separation": [
            "spatialbench-paper-v2",
            "spatialbench-repository",
        ],
        "anthropic-bixbench": ["anthropic-bixbench"],
    }
    completed_cases = set(progress.get("completed_cases", [])) & set(case_sources)
    fingerprints = dict(progress.get("source_fingerprints", {}))

    for case_name, source_names in case_sources.items():
        retrieved_sources = {}
        try:
            for name in source_names:
                retrieved_sources[name] = retrieve_source(
                    sources_by_name[name].url,
                    rights_confirmed=True,
                )
            current_fingerprints = {
                name: review_source_sha256(retrieved_sources[name].path)
                for name in source_names
            }
            if _checkpoint_case_current(progress, case_name, current_fingerprints):
                print(f"golden case {case_name}: resumed from matching safe checkpoint", flush=True)
                continue

            print(f"golden case {case_name}: started", flush=True)
            case_results = {
                name: run_double_pass(
                    retrieved_sources[name].path,
                    registry_context=registry_context(),
                    extractor_model=extractor_model,
                    verifier_model=verifier_model,
                )
                for name in source_names
            }
            if case_name == "lifescibench":
                _evaluate_lifescibench(case_results["lifescibench"])
            elif case_name == "biomysterybench":
                _evaluate_biomysterybench(case_results["biomysterybench"])
            elif case_name == "spatialbench-version-separation":
                _evaluate_spatialbench(
                    case_results["spatialbench-paper-v2"],
                    case_results["spatialbench-repository"],
                )
            else:
                _evaluate_anthropic_bixbench(case_results["anthropic-bixbench"])
            completed_cases.add(case_name)
            fingerprints.update(current_fingerprints)
            progress = {
                "input_hash": input_hash,
                "codex_cli_version": cli_version,
                "completed_cases": sorted(completed_cases),
                "source_fingerprints": fingerprints,
                "source_fingerprint_kind": "normalized-review-source-v1",
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "note": "Safe checkpoint only; no claims, excerpts, or model output are retained.",
            }
            progress_path.write_text(
                json.dumps(progress, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"golden case {case_name}: passed", flush=True)
        finally:
            for retrieved in retrieved_sources.values():
                retrieved.path.unlink(missing_ok=True)

    if completed_cases != set(case_sources):
        raise GoldenFailure("not all golden cases completed")
    summary = {
        "passed": True,
        "cases": 4,
        "sources": len(SOURCES),
        "assertions": 13,
        "note": "Only aggregate pass/fail metadata is retained; paper excerpts are not persisted.",
    }
    summary.update({
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_hash": input_hash,
        "extractor_model_requested": extractor_model,
        "verifier_model_requested": verifier_model,
        "codex_cli_version": cli_version,
    })
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    progress_path.unlink(missing_ok=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extractor-model", default=DEFAULT_MODEL)
    parser.add_argument("--verifier-model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    run_golden(
        output=args.output,
        extractor_model=args.extractor_model,
        verifier_model=args.verifier_model,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
