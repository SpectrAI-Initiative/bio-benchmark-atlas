#!/usr/bin/env python3
"""Owner-triggered local Codex workflow for paper intake and reviewable PR creation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from extract_paper import (
    DEFAULT_MODEL,
    EXTRACTOR_PROMPT,
    PROMPT_VERSION,
    VERIFIER_PROMPT,
    CodexExecutionError,
    PaperExtractionError,
    codex_version,
)
from generate_paper_records import GenerationBlocked, chinese_summary, stable_work_id
from paper_extraction_eval import golden_input_hash, run_golden
from paper_source import SourceAcquisitionError, retrieve_source
from registry_io import load_entities
from run_paper_intake import _arxiv_pdf_url, _first_url, _is_checked, process_issue
from triage_paper import (
    build_intake,
    normalize_arxiv,
    normalize_doi,
    normalize_url,
    parse_issue_form,
    title_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "SpectrAI-Initiative/bio-benchmark-atlas"
STATE_ROOT = Path.home() / ".codex" / "biobench-atlas"
RUN_ROOT = STATE_ROOT / "runs"
GOLDEN_RECEIPT = STATE_ROOT / "golden.json"
OWNER = "wang422003"
MAX_GOLDEN_AGE = timedelta(days=35)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class LocalIntakeError(RuntimeError):
    """A local workflow precondition or lifecycle operation failed."""


@dataclass(frozen=True)
class Preflight:
    issue_number: int
    issue_url: str
    paper_url: str
    source_url: str
    source_sha256: str
    source_content_type: str
    source_pages: int | None
    work_id_hint: str
    duplicate_work_ids: list[str]
    existing_pr_url: str | None
    base_sha: str
    codex_cli_version: str
    golden_status: str


def _run(
    command: list[str],
    *,
    runner: CommandRunner = subprocess.run,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2000:].strip()
        raise LocalIntakeError(f"{command[0]} command failed: {detail}")
    return completed


def _json_command(command: list[str], *, runner: CommandRunner = subprocess.run) -> Any:
    completed = _run(command, runner=runner)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LocalIntakeError(f"{command[0]} returned invalid JSON") from error


def _git(*args: str, runner: CommandRunner = subprocess.run) -> str:
    return _run(["git", *args], runner=runner).stdout.strip()


def _gh(*args: str, runner: CommandRunner = subprocess.run) -> str:
    return _run(["gh", *args], runner=runner).stdout.strip()


def check_local_tools(*, runner: CommandRunner = subprocess.run) -> str:
    _run(["git", "--version"], runner=runner)
    _run(["gh", "auth", "status"], runner=runner)
    version = codex_version(runner=runner)
    return version


def _issue(number: int, *, runner: CommandRunner = subprocess.run) -> dict[str, Any]:
    payload = _json_command([
        "gh", "issue", "view", str(number), "--repo", REPOSITORY,
        "--json", "number,title,body,labels,state,url",
    ], runner=runner)
    if payload.get("state") != "OPEN":
        raise LocalIntakeError(f"issue #{number} is not open")
    return payload


def _issue_labels(issue: dict[str, Any]) -> set[str]:
    return {item["name"] for item in issue.get("labels", [])}


def _list_intake_issues(*, runner: CommandRunner = subprocess.run) -> list[dict[str, Any]]:
    return _json_command([
        "gh", "issue", "list", "--repo", REPOSITORY, "--state", "all", "--limit", "100",
        "--json", "number,title,body,labels,state,url",
    ], runner=runner)


def _issue_identity(body: str) -> dict[str, str | None]:
    sections = parse_issue_form(body)
    paper_url = sections.get("Paper or preprint URL")
    arxiv_base, _ = normalize_arxiv(
        sections.get("arXiv or preprint ID (optional)") or paper_url
    )
    return {
        "doi": normalize_doi(sections.get("DOI (optional)") or paper_url),
        "arxiv": arxiv_base,
        "canonical_url": normalize_url(paper_url),
        "title_fingerprint": title_fingerprint(sections.get("Title (optional)")),
    }


def find_issue_for_url(
    url: str,
    *,
    identity: dict[str, Any] | None = None,
    runner: CommandRunner = subprocess.run,
) -> int | None:
    target = identity or build_intake(url=url, resolve=True)["normalized_identity"]
    for item in _list_intake_issues(runner=runner):
        candidate = _issue_identity(item.get("body") or "")
        for key in ("doi", "arxiv", "canonical_url", "title_fingerprint"):
            if target.get(key) and candidate.get(key) == target.get(key):
                return int(item["number"])
    return None


def _direct_issue_body(url: str, intake: dict[str, Any]) -> str:
    identity = intake["normalized_identity"]
    arxiv = identity.get("arxiv_version") or identity.get("arxiv")
    automatic_open = "arxiv.org/" in url.casefold()
    source_url = _arxiv_pdf_url(url) if automatic_open else ""
    confirmation = (
        "- [x] The source is a recognized open arXiv source and may be read locally for this review."
        if automatic_open else
        "- [ ] Add a legal open full-text URL or confirm authorization before local extraction."
    )
    return f"""### Paper or preprint URL

