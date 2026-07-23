from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from registry_io import load_entities  # noqa: E402
from local_paper_intake import ensure_issue_for_url  # noqa: E402
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


def test_local_paper_intake_skill_and_entrypoint_exist() -> None:
    skill = ROOT / ".agents" / "skills" / "biobench-paper-intake"
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    agent = (skill / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert text.startswith("---\nname: biobench-paper-intake\n")
    assert "$biobench-paper-intake issue 44" in text
    assert "biobench-paper-intake" in agent
    assert (ROOT / "scripts" / "local_paper_intake.py").exists()


def test_url_entry_reuses_issue_by_doi_before_creating_another(
    monkeypatch,
) -> None:
    body = """### Paper or preprint URL

https://publisher.example/article

### DOI (optional)

10.1234/EXAMPLE

### Title (optional)

Example benchmark paper
"""
    commands: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            payload = [{
                "number": 44,
                "title": "[Paper intake]: Example benchmark paper",
                "body": body,
                "labels": [{"name": "paper-candidate"}],
                "state": "OPEN",
                "url": "https://github.com/SpectrAI-Initiative/bio-benchmark-atlas/issues/44",
            }]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(
        "local_paper_intake.build_intake",
        lambda **_: {
            "normalized_identity": {
                "doi": "10.1234/example",
                "arxiv": None,
                "canonical_url": "https://doi.org/10.1234/example",
                "title_fingerprint": "examplebenchmarkpaper",
                "title": "Example benchmark paper",
            },
        },
    )
    assert ensure_issue_for_url("https://doi.org/10.1234/example", runner=runner) == 44
    assert not any(command[:3] == ["gh", "issue", "create"] for command in commands)
