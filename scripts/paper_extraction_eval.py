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
    VERIFIER_PROMPT,
    codex_version,
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


def evaluate_results(results: dict[str, Any]) -> dict[str, Any]:
    life = _claim_payloads(results["lifescibench"], "lifescibench")
    for expected, label in ((750, "total"), (136, "protein"), (62, "design")):
        if not _count(life, expected, label):
            raise GoldenFailure(f"LifeSciBench missing verified {label} count {expected}")
    for kind, payload in life:
        if kind == "benchmark-count" and isinstance(payload, dict):
            if "binding" in str(payload.get("label", "")).casefold() and payload.get("count") is not None:
                raise GoldenFailure("LifeSciBench invented a binding count")

    mystery = _claim_payloads(results["biomysterybench"], "biomysterybench")
    for expected, label in ((99, "total"), (76, "human-solvable"), (23, "human-difficult")):
        if not _count(mystery, expected, label):
            raise GoldenFailure(f"BioMysteryBench missing verified {label} count {expected}")
    if not any(kind == "repeats" and payload == 5 for kind, payload in mystery):
        raise GoldenFailure("BioMysteryBench missing five verified repeats")

    spatial_paper = _claim_payloads(results["spatialbench-paper-v2"], "spatialbench")
    spatial_repo = _claim_payloads(results["spatialbench-repository"], "spatialbench")
    if not any(kind == "benchmark-count" and isinstance(payload, dict) and payload.get("count") == 146 for kind, payload in spatial_paper):
        raise GoldenFailure("SpatialBench paper-v2 count 146 is missing")
    if not any(kind == "benchmark-count" and isinstance(payload, dict) and payload.get("count") == 159 for kind, payload in spatial_repo):
        raise GoldenFailure("SpatialBench repository snapshot count 159 is missing")
    paper_versions = {str(payload) for kind, payload in spatial_paper if kind == "benchmark-version"}
    repo_versions = {str(payload) for kind, payload in spatial_repo if kind == "benchmark-version"}
    if paper_versions & repo_versions:
        raise GoldenFailure("SpatialBench 146 and 159 snapshots were assigned the same version")

    bix = _claim_payloads(results["anthropic-bixbench"], "bixbench")
    if any(kind == "result" for kind, _ in bix):
        raise GoldenFailure("Anthropic × BixBench produced a numeric result claim")
    if not any(kind == "relation" and payload == "evaluation" for kind, payload in bix):
        raise GoldenFailure("Anthropic × BixBench evaluation relation is missing")
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
        "extractor_prompt": EXTRACTOR_PROMPT,
        "verifier_prompt": VERIFIER_PROMPT,
        "extractor_schema": PaperEvidenceDraft.model_json_schema(),
        "verifier_schema": PaperEvidenceVerification.model_json_schema(),
        "extractor_model": extractor_model,
        "verifier_model": verifier_model,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_golden(
    *,
    output: Path,
    extractor_model: str = DEFAULT_MODEL,
    verifier_model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    results = {}
    for source in SOURCES:
        retrieved = retrieve_source(source.url, rights_confirmed=True)
        try:
            results[source.name] = run_double_pass(
                retrieved.path,
                registry_context=registry_context(),
                extractor_model=extractor_model,
                verifier_model=verifier_model,
            )
        finally:
            retrieved.path.unlink(missing_ok=True)
    summary = evaluate_results(results)
    summary.update({
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_hash": golden_input_hash(extractor_model, verifier_model),
        "extractor_model_requested": extractor_model,
        "verifier_model_requested": verifier_model,
        "codex_cli_version": codex_version(),
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