{url}

### Open PDF or full-text URL / attachment (optional)

{source_url or "_No response_"}

### DOI (optional)

{identity.get("doi") or "_No response_"}

### arXiv or preprint ID (optional)

{arxiv or "_No response_"}

### Title (optional)

{identity.get("title") or "_No response_"}

### Possible benchmarks

_No response_

### Relevant tables, figures, or sections

_No response_

### Could this introduce a new benchmark?

Unknown

### Source-use confirmation

{confirmation}
"""


def ensure_issue_for_url(url: str, *, runner: CommandRunner = subprocess.run) -> int:
    intake = build_intake(url=url, resolve=True)
    existing = find_issue_for_url(
        url,
        identity=intake["normalized_identity"],
        runner=runner,
    )
    if existing is not None:
        return existing
    title = intake["normalized_identity"].get("title") or url
    body = _direct_issue_body(url, intake)
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        output = _gh(
            "issue", "create", "--repo", REPOSITORY,
            "--title", f"[Paper intake]: {title}",
            "--label", "paper-intake",
            "--label", "paper-candidate",
            "--body-file", str(body_path),
            runner=runner,
        )
    finally:
        body_path.unlink(missing_ok=True)
    match = re.search(r"/issues/(\d+)$", output)
    if not match:
        raise LocalIntakeError("created paper issue URL could not be parsed")
    return int(match.group(1))


def _existing_pr(issue_number: int, *, runner: CommandRunner = subprocess.run) -> str | None:
    items = _json_command([
        "gh", "pr", "list", "--repo", REPOSITORY, "--state", "all", "--limit", "100",
        "--json", "headRefName,url",
    ], runner=runner)
    for item in items:
        if str(item["headRefName"]).endswith(f"-{issue_number}"):
            return str(item["url"])
    return None


def _clean_current_main(*, runner: CommandRunner = subprocess.run) -> str:
    if _git("status", "--porcelain", runner=runner):
        raise LocalIntakeError("working tree is not clean")
    if _git("branch", "--show-current", runner=runner) != "main":
        raise LocalIntakeError("paper intake must start from the main branch")
    local_sha = _git("rev-parse", "main", runner=runner)
    remote_sha = _git("rev-parse", "origin/main", runner=runner)
    if local_sha != remote_sha:
        raise LocalIntakeError("local main is not fast-forward synchronized with origin/main")
    return local_sha


def _source_details(issue: dict[str, Any]) -> tuple[str, bool, bool]:
    sections = parse_issue_form(issue["body"])
    paper_url = sections.get("Paper or preprint URL")
    if not paper_url:
        raise LocalIntakeError("issue has no Paper or preprint URL")
    supplied = (
        sections.get("Open PDF or full-text URL / attachment (optional)")
        or sections.get("Open PDF or full-text URL (optional)")
    )
    source_url = _first_url(supplied) or _arxiv_pdf_url(paper_url)
    rights_confirmed = _is_checked(sections.get("Source-use confirmation", ""))
    discovered = "paper-candidate" in _issue_labels(issue)
    return source_url, rights_confirmed, discovered


def _work_hint(issue: dict[str, Any]) -> tuple[str, list[str]]:
    sections = parse_issue_form(issue["body"])
    intake = build_intake(
        url=sections["Paper or preprint URL"],
        doi=sections.get("DOI (optional)"),
        arxiv=sections.get("arXiv or preprint ID (optional)"),
        title=sections.get("Title (optional)"),
        benchmark_hints=sections.get("Possible benchmarks", ""),
        focus_locators=sections.get("Relevant tables, figures, or sections", ""),
        may_contain_new_benchmark=sections.get("Could this introduce a new benchmark?", ""),
        resolve=True,
    )
    duplicate_ids = [item["work_id"] for item in intake["duplicate_work_candidates"]]
    if duplicate_ids:
        return duplicate_ids[0], duplicate_ids
    entities = load_entities()
    existing_ids = {item["id"] for item in entities["work"]}
    identity = intake["normalized_identity"]
    title = identity.get("title") or sections.get("Title (optional)") or "paper-intake"
    return stable_work_id(title, identity.get("doi"), existing_ids), []


def _golden_status(*, version: str) -> str:
    try:
        require_fresh_golden(version=version)
    except LocalIntakeError as error:
        return f"blocked: {error}"
    return "current"


def preflight_issue(
    issue_number: int,
    *,
    runner: CommandRunner = subprocess.run,
    require_clean_main: bool = True,
) -> Preflight:
    version = check_local_tools(runner=runner)
    base_sha = _clean_current_main(runner=runner) if require_clean_main else _git("rev-parse", "HEAD", runner=runner)
    issue = _issue(issue_number, runner=runner)
    source_url, rights_confirmed, discovered = _source_details(issue)
    source = retrieve_source(source_url, rights_confirmed=rights_confirmed, discovered=discovered)
    try:
        work_id, duplicate_ids = _work_hint(issue)
        return Preflight(
            issue_number=issue_number,
            issue_url=issue["url"],
            paper_url=parse_issue_form(issue["body"])["Paper or preprint URL"],
            source_url=source.url,
            source_sha256=source.content_sha256,
            source_content_type=source.content_type,
            source_pages=source.page_count,
            work_id_hint=work_id,
            duplicate_work_ids=duplicate_ids,
            existing_pr_url=_existing_pr(issue_number, runner=runner),
            base_sha=base_sha,
            codex_cli_version=version,
            golden_status=_golden_status(version=version),
        )
    finally:
        source.path.unlink(missing_ok=True)


def _major_cli_version(value: str) -> str:
    match = re.search(r"(\d+)\.", value)
    return match.group(1) if match else value


def require_fresh_golden(*, version: str) -> dict[str, Any]:
    if not GOLDEN_RECEIPT.exists():
        raise LocalIntakeError("no local golden receipt exists; run the golden command")
    try:
        receipt = json.loads(GOLDEN_RECEIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalIntakeError("local golden receipt is invalid") from error
    if receipt.get("passed") is not True:
        raise LocalIntakeError("latest local golden evaluation did not pass")
    completed = datetime.fromisoformat(str(receipt["completed_at"]).replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - completed > MAX_GOLDEN_AGE:
        raise LocalIntakeError("local golden receipt is older than 35 days")
    expected_hash = golden_input_hash(DEFAULT_MODEL, DEFAULT_MODEL)
    if receipt.get("input_hash") != expected_hash:
        raise LocalIntakeError("prompt, schema, or requested model changed since the golden run")
    if _major_cli_version(receipt.get("codex_cli_version", "")) != _major_cli_version(version):
        raise LocalIntakeError("Codex CLI major version changed since the golden run")
    return receipt


def _ensure_labels(*, runner: CommandRunner = subprocess.run) -> None:
    definitions = {
        "paper-candidate": ("1d76db", "Paper awaiting owner-selected local intake"),
        "ready-for-local-intake": ("0e8a16", "Owner selected paper for local Codex intake"),
        "local-intake-in-progress": ("fbca04", "Local Codex paper intake is running"),
        "needs-human-review": ("d93f0b", "Paper intake requires source or evidence review"),
        "intake-failed": ("b60205", "Local paper intake stopped on a technical failure"),
        "paper-intake-pr": ("6f42c1", "Paper intake has a reviewable pull request"),
    }
    for name, (color, description) in definitions.items():
        _gh(
            "label", "create", name, "--repo", REPOSITORY,
            "--color", color, "--description", description, "--force",
            runner=runner,
        )


def _claim_issue(issue: dict[str, Any], run_id: str, base_sha: str, *, runner: CommandRunner) -> None:
    labels = _issue_labels(issue)
    if "local-intake-in-progress" in labels:
        raise LocalIntakeError("issue already has an active local intake")
    _ensure_labels(runner=runner)
    arguments = [
        "issue", "edit", str(issue["number"]), "--repo", REPOSITORY,
        "--add-label", "ready-for-local-intake",
        "--add-label", "local-intake-in-progress",
    ]
    if "paper-candidate" in labels:
        arguments.extend(["--remove-label", "paper-candidate"])
    _gh(*arguments, runner=runner)
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _gh(
        "issue", "comment", str(issue["number"]), "--repo", REPOSITORY,
        "--body", (
            "<!-- biobench-local-intake-claim -->\n"
            f"Local Codex intake claimed this issue. Run: `{run_id}` · base: `{base_sha}` · started: `{started}`."
        ),
        runner=runner,
    )


def _state_path(run_id: str) -> Path:
    return RUN_ROOT / f"{run_id}.json"


def _save_state(run_id: str, payload: dict[str, Any]) -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    _state_path(run_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_state(run_id: str) -> dict[str, Any]:
    path = _state_path(run_id)
    if not path.exists():
        raise LocalIntakeError(f"local run {run_id} does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_generated_output(*, runner: CommandRunner) -> None:
    commands = [
        [sys.executable, "scripts/validate_registry.py"],
        [sys.executable, "-m", "pytest"],
        [sys.executable, "scripts/build_registry.py"],
        ["pnpm", "site:build"],
        ["pnpm", "site:test"],
    ]
    for command in commands:
        _run(command, runner=runner)


def _publish_records(
    *,
    issue: dict[str, Any],
    work_id: str,
    run_id: str,
    summary: str,
    runner: CommandRunner,
) -> str:
    branch = f"paper-intake/{work_id}-{issue['number']}"
    if _existing_pr(int(issue["number"]), runner=runner):
        raise LocalIntakeError("an intake PR already exists for this issue")
    _git("switch", "-c", branch, runner=runner)
    _git("add", "registry", runner=runner)
    staged = _git("diff", "--cached", "--name-only", runner=runner).splitlines()
    if not staged or any(not name.startswith("registry/") for name in staged):
        raise LocalIntakeError("paper intake may commit Registry files only")
    forbidden = (".pdf", ".xml", ".html", ".txt", ".json")
    if any(name.casefold().endswith(forbidden) for name in staged):
        raise LocalIntakeError("paper source or model artifacts were staged")
    _git("commit", "-m", f"data: intake {work_id} from issue #{issue['number']}", runner=runner)
    _git("push", "-u", "origin", branch, runner=runner)
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
        handle.write(summary)
        handle.write(f"\nCloses #{issue['number']}\n")
        summary_path = Path(handle.name)
    try:
        pr_url = _gh(
            "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head", branch,
            "--label", "paper-intake", "--title", f"Paper intake: {work_id}",
            "--body-file", str(summary_path),
            runner=runner,
        )
    finally:
        summary_path.unlink(missing_ok=True)
    return pr_url


def _mark_issue_success(issue_number: int, pr_url: str, *, runner: CommandRunner) -> None:
    _gh(
        "issue", "edit", str(issue_number), "--repo", REPOSITORY,
        "--remove-label", "local-intake-in-progress",
        "--remove-label", "ready-for-local-intake",
        "--add-label", "paper-intake-pr",
        runner=runner,
    )
    _gh(
        "issue", "comment", str(issue_number), "--repo", REPOSITORY,
        "--body", (
            f"Created a Ready local intake PR: {pr_url}. After CI passes, `wang422003` must comment "
            "`/approve-paper-intake <full-current-head-sha>` on that PR. The workflow never auto-merges."
        ),
        runner=runner,
    )


def _mark_issue_failure(issue_number: int, label: str, *, runner: CommandRunner) -> None:
    issue = _issue(issue_number, runner=runner)
    labels = _issue_labels(issue)
    arguments = ["issue", "edit", str(issue_number), "--repo", REPOSITORY, "--add-label", label]
    if "local-intake-in-progress" in labels:
        arguments.extend(["--remove-label", "local-intake-in-progress"])
    _gh(*arguments, runner=runner)
    _gh(
        "issue", "comment", str(issue_number), "--repo", REPOSITORY,
        "--body", (
            f"Local Codex intake stopped safely with `{label}`. No unsupported claim was published, "
            "and paper full text plus model drafts were removed from the workspace."
        ),
        runner=runner,
    )


def run_issue(
    issue_number: int,
    *,
    run_id: str | None = None,
    runner: CommandRunner = subprocess.run,
) -> str:
    selected_run_id = run_id or str(uuid.uuid4())
    preflight = preflight_issue(issue_number, runner=runner)
    if preflight.existing_pr_url:
        raise LocalIntakeError(f"an intake PR already exists: {preflight.existing_pr_url}")
    if preflight.golden_status != "current":
        raise LocalIntakeError(preflight.golden_status)
    issue = _issue(issue_number, runner=runner)
    _save_state(selected_run_id, {
        "run_id": selected_run_id,
        "issue_number": issue_number,
        "base_sha": preflight.base_sha,
        "status": "claimed",
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    })
    _claim_issue(issue, selected_run_id, preflight.base_sha, runner=runner)
    records, source, result = process_issue(
        issue["body"],
        discovered="paper-candidate" in _issue_labels(issue),
        extractor_model=DEFAULT_MODEL,
        verifier_model=DEFAULT_MODEL,
        write=True,
        local_run_id=selected_run_id,
    )
    _save_state(selected_run_id, {
        "run_id": selected_run_id,
        "issue_number": issue_number,
        "base_sha": preflight.base_sha,
        "status": "reviewed",
        "source_sha256": source.content_sha256,
        "extractor_thread_id": result.extractor_thread_id,
        "verifier_thread_id": result.verifier_thread_id,
        "codex_cli_version": result.codex_cli_version,
    })
    _validate_generated_output(runner=runner)
    work_id = records.work["id"] if records.work else records.uses[0]["work_id"]
    receipt = require_fresh_golden(version=result.codex_cli_version)
    summary = chinese_summary(records)
    summary += (
        f"\nSource SHA256: `{source.content_sha256}`  \n"
        f"Extractor thread: `{result.extractor_thread_id}`  \n"
        f"Verifier thread: `{result.verifier_thread_id}`  \n"
        f"Codex CLI: `{result.codex_cli_version}`  \n"
        f"Local run: `{selected_run_id}`  \n"
        f"Golden: `{receipt['completed_at']}` · `{receipt['input_hash']}`\n\n"
        "Confirmed: no paper full text, long excerpt, Codex transcript, extraction draft, "
        "or verification draft is included in this PR.\n"
    )
    pr_url = _publish_records(
        issue=issue,
        work_id=work_id,
        run_id=selected_run_id,
        summary=summary,
        runner=runner,
    )
    _mark_issue_success(issue_number, pr_url, runner=runner)
    _save_state(selected_run_id, {
        "run_id": selected_run_id,
        "issue_number": issue_number,
        "base_sha": preflight.base_sha,
        "status": "pr-open",
        "pr_url": pr_url,
        "source_sha256": source.content_sha256,
        "extractor_thread_id": result.extractor_thread_id,
        "verifier_thread_id": result.verifier_thread_id,
        "codex_cli_version": result.codex_cli_version,
    })
    return pr_url


def _resolve_issue_argument(args: argparse.Namespace, *, runner: CommandRunner) -> int:
    if args.issue is not None:
        return args.issue
    if args.url:
        return ensure_issue_for_url(args.url, runner=runner)
    raise LocalIntakeError("--issue or --url is required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        command = subparsers.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--issue", type=int)
        source.add_argument("--url")
    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    subparsers.add_parser("golden")
    args = parser.parse_args()

    issue_number: int | None = None
    try:
        if args.command == "golden":
            receipt = run_golden(output=GOLDEN_RECEIPT)
            print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "resume":
            state = _load_state(args.run_id)
            issue_number = int(state["issue_number"])
            if state.get("status") == "pr-open" and state.get("pr_url"):
                print(state["pr_url"])
                return 0
            print(run_issue(issue_number, run_id=args.run_id))
            return 0
        issue_number = _resolve_issue_argument(args, runner=subprocess.run)
        if args.command == "preflight":
            print(json.dumps(asdict(preflight_issue(issue_number)), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(run_issue(issue_number))
        return 0
    except Exception as error:
        if issue_number is not None and args.command in {"run", "resume"}:
            label = (
                "needs-human-review"
                if isinstance(error, (GenerationBlocked, PaperExtractionError, SourceAcquisitionError))
                and not isinstance(error, CodexExecutionError)
                else "intake-failed"
            )
            try:
                _mark_issue_failure(issue_number, label, runner=subprocess.run)
            except Exception:
                pass
        print(f"paper intake stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
