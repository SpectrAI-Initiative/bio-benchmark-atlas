#!/usr/bin/env python3
"""Discover high-precision bio/chem benchmark-paper candidates from public APIs."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

from registry_io import load_entities
from triage_paper import duplicate_work_candidates, normalize_arxiv, normalize_doi, normalize_url, parse_issue_form, title_fingerprint


EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF_URL = "https://api.crossref.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
MAX_CANDIDATES = 10
AREA_QUOTAS = {"protein": 4, "dna-rna": 2, "small-molecule": 2, "omics-cell": 2}
BIO_TERMS = re.compile(
    r"(?i)\b(protein|peptide|antibody|dna|rna|gene|genom|transcript|cell|omics|molecul|chem|drug|reaction|retrosynth|bioinformatic)\w*\b"
)
EVAL_TERMS = re.compile(r"(?i)\b(benchmark|evaluation|evaluate|evaluating|challenge|leaderboard)\w*\b")
NEW_BENCHMARK_CLAIM = re.compile(
    r"(?is)\b(introduc|present|propos|releas|construct|develop|creat)\w*\b.{0,100}"
    r"\b(benchmark|evaluation suite|challenge dataset)\w*\b|"
    r"\b(benchmark|evaluation suite|challenge dataset)\w*\b.{0,100}"
    r"\b(introduc|present|propos|releas|construct|develop|creat)\w*\b"
)
REPOSITORY_RE = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+", re.I)


@dataclass
class Candidate:
    source_api: str
    source_id: str
    title: str
    abstract: str
    publication_date: str | None
    doi: str | None
    arxiv: str | None
    canonical_url: str
    pdf_url: str | None
    authors: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    repository_urls: list[str] = field(default_factory=list)
    peer_reviewed: bool = False
    open_fulltext: bool = False
    matched_benchmark_ids: list[str] = field(default_factory=list)
    match_reasons: list[str] = field(default_factory=list)
    area: str | None = None
    score: int = 0

    @property
    def candidate_id(self) -> str:
        identity = self.doi or self.arxiv or self.canonical_url or title_fingerprint(self.title) or self.title
        return hashlib.sha256(f"{self.source_api}:{identity}".encode()).hexdigest()[:16]


def _request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    for attempt in range(3):
        response = session.request(method, url, timeout=40, **kwargs)
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
            return response
        if attempt == 2:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt)
    raise AssertionError("unreachable")


def _strip_markup(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def fetch_europe_pmc(session: requests.Session, *, start_year: int = 2024, end_year: int = 2026, max_pages: int = 2) -> list[Candidate]:
    query = (
        f"FIRST_PDATE:[{start_year}-01-01 TO {end_year}-12-31] AND "
        "(benchmark OR evaluation) AND (protein OR DNA OR RNA OR genomics OR transcriptomics OR "
        "single-cell OR spatial OR molecule OR chemistry OR bioinformatics)"
    )
    results = []
    cursor = "*"
    for _ in range(max_pages):
        response = _request(session, "GET", EUROPE_PMC_URL, params={
            "query": query, "format": "json", "pageSize": 100, "resultType": "core", "cursorMark": cursor,
        })
        payload = response.json()
        results.extend(payload.get("resultList", {}).get("result", []))
        next_cursor = payload.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    candidates = []
    for item in results:
        raw_publication_types = item.get("pubTypeList") or {}
        if isinstance(raw_publication_types, dict):
            raw_publication_types = raw_publication_types.get("pubType") or []
        publication_types = {
            str(value).casefold() for value in raw_publication_types
        }
        title_value = _strip_markup(item.get("title"))
        if publication_types & {"review", "systematic review", "editorial", "letter"}:
            continue
        if re.match(r"(?i)^(review|editorial|decision letter|author response)\b", title_value):
            continue
        doi = normalize_doi(item.get("doi"))
        pmcid = item.get("pmcid")
        canonical = normalize_url(
            f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id')}"
        )
        if not canonical:
            continue
        pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None
        candidates.append(Candidate(
            source_api="europe-pmc",
            source_id=str(item.get("id") or doi or canonical),
            title=title_value,
            abstract=_strip_markup(item.get("abstractText")),
            publication_date=item.get("firstPublicationDate") or item.get("firstIndexDate"),
            doi=doi,
            arxiv=None,
            canonical_url=canonical,
            pdf_url=pdf_url,
            authors=[part.strip() for part in (item.get("authorString") or "").split(",") if part.strip()],
            peer_reviewed=str(item.get("source", "")).upper() not in {"PPR", "CTX"},
            open_fulltext=bool(item.get("isOpenAccess") == "Y" and pmcid),
        ))
    return candidates


def _crossref_date(item: dict[str, Any]) -> str | None:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            values = list(parts[0]) + [1, 1]
            return f"{values[0]:04d}-{values[1]:02d}-{values[2]:02d}"
    return None


def fetch_crossref(
    session: requests.Session,
    *,
    start_year: int = 2024,
    end_year: int = 2026,
    max_pages: int = 2,
    query_terms: str = "benchmark evaluation protein DNA RNA genomics chemistry single-cell",
) -> list[Candidate]:
    items = []
    cursor = "*"
    for _ in range(max_pages):
        response = _request(session, "GET", CROSSREF_URL, params={
            "query.bibliographic": query_terms,
            "filter": f"from-pub-date:{start_year}-01-01,until-pub-date:{end_year}-12-31",
            "rows": 100,
            "cursor": cursor,
            "select": "DOI,title,abstract,author,published,published-print,published-online,issued,URL,reference,link,type",
            "mailto": "wang422003@users.noreply.github.com",
        })
        message = response.json().get("message", {})
        items.extend(message.get("items", []))
        next_cursor = message.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    candidates = []
    for item in items:
        title_value = _strip_markup((item.get("title") or [""])[0])
        if item.get("type") not in {"journal-article", "proceedings-article", "posted-content", "report"}:
            continue
        if re.match(r"(?i)^(review|editorial|decision letter|author response)\b", title_value):
            continue
        doi = normalize_doi(item.get("DOI"))
        canonical = normalize_url(item.get("URL") or (f"https://doi.org/{doi}" if doi else None))
        if not canonical:
            continue
        links = item.get("link") or []
        pdf = next((link.get("URL") for link in links if link.get("content-type") == "application/pdf"), None)
        authors = [
            " ".join(value for value in (author.get("given"), author.get("family")) if value)
            for author in item.get("author", [])
        ]
        candidates.append(Candidate(
            source_api="crossref",
            source_id=doi or canonical,
            title=title_value,
            abstract=_strip_markup(item.get("abstract")),
            publication_date=_crossref_date(item),
            doi=doi,
            arxiv=None,
            canonical_url=canonical,
            pdf_url=normalize_url(pdf),
            authors=[author for author in authors if author],
            references=[normalize_doi(ref.get("DOI")) for ref in item.get("reference", []) if normalize_doi(ref.get("DOI"))],
            peer_reviewed=item.get("type") not in {"posted-content", "preprint"},
            open_fulltext=bool(pdf),
        ))
    return candidates


def fetch_arxiv(session: requests.Session, *, start_year: int = 2024, end_year: int = 2026, max_pages: int = 2) -> list[Candidate]:
    start = f"{start_year}01010000"
    end = f"{end_year}12312359"
    query = f"submittedDate:[{start} TO {end}] AND (cat:q-bio.* OR cat:cs.AI OR cat:cs.LG)"
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    candidates = []
    entries = []
    for page in range(max_pages):
        response = _request(session, "GET", ARXIV_URL, params={
            "search_query": query, "start": page * 100, "max_results": 100, "sortBy": "submittedDate", "sortOrder": "descending",
        }, headers={"User-Agent": "BioBench-Atlas/1.4"})
        root = ET.fromstring(response.text)
        page_entries = root.findall("atom:entry", ns)
        entries.extend(page_entries)
        if len(page_entries) < 100:
            break
    for entry in entries:
        canonical = normalize_url(entry.findtext("atom:id", default="", namespaces=ns))
        arxiv, arxiv_version = normalize_arxiv(canonical)
        if not canonical or not arxiv:
            continue
        title = _strip_markup(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = _strip_markup(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [
            _strip_markup(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        published = entry.findtext("atom:published", default="", namespaces=ns)[:10] or None
        candidates.append(Candidate(
            source_api="arxiv",
            source_id=arxiv_version or arxiv,
            title=title,
            abstract=abstract,
            publication_date=published,
            doi=None,
            arxiv=arxiv,
            canonical_url=canonical,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_version or arxiv}.pdf",
            authors=[author for author in authors if author],
            peer_reviewed=False,
            open_fulltext=True,
        ))
    return candidates


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _contains_label(text: str, label: str) -> bool:
    normalized = _normalized_text(label).strip()
    return len(normalized) >= 4 and bool(re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text))


def _alias_is_distinctive(label: str) -> bool:
    normalized = _normalized_text(label).strip()
    return (
        len(normalized) >= 8
        or "bench" in normalized
        or normalized.endswith("qa")
        or normalized in {"casp", "cameo", "flip", "tape", "blade", "scigym", "atom3d", "guacamol"}
    )


def classify_area(text: str) -> str | None:
    lowered = _normalized_text(text)
    if re.search(r"\b(single-cell|single cell|cell-state|cell state|cell-type|cell type|spatial omics|spatial transcriptomics|trajectory)\b", lowered):
        return "omics-cell"
    if re.search(r"\b(crispr|guide-rna|guide rna|dna|rna|genom|variant|transcript)\w*", lowered) and not re.search(r"\bprotein\b", lowered):
        return "dna-rna"
    if re.search(r"\b(protein|peptide|antibody|folding|docking)\w*", lowered):
        return "protein"
    if re.search(r"\b(small molecule|chemical|reaction|retrosynth|admet|toxicity)\w*", lowered):
        return "small-molecule"
    scores = {
        "protein": len(re.findall(r"\b(protein|peptide|antibody|binding|folding)\w*", lowered)),
        "dna-rna": len(re.findall(r"\b(dna|rna|genom|variant|crispr|transcript)\w*", lowered)),
        "small-molecule": len(re.findall(r"\b(small molecule|chemical|reaction|retrosynth|admet|toxicity)\w*", lowered)),
        "omics-cell": len(re.findall(r"\b(omics|single-cell|spatial|cell type|trajectory|biomarker|regulatory network)\w*", lowered)),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def score_candidate(candidate: Candidate, entities: dict[str, list[dict[str, Any]]]) -> Candidate | None:
    text = _normalized_text(f"{candidate.title}\n{candidate.abstract}")
    candidate.repository_urls = sorted(set(REPOSITORY_RE.findall(text)))
    creator_dois: dict[str, str] = {}
    works = {work["id"]: work for work in entities["work"]}
    for use in entities["benchmark_use"]:
        if use["relation_type"] == "benchmark-creation":
            doi = normalize_doi(works[use["work_id"]].get("doi"))
            if doi:
                creator_dois[doi] = use["benchmark_id"]
    official_repos: dict[str, str] = {}
    for benchmark in entities["benchmark"]:
        for resource in benchmark["resources"]:
            if resource["type"] == "repository":
                official_repos[normalize_url(resource["url"]) or resource["url"]] = benchmark["id"]

    matched = set()
    reasons = []
    for benchmark in entities["benchmark"]:
        labels = [benchmark["id"]]
        if _alias_is_distinctive(benchmark["name"]):
            labels.append(benchmark["name"])
        labels.extend(alias for alias in benchmark["aliases"] if _alias_is_distinctive(str(alias)))
        hit = next((label for label in labels if _contains_label(text, str(label))), None)
        if hit:
            matched.add(benchmark["id"]); reasons.append(f"exact benchmark label: {hit}")
    for reference in candidate.references:
        if reference in creator_dois:
            matched.add(creator_dois[reference]); reasons.append(f"creator DOI citation: {reference}")
    for repository_url in candidate.repository_urls:
        normalized = normalize_url(repository_url)
        if normalized in official_repos:
            matched.add(official_repos[normalized]); reasons.append(f"official repository: {normalized}")

    title_text = _normalized_text(candidate.title)
    title_signal = bool(
        BIO_TERMS.search(title_text)
        and re.search(r"(?i)\b(benchmark|challenge)\w*\b", title_text)
    )
    new_benchmark_signal = not matched and bool(
        BIO_TERMS.search(text) and (title_signal or NEW_BENCHMARK_CLAIM.search(text))
    )
    if not matched and not new_benchmark_signal:
        return None
    if new_benchmark_signal:
        reasons.append("new benchmark/evaluation language plus explicit bio/chem topic")
    score = 100 if any(reason.startswith("exact") for reason in reasons) else 0
    score = max(score, 90 if any(reason.startswith("creator") for reason in reasons) else 0)
    score = max(score, 85 if any(reason.startswith("official") for reason in reasons) else 0)
    score = max(score, 45 if new_benchmark_signal else 0)
    score += 15 if candidate.open_fulltext else 0
    score += 10 if candidate.publication_date and candidate.publication_date[:4] in {"2024", "2025", "2026"} else 0
    score += 5 if candidate.peer_reviewed else 0
    score += 3 if EVAL_TERMS.search(text) else 0
    candidate.matched_benchmark_ids = sorted(matched)
    candidate.match_reasons = reasons
    candidate.area = classify_area(text)
    candidate.score = score
    if candidate.area is None:
        return None
    return candidate


def deduplicate_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen: set[tuple[str, str]] = set()
    result = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.publication_date or "", item.title)):
        identities = [
            ("doi", candidate.doi or ""),
            ("arxiv", candidate.arxiv or ""),
            ("url", normalize_url(candidate.canonical_url) or ""),
            ("title", title_fingerprint(candidate.title) or ""),
        ]
        if any(value and (kind, value) in seen for kind, value in identities):
            continue
        seen.update((kind, value) for kind, value in identities if value)
        result.append(candidate)
    return result


def select_by_quota(candidates: Iterable[Candidate]) -> list[Candidate]:
    counts = {key: 0 for key in AREA_QUOTAS}
    selected = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.publication_date or "", item.title)):
        if candidate.area not in counts or counts[candidate.area] >= AREA_QUOTAS[candidate.area]:
            continue
        selected.append(candidate)
        counts[candidate.area] += 1
        if len(selected) == MAX_CANDIDATES:
            break
    return selected


def candidate_identity_tokens(candidate: Candidate) -> set[str]:
    return {
        token for token in (
            f"candidate:{candidate.candidate_id}",
            f"doi:{candidate.doi}" if candidate.doi else None,
            f"arxiv:{candidate.arxiv}" if candidate.arxiv else None,
            f"url:{normalize_url(candidate.canonical_url)}" if normalize_url(candidate.canonical_url) else None,
            f"title:{title_fingerprint(candidate.title)}" if title_fingerprint(candidate.title) else None,
        ) if token
    }


def existing_candidate_fingerprints(
    session: requests.Session,
    repository: str,
    token: str,
) -> set[str]:
    response = _request(session, "GET", f"https://api.github.com/repos/{repository}/issues", params={
        "state": "all", "labels": "paper-candidate", "per_page": 100,
    }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    fingerprints = set()
    for issue in response.json():
        if "pull_request" in issue:
            continue
        closed = issue.get("closed_at")
        if closed and datetime.fromisoformat(closed.replace("Z", "+00:00")) < cutoff:
            continue
        body = issue.get("body") or ""
        match = re.search(r"Candidate ID:\s*`([a-f0-9]{16})`", body)
        if match:
            fingerprints.add(f"candidate:{match.group(1)}")
        sections = parse_issue_form(body)
        doi = normalize_doi(sections.get("DOI (optional)"))
        arxiv = normalize_arxiv(sections.get("arXiv or preprint ID (optional)"))[0]
        url = normalize_url(sections.get("Paper or preprint URL"))
        title = title_fingerprint(sections.get("Title (optional)"))
        fingerprints.update(token for token in (
            f"doi:{doi}" if doi else None,
            f"arxiv:{arxiv}" if arxiv else None,
            f"url:{url}" if url else None,
            f"title:{title}" if title else None,
        ) if token)
    return fingerprints


def candidate_is_existing_work(candidate: Candidate, entities: dict[str, list[dict[str, Any]]]) -> bool:
    identity = {
        "doi": candidate.doi,
        "arxiv": candidate.arxiv,
        "canonical_url": normalize_url(candidate.canonical_url),
        "title_fingerprint": title_fingerprint(candidate.title),
    }
    return bool(duplicate_work_candidates(identity, entities["work"]))


def issue_body(candidate: Candidate) -> str:
    matched = ", ".join(candidate.matched_benchmark_ids) or "Potential new benchmark"
    reasons = "\n".join(f"- {reason}" for reason in candidate.match_reasons)
    source_url = candidate.pdf_url or "_No response_"
    source_confirmed = candidate.open_fulltext and candidate.source_api in {"europe-pmc", "arxiv"}
    confirmation = (
        "- [x] This candidate points to an openly accessible source discovered through Europe PMC or arXiv."
        if source_confirmed else
        "- [ ] A maintainer must provide or confirm a legal full-text source before approving extraction; Crossref metadata alone is not evidence."
    )
    return f"""### Paper or preprint URL

