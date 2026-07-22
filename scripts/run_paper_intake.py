#!/usr/bin/env python3
"""End-to-end, non-interactive paper intake used by the GitHub Actions bot."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from extract_paper import DEFAULT_MODEL, run_double_pass
from generate_paper_records import GenerationBlocked, build_records, chinese_summary, write_records
from paper_source import RetrievedSource, retrieve_source
from paper_models import accepted_claims
from registry_io import load_entities, load_taxonomies
from triage_paper import build_intake, normalize_url, parse_issue_form


def _is_checked(value: str) -> bool:
    return "[x]" in value.casefold() or "confirmed" in value.casefold()


def _arxiv_pdf_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname and parsed.hostname.lower() == "arxiv.org" and parsed.path.startswith("/abs/"):
        identifier = parsed.path.removeprefix("/abs/")
        return urlunsplit(("https", "arxiv.org", f"/pdf/{identifier}.pdf", "", ""))
    return url


def _first_url(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"https?://[^\s)>]+", value)
    return match.group(0).rstrip(".,") if match else None


def registry_context() -> dict[str, object]:
    entities = load_entities()
    taxonomies = load_taxonomies()
    works = {item["id"]: item for item in entities["work"]}
    creator_uses: dict[str, list[str]] = {}
    for use in entities["benchmark_use"]:
        if use["relation_type"] == "benchmark-creation":
            work = works[use["work_id"]]
            creator_uses.setdefault(use["benchmark_id"], []).extend(
                value for value in (work.get("doi"), work.get("arxiv"), work.get("canonical_url")) if value
            )
    return {
        "benchmarks": [{
            "id": benchmark["id"],
            "name": benchmark["name"],
            "aliases": benchmark["aliases"],
            "latest_version": benchmark["latest_version"],
            "known_versions": [item["label"] for item in benchmark.get("versions", [])],
            "creator_identifiers": sorted(set(creator_uses.get(benchmark["id"], []))),
        } for benchmark in entities["benchmark"]],
        "models": [{
            "id": model["id"], "name": model["name"], "provider": model["provider"],
            "version_string": model["version_string"], "aliases": model["aliases"],
        } for model in entities["model"]],
        "taxonomy_ids": {
            "domains": [item["id"] for item in taxonomies["domains"]],
            "capabilities": [item["id"] for item in taxonomies["capabilities"]],
            "modalities": [item["id"] for item in taxonomies["modalities"]],
            "access_levels": [item["id"] for item in taxonomies["access_levels"]],
            "scientific_tasks": [item["id"] for item in taxonomies["scientific_tasks"]],
        },
    }


def resolve_repository_pins(result: object) -> dict[str, dict[str, str]]:
    pins: dict[str, dict[str, str]] = {}
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "BioBench-Atlas/1.4"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for claim in accepted_claims(result.draft, result.verification):
        if claim.claim_type != "official-repository":
            continue
        payload = json.loads(claim.value_json)
        url = normalize_url(payload.get("url"))
        match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)", url or "")
        if not match:
            continue
        owner, repository = match.groups()
        repository = repository.removesuffix(".git")
        repo_response = requests.get(
            f"https://api.github.com/repos/{owner}/{repository}", headers=headers, timeout=30,
        )
        repo_response.raise_for_status()
        default_branch = repo_response.json()["default_branch"]
        commit_response = requests.get(
            f"https://api.github.com/repos/{owner}/{repository}/commits/{default_branch}",
            headers=headers, timeout=30,
        )
        commit_response.raise_for_status()
        commit = commit_response.json()["sha"]
        pins[url] = {
            "kind": "commit", "value": commit,
            "url": f"https://github.com/{owner}/{repository}/commit/{commit}",
        }
    return pins


def process_issue(
    body: str,
    *,
    discovered: bool,
    extractor_model: str,
    verifier_model: str,
    write: bool,
) -> tuple[object, RetrievedSource]:
    sections = parse_issue_form(body)
    paper_url = sections.get("Paper or preprint URL")
    if not paper_url:
        raise GenerationBlocked("issue has no Paper or preprint URL")
    rights_value = sections.get("Source-use confirmation", "")
    rights_confirmed = _is_checked(rights_value)
    supplied_source = (
        sections.get("Open PDF or full-text URL / attachment (optional)")
        or sections.get("Open PDF or full-text URL (optional)")
    )
    source_url = _first_url(supplied_source) or _arxiv_pdf_url(paper_url)
    triage = build_intake(
        url=paper_url,
        doi=sections.get("DOI (optional)"),
        arxiv=sections.get("arXiv or preprint ID (optional)"),
        title=sections.get("Title (optional)"),
        benchmark_hints=sections.get("Possible benchmarks", ""),
        focus_locators=sections.get("Relevant tables, figures, or sections", ""),
        may_contain_new_benchmark=sections.get("Could this introduce a new benchmark?", ""),
        resolve=True,
    )
    if triage["duplicate_work_candidates"]:
        # Existing Work is allowed: the paper may have no BenchmarkUse yet. The
        # generator resolves the duplicate deterministically and adds only missing uses.
        pass
    source = retrieve_source(source_url, rights_confirmed=rights_confirmed, discovered=discovered)
    try:
        result = run_double_pass(
            source.path,
            registry_context=registry_context(),
            extractor_model=extractor_model,
            verifier_model=verifier_model,
        )
        records = build_records(
            result.as_dict(),
            source={
                "url": source.url,
                "source_access": source.source_access,
                "content_sha256": source.content_sha256,
                "content_type": source.content_type,
                "retrieved_at": source.retrieved_at,
                "repository_pins": resolve_repository_pins(result),
            },
            generated_at=source.retrieved_at,
            verified_on=source.retrieved_at[:10],
        )
        if records.blocked_reasons:
            raise GenerationBlocked("; ".join(records.blocked_reasons))
        if write:
            write_records(records)
        return records, source
    finally:
        source.path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-body-file", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--discovered", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--extractor-model", default=os.environ.get("PAPER_EXTRACT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--verifier-model", default=os.environ.get("PAPER_VERIFY_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()
    records, source = process_issue(
        args.issue_body_file.read_text(encoding="utf-8"),
        discovered=args.discovered,
        extractor_model=args.extractor_model,
        verifier_model=args.verifier_model,
        write=args.write,
    )
    summary = chinese_summary(records)
    summary += f"\nSource SHA256: `{source.content_sha256}`  \n"
    summary += f"Extractor: `{args.extractor_model}` · Verifier: `{args.verifier_model}`\n"
    args.summary.write_text(summary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
