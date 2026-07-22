from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from registry_io import load_entities  # noqa: E402
from triage_paper import (  # noqa: E402
    benchmark_candidates,
    build_intake,
    duplicate_work_candidates,
    normalize_arxiv,
    normalize_doi,
    normalize_url,
    parse_issue_form,
    title_fingerprint,
)


def test_identifier_normalization_and_issue_form_parsing() -> None:
    assert normalize_doi("https://doi.org/10.48550/arXiv.2512.21907") == "10.48550/arxiv.2512.21907"
    assert normalize_arxiv("https://arxiv.org/pdf/2512.21907v2.pdf") == ("2512.21907", "2512.21907v2")
    assert normalize_url("HTTPS://ARXIV.ORG/abs/2512.21907/#fragment") == "https://arxiv.org/abs/2512.21907"
    assert title_fingerprint("SpatialBench:  A Benchmark!") == "spatialbenchabenchmark"
    body = """### Paper or preprint URL

https://arxiv.org/abs/2512.21907

### Possible benchmarks

SpatialBench and BixBench

### Relevant tables, figures, or sections

Table 1
"""
    assert parse_issue_form(body) == {
        "Paper or preprint URL": "https://arxiv.org/abs/2512.21907",
        "Possible benchmarks": "SpatialBench and BixBench",
        "Relevant tables, figures, or sections": "Table 1",
    }


def test_duplicate_priority_and_benchmark_alias_matching() -> None:
    entities = load_entities()
    spatial_work = next(work for work in entities["work"] if work["id"] == "spatialbench-preprint")
    identity = {
        "doi": normalize_doi(spatial_work["doi"]),
        "arxiv": spatial_work["arxiv"],
        "canonical_url": normalize_url(spatial_work["canonical_url"]),
        "title_fingerprint": title_fingerprint(spatial_work["title"]),
    }
    assert duplicate_work_candidates(identity, entities["work"]) == [
        {"work_id": "spatialbench-preprint", "matched_by": "doi"}
    ]
    matches = benchmark_candidates("We evaluate SpatialBench and BixBench.", entities["benchmark"])
    assert {match["benchmark_id"] for match in matches} == {"spatialbench", "bixbench"}


def test_build_intake_is_non_production_and_does_not_infer_relations() -> None:
    intake = build_intake(
        url="https://arxiv.org/abs/2512.21907v2",
        title="SpatialBench: Can Agents Solve Spatial Biology Challenges?",
        benchmark_hints="SpatialBench",
        focus_locators="Table 1",
        may_contain_new_benchmark="No",
        resolve=False,
    )
    assert intake["status"] == "needs-human-review"
    assert intake["production_ready"] is False
    assert intake["normalized_identity"]["arxiv"] == "2512.21907"
    assert intake["normalized_identity"]["arxiv_version"] == "2512.21907v2"
    assert intake["duplicate_work_candidates"][0]["work_id"] == "spatialbench-preprint"
    assert intake["benchmark_candidates"][0]["benchmark_id"] == "spatialbench"
    assert "relation_type" not in intake


def test_paper_intake_workflow_is_valid_yaml() -> None:
    workflow = ROOT / ".github" / "workflows" / "paper-intake.yml"
    payload = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert payload["name"] == "Paper intake"
    assert {"authorize", "extract"} <= set(payload["jobs"])