{candidate.canonical_url}

### Open PDF or full-text URL / attachment (optional)

{source_url}

### DOI (optional)

{candidate.doi or '_No response_'}

### arXiv or preprint ID (optional)

{candidate.arxiv or '_No response_'}

### Title (optional)

{candidate.title}

### Possible benchmarks

{matched}

### Relevant tables, figures, or sections

_No response_

### Could this introduce a new benchmark?

{'Yes' if not candidate.matched_benchmark_ids else 'Unknown'}

### Source-use confirmation

{confirmation}

### Discovery provenance

Candidate ID: `{candidate.candidate_id}`  
Source API: `{candidate.source_api}`  
Area: `{candidate.area}`  
Open full text: `{candidate.open_fulltext}`  
Score: `{candidate.score}`

{reasons}
"""


def create_candidate_issue(session: requests.Session, repository: str, token: str, candidate: Candidate) -> str:
    response = _request(session, "POST", f"https://api.github.com/repos/{repository}/issues", json={
        "title": f"[Paper candidate] {candidate.title}",
        "body": issue_body(candidate),
        "labels": ["paper-candidate", f"area:{candidate.area}"],
    }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return response.json()["html_url"]


def close_stale_candidates(session: requests.Session, repository: str, token: str) -> list[int]:
    response = _request(session, "GET", f"https://api.github.com/repos/{repository}/issues", params={
        "state": "open", "labels": "paper-candidate", "per_page": 100,
    }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    closed = []
    for issue in response.json():
        labels = {item["name"] for item in issue.get("labels", [])}
        created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
        if (
            labels
            & {
                "ready-for-local-intake",
                "local-intake-in-progress",
                "paper-intake-pr",
            }
            or created >= cutoff
        ):
            continue
        number = issue["number"]
        _request(session, "PATCH", f"https://api.github.com/repos/{repository}/issues/{number}", json={
            "state": "closed", "state_reason": "not_planned",
        }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        closed.append(number)
    return closed


def discover(session: requests.Session | None = None) -> list[Candidate]:
    session = session or requests.Session()
    entities = load_entities()
    fetched = [
        *fetch_europe_pmc(session),
        *fetch_crossref(session),
        *fetch_crossref(
            session, max_pages=1,
            query_terms="benchmark small molecule molecular property reaction prediction retrosynthesis ADMET chemistry",
        ),
        *fetch_crossref(
            session, max_pages=1,
            query_terms="benchmark protein design binding affinity interaction folding antibody",
        ),
        *fetch_arxiv(session),
    ]
    scored = [candidate for item in fetched if (candidate := score_candidate(item, entities))]
    novel = [candidate for candidate in deduplicate_candidates(scored) if not candidate_is_existing_work(candidate, entities)]
    return select_by_quota(novel)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--create-issues", action="store_true")
    parser.add_argument("--close-stale", action="store_true")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    session = requests.Session()
    candidates = discover(session)
    payload: dict[str, Any] = {"candidates": [asdict(candidate) | {"candidate_id": candidate.candidate_id} for candidate in candidates]}
    if args.create_issues or args.close_stale:
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not args.repository:
            raise SystemExit("GITHUB_TOKEN and --repository are required for issue mutations")
        known = existing_candidate_fingerprints(session, args.repository, token)
        payload["issues"] = [
            create_candidate_issue(session, args.repository, token, candidate)
            for candidate in candidates if not (candidate_identity_tokens(candidate) & known)
        ] if args.create_issues else []
        payload["closed_stale"] = close_stale_candidates(session, args.repository, token) if args.close_stale else []
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
