#!/usr/bin/env python3
"""Normalize a paper submission and scaffold a human-audited registry intake.

This tool deliberately does not infer evaluation settings or results. It resolves
bibliographic identity, detects existing Works and benchmark-name hints, and writes
an intake file that can safely start a draft pull request.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
import yaml

from registry_io import load_entities


ARXIV_RE = re.compile(r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(v\d+)?")
DOI_RE = re.compile(r"(?i)10\.\d{4,9}/[-._;()/:A-Z0-9]+")


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    decoded = urllib.parse.unquote(value.strip())
    decoded = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", decoded)
    decoded = re.sub(r"(?i)^doi\s*:\s*", "", decoded).strip()
    match = DOI_RE.search(decoded)
    return match.group(0).rstrip(".,;)").lower() if match else None


def normalize_arxiv(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = ARXIV_RE.search(urllib.parse.unquote(value.strip()))
    if not match:
        return None, None
    return match.group(1), f"{match.group(1)}{match.group(2) or ''}"


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


def title_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKD", value).casefold()
    fingerprint = "".join(character for character in normalized if character.isalnum())
    return fingerprint or None


def parse_issue_form(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^###\s+(.+?)\s*$", body))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[start:end].strip()
        if value not in {"", "_No response_"}:
            sections[match.group(1).strip()] = value
    return sections


def _date_parts(message: dict[str, Any]) -> str | None:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = message.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            values = list(parts[0]) + [1, 1]
            return f"{values[0]:04d}-{values[1]:02d}-{values[2]:02d}"
    return None


def resolve_crossref(doi: str, timeout: float = 20) -> dict[str, Any]:
    response = requests.get(
        f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}",
        headers={"User-Agent": "BioBench-Atlas/1.3 (https://github.com/SpectrAI-Initiative/bio-benchmark-atlas)"},
        timeout=timeout,
    )
    response.raise_for_status()
    message = response.json()["message"]
    authors = []
    for author in message.get("author", []):
        name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
        if name:
            authors.append(name)
    return {
        "title": (message.get("title") or [None])[0],
        "authors": authors,
        "publication_date": _date_parts(message),
        "doi": normalize_doi(message.get("DOI")) or doi,
        "canonical_url": normalize_url(message.get("URL")),
        "publisher": message.get("publisher"),
        "source": "Crossref",
    }


def resolve_arxiv(arxiv_id: str, timeout: float = 20) -> dict[str, Any]:
    response = requests.get(
        "https://export.arxiv.org/api/query",
        params={"id_list": arxiv_id},
        headers={"User-Agent": "BioBench-Atlas/1.3"},
        timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv returned no entry for {arxiv_id}")
    title = " ".join((entry.findtext("atom:title", default="", namespaces=ns)).split())
    authors = [
        " ".join(author.findtext("atom:name", default="", namespaces=ns).split())
        for author in entry.findall("atom:author", ns)
    ]
    published = entry.findtext("atom:published", default="", namespaces=ns)
    return {
        "title": title or None,
        "authors": [author for author in authors if author],
        "publication_date": published[:10] or None,
        "arxiv": arxiv_id,
        "canonical_url": f"https://arxiv.org/abs/{arxiv_id}",
        "source": "arXiv API",
    }


def _normalized_work_identity(work: dict[str, Any]) -> dict[str, str | None]:
    return {
        "doi": normalize_doi(work.get("doi")),
        "arxiv": normalize_arxiv(work.get("arxiv"))[0],
        "canonical_url": normalize_url(work.get("canonical_url")),
        "title_fingerprint": title_fingerprint(work.get("title")),
    }


def duplicate_work_candidates(identity: dict[str, Any], works: list[dict[str, Any]]) -> list[dict[str, str]]:
    priorities = ("doi", "arxiv", "canonical_url", "title_fingerprint")
    matches: list[dict[str, str]] = []
    for work in works:
        existing = _normalized_work_identity(work)
        matched_by = next(
            (key for key in priorities if identity.get(key) and identity.get(key) == existing.get(key)),
            None,
        )
        if matched_by:
            matches.append({"work_id": work["id"], "matched_by": matched_by})
    return matches


def benchmark_candidates(text: str, benchmarks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    haystack = unicodedata.normalize("NFKC", text).casefold()
    matches = []
    for benchmark in benchmarks:
        labels = [benchmark["id"], benchmark["name"], *benchmark.get("aliases", [])]
        found = []
        for label in labels:
            normalized = unicodedata.normalize("NFKC", str(label)).casefold().strip()
            if len(normalized) >= 4 and re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", haystack):
                found.append(label)
        if found:
            matches.append({"benchmark_id": benchmark["id"], "matched_labels": sorted(set(found))})
    return matches


def build_intake(
    *,
    url: str,
    doi: str | None = None,
    arxiv: str | None = None,
    title: str | None = None,
    benchmark_hints: str = "",
    focus_locators: str = "",
    may_contain_new_benchmark: str = "",
    resolve: bool = False,
) -> dict[str, Any]:
    canonical_url = normalize_url(url)
    normalized_doi = normalize_doi(doi) or normalize_doi(url)
    arxiv_base, arxiv_version = normalize_arxiv(arxiv or url)
    metadata: dict[str, Any] = {}
    resolution_errors = []
    if resolve:
        try:
            if normalized_doi:
                metadata = resolve_crossref(normalized_doi)
            elif arxiv_base:
                metadata = resolve_arxiv(arxiv_base)
        except (requests.RequestException, ValueError, KeyError, ET.ParseError) as error:
            resolution_errors.append(str(error))
    resolved_title = metadata.get("title") or title
    identity = {
        "doi": metadata.get("doi") or normalized_doi,
        "arxiv": metadata.get("arxiv") or arxiv_base,
        "arxiv_version": arxiv_version,
        "canonical_url": metadata.get("canonical_url") or canonical_url,
        "title": resolved_title,
        "title_fingerprint": title_fingerprint(resolved_title),
    }
    entities = load_entities()
    candidates_text = "\n".join(part for part in (benchmark_hints, focus_locators, resolved_title or "") if part)
    return {
        "entity_type": "paper_intake",
        "status": "needs-human-review",
        "normalized_identity": identity,
        "bibliographic_metadata": {
            "authors": metadata.get("authors", []),
            "publication_date": metadata.get("publication_date"),
            "publisher": metadata.get("publisher"),
            "metadata_source": metadata.get("source"),
            "resolution_errors": resolution_errors,
        },
        "duplicate_work_candidates": duplicate_work_candidates(identity, entities["work"]),
        "benchmark_candidates": benchmark_candidates(candidates_text, entities["benchmark"]),
        "submitter_notes": {
            "benchmark_hints": benchmark_hints or None,
            "focus_locators": focus_locators or None,
            "may_contain_new_benchmark": may_contain_new_benchmark or None,
        },
        "required_review": [
            "Confirm the versioned primary paper identity and duplicate status.",
            "Separate actual evaluation, training, fine-tuning, validation, model selection, external result summary, and background citations.",
            "For each evaluation, extract benchmark version, scope, realized n, selection, exact system, protocol, grader, metrics, results, and source locators.",
            "If a benchmark is new, verify its creator paper and official repository or dataset before adding production entities.",
            "Represent source omissions as partial BenchmarkUse or null plus not_reported; never estimate unlabeled figure values.",
        ],
        "production_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--doi")
    parser.add_argument("--arxiv")
    parser.add_argument("--title")
    parser.add_argument("--benchmark-hints", default="")
    parser.add_argument("--focus-locators", default="")
    parser.add_argument("--may-contain-new-benchmark", default="")
    parser.add_argument("--issue-body")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.issue_body:
        sections = parse_issue_form(args.issue_body)
        args.url = args.url or sections.get("Paper or preprint URL")
        args.doi = args.doi or sections.get("DOI (optional)")
        args.arxiv = args.arxiv or sections.get("arXiv or preprint ID (optional)")
        args.title = args.title or sections.get("Title (optional)")
        args.benchmark_hints = args.benchmark_hints or sections.get("Possible benchmarks", "")
        args.focus_locators = args.focus_locators or sections.get("Relevant tables, figures, or sections", "")
        args.may_contain_new_benchmark = args.may_contain_new_benchmark or sections.get("Could this introduce a new benchmark?", "")
    if not args.url:
        parser.error("--url or an issue body containing 'Paper or preprint URL' is required")

    intake = build_intake(
        url=args.url,
        doi=args.doi,
        arxiv=args.arxiv,
        title=args.title,
        benchmark_hints=args.benchmark_hints,
        focus_locators=args.focus_locators,
        may_contain_new_benchmark=args.may_contain_new_benchmark,
        resolve=args.resolve,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(yaml.safe_dump(intake, sort_keys=False, allow_unicode=True), encoding="utf-8")
    else:
        print(json.dumps(intake, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
