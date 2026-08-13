#!/usr/bin/env python3
"""Generate paper records from an issue body with two local Codex evidence passes."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from extract_paper import DEFAULT_MODEL, run_double_pass
from generate_paper_records import GenerationBlocked, build_records, chinese_summary, write_records
from paper_source import RetrievedSource, retrieve_source
from paper_models import accepted_claims
from registry_io import load_entities, load_taxonomies
from triage_paper import build_intake, normalize_url, parse_issue_form


GITHUB_API_ATTEMPTS = 3
MAX_OFFICIAL_ARTIFACTS = 4
MAX_REPOSITORY_TREE_PATHS = 500


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


def _urls(value: str | None) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for match in re.finditer(r"https?://[^\s)>]+", value):
        url = match.group(0).rstrip(".,")
        if url not in result:
            result.append(url)
    return result


def _focus_pdf_pages(value: str | None) -> list[int] | None:
    """Expand explicit owner-supplied physical page locators, never free-form numbers."""

    if not value:
        return None
    pages: set[int] = set()
    for match in re.finditer(
        r"\bpages?\s+(\d+)(?:\s*[-\N{EN DASH}\N{EM DASH}]\s*(\d+))?",
        value,
        flags=re.IGNORECASE,
    ):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            start, end = end, start
        if end - start + 1 > 40:
            raise GenerationBlocked("a PDF focus range may contain at most 40 pages")
        pages.update(range(start, end + 1))
    if len(pages) > 40:
        raise GenerationBlocked("combined PDF focus ranges may contain at most 40 pages")
    return sorted(pages) or None


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


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float = 30,
) -> dict[str, object]:
    last_error: requests.RequestException | None = None
    for attempt in range(GITHUB_API_ATTEMPTS):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            retryable = (
                response is None
                or status == 429
                or (isinstance(status, int) and status >= 500)
            )
            if not retryable:
                raise
            last_error = error
            if attempt + 1 < GITHUB_API_ATTEMPTS:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _github_json_request(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float = 30,
) -> dict[str, object]:
    """Backward-compatible wrapper retained for the existing retry contract."""

    return _json_request(url, headers=headers, timeout=timeout)


def official_artifact_context(
    value: str | None,
    *,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    """Resolve bounded public GitHub evidence named by an owner-selected Issue.

    The returned packet is temporary input to both independent Codex passes. It
    can establish only the identity, immutable pin, public visibility, and file
    inventory of an official resource. Paper claims such as counts, protocols,
    and results must still come from the target source itself.
    """

    urls = _urls(value)
    if len(urls) > MAX_OFFICIAL_ARTIFACTS:
        raise GenerationBlocked(
            f"Official artifact lists more than {MAX_OFFICIAL_ARTIFACTS} URLs"
        )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BioBench-Atlas/1.4",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    packet: list[dict[str, Any]] = []
    for raw_url in urls:
        url = normalize_url(raw_url)
        match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)", url or "")
        if not match:
            raise GenerationBlocked(
                "Official artifact currently accepts only public GitHub repository URLs"
            )
        owner, repository = match.groups()
        repository = repository.removesuffix(".git")
        repository_payload = _github_json_request(
            f"https://api.github.com/repos/{owner}/{repository}",
            headers=headers,
            timeout=timeout,
        )
        default_branch = str(repository_payload["default_branch"])
        commit_payload = _github_json_request(
            f"https://api.github.com/repos/{owner}/{repository}/commits/{default_branch}",
            headers=headers,
            timeout=timeout,
        )
        commit = str(commit_payload["sha"])
        tree_payload = _github_json_request(
            f"https://api.github.com/repos/{owner}/{repository}/git/trees/{commit}?recursive=1",
            headers=headers,
            timeout=timeout,
        )
        owner_payload = _github_json_request(
            f"https://api.github.com/users/{owner}",
            headers=headers,
            timeout=timeout,
        )
        paths = sorted(
            str(item["path"])
            for item in tree_payload.get("tree", [])
            if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
        )
        if len(paths) > MAX_REPOSITORY_TREE_PATHS:
            raise GenerationBlocked(
                f"Official artifact repository exceeds {MAX_REPOSITORY_TREE_PATHS} files"
            )
        license_payload = repository_payload.get("license") or {}
        packet.append({
            "evidence_scope": "official-resource-identity-pin-and-public-files-only",
            "resource_type": "repository",
            "url": f"https://github.com/{owner}/{repository}",
            "api_url": f"https://api.github.com/repos/{owner}/{repository}",
            "full_name": repository_payload.get("full_name"),
            "description": repository_payload.get("description"),
            "owner_login": owner_payload.get("login"),
            "owner_display_name": owner_payload.get("name"),
            "owner_profile_url": owner_payload.get("html_url"),
            "default_branch": default_branch,
            "head_commit": commit,
            "head_commit_url": f"https://github.com/{owner}/{repository}/commit/{commit}",
            "visibility": repository_payload.get("visibility"),
            "private": repository_payload.get("private"),
            "archived": repository_payload.get("archived"),
            "license": license_payload.get("spdx_id"),
            "file_paths": paths,
        })
    return packet


def _zenodo_record_id(url: str) -> str | None:
    for pattern in (
        r"^https://zenodo\.org/records/(\d+)$",
        r"^https://doi\.org/10\.5281/zenodo\.(\d+)$",
    ):
        match = re.fullmatch(pattern, url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def resolve_resource_pins(result: object) -> dict[str, dict[str, str]]:
    pins: dict[str, dict[str, str]] = {}
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "BioBench-Atlas/1.4"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for claim in accepted_claims(result.draft, result.verification):
        if claim.claim_type not in {"official-repository", "official-resource"}:
            continue
        payload = json.loads(claim.value_json)
        url = normalize_url(payload.get("url"))
        if not url:
            continue
        match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)", url or "")
        if match:
            owner, repository = match.groups()
            repository = repository.removesuffix(".git")
            repository_payload = _json_request(
                f"https://api.github.com/repos/{owner}/{repository}", headers=headers, timeout=30,
            )
            default_branch = str(repository_payload["default_branch"])
            commit_payload = _json_request(
                f"https://api.github.com/repos/{owner}/{repository}/commits/{default_branch}",
                headers=headers, timeout=30,
            )
            commit = str(commit_payload["sha"])
            pins[url] = {
                "resource_type": "repository",
                "kind": "commit", "value": commit,
                "url": f"https://github.com/{owner}/{repository}/commit/{commit}",
                "resolved_url": f"https://github.com/{owner}/{repository}",
                "license": payload.get("license"),
            }
            continue
        zenodo_id = _zenodo_record_id(url)
        if zenodo_id is None or payload.get("resource_type") != "dataset":
            continue
        record = _json_request(
            f"https://zenodo.org/api/records/{zenodo_id}",
            headers={"Accept": "application/json", "User-Agent": "BioBench-Atlas/1.4"},
            timeout=30,
        )
        metadata = record.get("metadata") or {}
        links = record.get("links") or {}
        resolved_id = str(record.get("id") or "")
        version = str(metadata.get("version") or record.get("doi") or "").strip()
        resolved_url = str(links.get("self_html") or f"https://zenodo.org/records/{resolved_id}")
        license_payload = metadata.get("license") or {}
        license_id = license_payload.get("id") if isinstance(license_payload, dict) else None
        if not resolved_id or not version:
            continue
        pins[url] = {
            "resource_type": "dataset",
            "kind": "version", "value": version,
            "url": resolved_url,
            "resolved_url": resolved_url,
            "license": payload.get("license") or license_id,
        }
    return pins


def resolve_repository_pins(result: object) -> dict[str, dict[str, str]]:
    """Backward-compatible alias for callers predating dataset-backed intake."""

    return resolve_resource_pins(result)


def process_issue(
    body: str,
    *,
    discovered: bool,
    extractor_model: str,
    verifier_model: str,
    write: bool,
    local_run_id: str | None = None,
    owner_conflict_resolution: dict[str, object] | None = None,
) -> tuple[object, RetrievedSource, object]:
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
    focus_locators = sections.get("Relevant tables, figures, or sections", "")
    preferred_pdf_pages = _focus_pdf_pages(focus_locators)
    artifact_context = official_artifact_context(sections.get("Official artifact"))
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
    source = retrieve_source(
        source_url,
        rights_confirmed=rights_confirmed,
        discovered=discovered,
        preferred_pdf_pages=preferred_pdf_pages,
    )
    try:
        benchmark_hints = sections.get("Possible benchmarks", "")
        review_focus = {
            key: value[:6000]
            for key, value in {
                "benchmark_hints": benchmark_hints,
                "focus_locators": focus_locators,
            }.items()
            if value and value.strip() and value.strip() != "_No response_"
        }
        result = run_double_pass(
            source.path,
            registry_context=registry_context(),
            extractor_model=extractor_model,
            verifier_model=verifier_model,
            local_run_id=local_run_id,
            review_focus=review_focus or None,
            preferred_pdf_pages=preferred_pdf_pages,
            official_artifact_context=artifact_context or None,
        )
        records = build_records(
            result.as_dict(),
            source={
                "url": source.url,
                "source_access": source.source_access,
                "content_sha256": source.content_sha256,
                "content_type": source.content_type,
                "retrieved_at": source.retrieved_at,
                "bibliographic_metadata": triage["bibliographic_metadata"],
                "resource_pins": resolve_resource_pins(result),
            },
            generated_at=source.retrieved_at,
            verified_on=source.retrieved_at[:10],
            owner_conflict_resolution=owner_conflict_resolution,
        )
        if records.blocked_reasons:
            raise GenerationBlocked("; ".join(records.blocked_reasons))
        if write:
            write_records(records)
        return records, source, result
    finally:
        source.path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-body-file", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--discovered", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--extractor-model", default=DEFAULT_MODEL)
    parser.add_argument("--verifier-model", default=DEFAULT_MODEL)
    parser.add_argument("--local-run-id")
    args = parser.parse_args()
    records, source, result = process_issue(
        args.issue_body_file.read_text(encoding="utf-8"),
        discovered=args.discovered,
        extractor_model=args.extractor_model,
        verifier_model=args.verifier_model,
        write=args.write,
        local_run_id=args.local_run_id,
    )
    summary = chinese_summary(records)
    summary += f"\nSource SHA256: `{source.content_sha256}`  \n"
    summary += f"Extractor: `{args.extractor_model}` · Verifier: `{args.verifier_model}`  \n"
    summary += f"Local run: `{result.local_run_id}`\n"
    args.summary.write_text(summary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
