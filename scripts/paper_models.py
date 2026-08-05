"""Structured, model-facing types for the double-pass paper intake pipeline.

The language model produces claims, never registry identifiers or YAML.  Registry
records are created later by deterministic code after an independent verification
pass has accepted each claim.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


Confidence = Literal["high", "medium", "low"]

_ATOMIC_BENCHMARK_METADATA_STRING_PATHS = {
    "/benchmark-metadata/name",
    "/benchmark-metadata/summary",
    "/benchmark-metadata/kind",
    "/benchmark-metadata/release_date",
    "/benchmark-metadata/access/level",
    "/benchmark-metadata/access/tasks",
    "/benchmark-metadata/access/artifacts",
    "/benchmark-metadata/access/grader",
    "/benchmark-metadata/access/license",
    "/benchmark-metadata/access/biosafety_notes",
}
RelationType = Literal[
    "benchmark-creation",
    "evaluation",
    "training",
    "fine-tuning",
    "validation",
    "model-selection",
    "external-result-summary",
    "background-citation",
]
ClaimType = Literal[
    "paper-identity",
    "relation",
    "benchmark-identity",
    "benchmark-version",
    "benchmark-count",
    "benchmark-metadata",
    "scope-type",
    "scope-n",
    "subset-id",
    "selection",
    "selection-method",
    "model",
    "prompt",
    "shots",
    "reasoning",
    "tools",
    "internet",
    "code-execution",
    "container",
    "budget",
    "seed",
    "repeats",
    "grader",
    "human-review",
    "metric",
    "result",
    "creator-source",
    "official-repository",
    "official-resource",
    "scientific-task",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocatorDraft(StrictModel):
    locator_type: Literal["page", "section", "figure", "table", "repository-path", "other"]
    value: str = Field(min_length=1)
    document_page: int | None = Field(ge=1)
    printed_page: str | None
    excerpt: str = Field(min_length=1)

    @field_validator("excerpt", mode="before")
    @classmethod
    def excerpt_is_short(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        words = re.findall(r"\S+", value)
        return " ".join(words[:20])


class PaperIdentityDraft(StrictModel):
    title: str = Field(min_length=1)
    authors: list[str]
    organizations: list[str]
    publication_date: str | None
    doi: str | None
    arxiv: str | None
    canonical_url: str | None
    version_label: str | None


class BenchmarkMentionDraft(StrictModel):
    mention_id: str = Field(pattern=r"^mention-[0-9]+$")
    benchmark_name: str = Field(min_length=1)
    registry_benchmark_id: str | None
    relation_type: RelationType
    is_new_benchmark: bool
    background_only: bool
    claim_ids: list[str]
    reporting_gaps: list[str]


class EvidenceClaimDraft(StrictModel):
    claim_id: str = Field(pattern=r"^claim-[0-9]+$")
    mention_id: str | None
    claim_type: ClaimType
    field_path: str = Field(pattern=r"^/")
    value_json: str
    confidence: Confidence
    locators: list[LocatorDraft] = Field(min_length=1)

    @field_validator("value_json", mode="before")
    @classmethod
    def value_is_json(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                string_claims = {
                    "relation",
                    "benchmark-identity",
                    "benchmark-version",
                    "scope-type",
                    "subset-id",
                    "selection",
                    "selection-method",
                    "prompt",
                    "reasoning",
                }
                atomic_metadata_string = (
                    info.data.get("claim_type") == "benchmark-metadata"
                    and info.data.get("field_path") in _ATOMIC_BENCHMARK_METADATA_STRING_PATHS
                )
                if (
                    info.data.get("claim_type") in string_claims or atomic_metadata_string
                ) and value.strip():
                    parsed = value.strip()
                else:
                    raise ValueError("value_json must contain valid JSON") from None
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


class PaperEvidenceDraft(StrictModel):
    paper: PaperIdentityDraft
    benchmark_mentions: list[BenchmarkMentionDraft]
    claims: list[EvidenceClaimDraft]
    reporting_gaps: list[str]
    conflicts: list[str]

    @field_validator("claims")
    @classmethod
    def claim_ids_are_unique(cls, claims: list[EvidenceClaimDraft]) -> list[EvidenceClaimDraft]:
        ids = [claim.claim_id for claim in claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim IDs must be unique")
        return claims


class ClaimVerification(StrictModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "conflicted", "not-verifiable"]
    confidence: Confidence
    locator: LocatorDraft | None
    notes: str | None


class VerificationConflict(StrictModel):
    """Classify a verifier disagreement without conflating it with source conflict."""

    kind: Literal["extractor-error", "source-internal", "cross-source"]
    claim_ids: list[str]
    summary: str = Field(min_length=1)


class PaperEvidenceVerification(StrictModel):
    claims: list[ClaimVerification]
    source_parseable: bool
    conflicts: list[VerificationConflict]
    # Backward compatibility for receipts and fixtures produced before the
    # structured conflict taxonomy. New verifier passes always return [].
    blocking_conflicts: list[str]

    @model_validator(mode="before")
    @classmethod
    def add_empty_structured_conflicts_to_legacy_payload(
        cls,
        value: object,
    ) -> object:
        if isinstance(value, dict) and "conflicts" not in value:
            return {**value, "conflicts": []}
        return value

    @model_validator(mode="after")
    def conflicts_match_claim_verdicts(self) -> PaperEvidenceVerification:
        if self.conflicts and self.blocking_conflicts:
            raise ValueError(
                "structured conflicts and legacy blocking_conflicts cannot both be populated"
            )
        verdicts = {item.claim_id: item.verdict for item in self.claims}
        for conflict in self.conflicts:
            unknown = sorted(set(conflict.claim_ids) - set(verdicts))
            if unknown:
                raise ValueError(f"conflict references unknown claim IDs: {unknown}")
            expected = "unsupported" if conflict.kind == "extractor-error" else "conflicted"
            mismatched = [
                claim_id for claim_id in conflict.claim_ids
                if verdicts[claim_id] != expected
            ]
            if mismatched:
                raise ValueError(
                    f"{conflict.kind} conflicts require {expected} verdicts: {mismatched}"
                )
        return self


def effective_blocking_conflicts(
    verification: PaperEvidenceVerification,
) -> list[str]:
    """Return only conflicts that genuinely make source publication unsafe.

    Once structured conflicts are present they are authoritative. Extractor
    mistakes remain claim-level rejections, while contradictions within the
    source or between authoritative sources continue to block generation. Old
    payloads without structured conflicts retain their conservative behavior.
    """

    if verification.conflicts:
        return [
            (
                f"{' '.join(conflict.claim_ids)}: {conflict.summary}"
                if conflict.claim_ids else conflict.summary
            )
            for conflict in verification.conflicts
            if conflict.kind in {"source-internal", "cross-source"}
        ]
    return list(verification.blocking_conflicts)


def locator_is_resolved(locator: LocatorDraft | None) -> bool:
    if locator is None or not locator.value.strip():
        return False
    if locator.locator_type == "page" and locator.document_page is None and not locator.printed_page:
        return False
    return True


def accepted_claims(
    draft: PaperEvidenceDraft,
    verification: PaperEvidenceVerification,
) -> list[EvidenceClaimDraft]:
    """Return only claims independently supported at high confidence.

    Numeric results must declare a textual, tabular, or explicitly labeled figure
    source in their JSON payload. This makes graph-height digitization impossible.
    """

    verdicts = {item.claim_id: item for item in verification.claims}
    accepted: list[EvidenceClaimDraft] = []
    for claim in draft.claims:
        verdict = verdicts.get(claim.claim_id)
        if claim.confidence != "high" or verdict is None:
            continue
        if verdict.verdict != "supported" or verdict.confidence != "high":
            continue
        if not locator_is_resolved(verdict.locator):
            continue
        if claim.claim_type == "result":
            payload = json.loads(claim.value_json)
            if payload.get("numeric_source") not in {"body", "table", "labeled-figure"}:
                continue
        accepted.append(claim)
    return accepted
