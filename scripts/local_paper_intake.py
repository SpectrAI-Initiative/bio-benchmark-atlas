#!/usr/bin/env python3
"""Owner-triggered local Codex workflow for paper intake and reviewable PR creation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from extract_paper import (
    DEFAULT_MODEL,
    EXTRACTOR_PROMPT,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_PATH,
    HEARTBEAT_ROOT,
    PROMPT_VERSION,
    VERIFIER_PROMPT,
    CodexExecutionError,
    PaperExtractionError,
    codex_version,
    heartbeat_path,
)
from generate_paper_records import GenerationBlocked, chinese_summary, stable_work_id
from paper_extraction_eval import golden_input_hash, run_golden
from paper_source import SourceAcquisitionError, retrieve_source
from registry_io import load_entities
from run_paper_intake import (
    _arxiv_pdf_url,
    _first_url,
    _focus_pdf_pages,
    _is_checked,
    process_issue,
)
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
OWNER_LOGIN = "wang422003"
STATE_ROOT = Path.home() / ".codex" / "biobench-atlas"
RUN_ROOT = STATE_ROOT / "runs"
WORKTREE_ROOT = STATE_ROOT / "worktrees"
RUN_LOCK_PATH = STATE_ROOT / "intake.lock"
GOLDEN_RECEIPT = STATE_ROOT / "golden.json"
OWNER = "wang422003"
MAX_GOLDEN_AGE = timedelta(days=35)
MAX_PARALLEL_RUNS = 3
ACTIVE_RUN_STATUSES = {
    "reserved",
    "claimed",
    "reviewing",
    "reviewed",
    "validating",
    "publishing",
}

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
GH_TRANSIENT_ERRORS = (
    "connection reset",
    "eof",
    "stream error",
    "tls handshake timeout",
    "timeout awaiting response headers",
    " 502 ",
    " 503 ",
    " 504 ",
)
GH_MAX_ATTEMPTS = 3


class LocalIntakeError(RuntimeError):
    """A local workflow precondition or lifecycle operation failed."""


def _heartbeat_payload(path: Path, *, now: datetime) -> dict[str, Any]:
    """Read one privacy-safe heartbeat and calculate its liveness."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise LocalIntakeError(f"local heartbeat is unreadable: {path.name}") from error
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_seconds = max(0, round((now - updated_at).total_seconds()))
    process_pid = payload.get("process_pid")
    process_alive = False
    if isinstance(process_pid, int) and process_pid > 0:
        try:
            os.kill(process_pid, 0)
            process_alive = True
        except (OSError, ValueError):
            process_alive = False
    stale_after = max(150, HEARTBEAT_INTERVAL_SECONDS * 2 + 30)
    payload["heartbeat_age_seconds"] = age_seconds
    payload["process_alive"] = process_alive
    payload["stale"] = (
        payload.get("status") == "running"
        and (age_seconds > stale_after or not process_alive)
    )
    payload["heartbeat_path"] = str(path)
    return payload


def heartbeat_status(
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one or all safe liveness states without exposing paper content."""

    current = now or datetime.now(timezone.utc)
    if run_id is not None:
        path = heartbeat_path(run_id)
        if not path.exists():
            return {
                "status": "not-started",
                "run_id": run_id,
                "heartbeat_path": str(path),
                "stale": False,
            }
        return _heartbeat_payload(path, now=current)

    paths = sorted(HEARTBEAT_ROOT.glob("*.json")) if HEARTBEAT_ROOT.exists() else []
    # Read the legacy singleton only when no per-run heartbeat exists.
    if not paths and HEARTBEAT_PATH.exists():
        paths = [HEARTBEAT_PATH]
    if not paths:
        return {
            "status": "idle",
            "active_count": 0,
            "max_parallel": MAX_PARALLEL_RUNS,
            "runs": [],
        }
    runs = [_heartbeat_payload(path, now=current) for path in paths]
    active = [
        payload for payload in runs
        if payload.get("status") == "running" and not payload.get("stale")
    ]
    return {
        "status": "running" if active else "idle",
        "active_count": len(active),
        "max_parallel": MAX_PARALLEL_RUNS,
        "runs": runs,
    }


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


@dataclass(frozen=True)
class BatchWorktree:
    issue_number: int
    run_id: str
    work_id_hint: str
    branch: str
    path: Path
    base_sha: str


def _run(
    command: list[str],
    *,
    runner: CommandRunner = subprocess.run,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    attempts = GH_MAX_ATTEMPTS if command and command[0] == "gh" else 1
    for attempt in range(attempts):
        completed = runner(
            command,
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0 or not check:
            return completed
        detail = (completed.stderr or completed.stdout)[-2000:].strip()
        transient = any(marker in f" {detail.casefold()} " for marker in GH_TRANSIENT_ERRORS)
        if not transient or attempt == attempts - 1:
            raise LocalIntakeError(f"{command[0]} command failed: {detail}")
        time.sleep(2 ** attempt)
    raise AssertionError("command retry loop exited without returning")


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
        "--json", "number,title,body,labels,state,url,author,comments",
    ], runner=runner)
    if payload.get("state") != "OPEN":
        raise LocalIntakeError(f"issue #{number} is not open")
    return payload


def _owner_conflict_resolution(issue: dict[str, Any]) -> dict[str, Any] | None:
    """Read the single narrow conflict override supported by local intake.

    The command does not accept prose or arbitrary field paths. It only lets the
    repository owner preserve an independently supported root total while
    excluding every conflicted benchmark subcount.
    """

    pattern = re.compile(
        r"^/resolve-paper-conflict benchmark-total=(\d+) "
        r"exclude=(benchmark-subcounts(?:,creator-evaluation)?)$"
    )
    resolutions: list[dict[str, Any]] = []
    for comment in issue.get("comments", []):
        author = (comment.get("author") or {}).get("login")
        if author != OWNER_LOGIN:
            continue
        match = pattern.fullmatch(str(comment.get("body") or "").strip())
        if not match:
            continue
        total = int(match.group(1))
        if total <= 0:
            continue
        resolutions.append({
            "benchmark_total": total,
            "exclude": match.group(2),
            "exclude_creator_evaluation": match.group(2).endswith(",creator-evaluation"),
            "approved_by": OWNER_LOGIN,
            "approved_at": comment.get("createdAt"),
        })
    if len(resolutions) > 1 and len({item["benchmark_total"] for item in resolutions}) > 1:
        raise LocalIntakeError("owner conflict-resolution comments disagree")
    return resolutions[-1] if resolutions else None


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


def _batch_worktree_plan(
    issue_numbers: list[int],
    *,
    runner: CommandRunner = subprocess.run,
) -> list[BatchWorktree]:
    """Plan independent worktrees without downloading or reviewing paper content."""

    if not issue_numbers:
        raise LocalIntakeError("batch requires at least one --issue")
    if len(set(issue_numbers)) != len(issue_numbers):
        raise LocalIntakeError("batch issue numbers must be unique")
    base_sha = _clean_current_main(runner=runner)
    version = check_local_tools(runner=runner)
    require_fresh_golden(version=version)
    with _run_state_lock():
        active = _active_run_states()
        active_issues = {int(item["issue_number"]) for item in active}
        duplicated = sorted(active_issues & set(issue_numbers))
        if duplicated:
            raise LocalIntakeError(
                "batch includes issues that already have active runs: "
                + ", ".join(f"#{number}" for number in duplicated)
            )
        available = MAX_PARALLEL_RUNS - len(active)
        if len(issue_numbers) > available:
            raise LocalIntakeError(
                f"batch requests {len(issue_numbers)} runs but only {available} of "
                f"{MAX_PARALLEL_RUNS} local slots are available"
            )

    plans: list[BatchWorktree] = []
    for issue_number in issue_numbers:
        issue = _issue(issue_number, runner=runner)
        if "local-intake-in-progress" in _issue_labels(issue):
            raise LocalIntakeError(f"issue #{issue_number} already has an active local intake")
        existing_pr = _existing_pr(issue_number, runner=runner)
        if existing_pr:
            raise LocalIntakeError(f"issue #{issue_number} already has intake PR {existing_pr}")
        work_id, _ = _work_hint(issue)
        run_id = str(uuid.uuid4())
        branch = f"paper-intake/{work_id}-{issue_number}"
        plans.append(BatchWorktree(
            issue_number=issue_number,
            run_id=run_id,
            work_id_hint=work_id,
            branch=branch,
            path=WORKTREE_ROOT / run_id,
            base_sha=base_sha,
        ))
    return plans


def _create_batch_worktree(
    plan: BatchWorktree,
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    local_branches = _git("branch", "--list", plan.branch, runner=runner).splitlines()
    if local_branches:
        raise LocalIntakeError(f"local branch already exists: {plan.branch}")
    remote = _git("ls-remote", "--heads", "origin", plan.branch, runner=runner)
    if remote:
        raise LocalIntakeError(f"remote branch already exists: {plan.branch}")
    _run(
        [
            "git", "worktree", "add", "-b", plan.branch,
            str(plan.path), plan.base_sha,
        ],
        runner=runner,
    )


def _remove_batch_worktree(
    plan: BatchWorktree,
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    _run(
        ["git", "worktree", "remove", "--force", str(plan.path)],
        runner=runner,
        check=False,
    )


def _run_batch_worker(plan: BatchWorktree) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/local_paper_intake.py",
        "run",
        "--issue",
        str(plan.issue_number),
        "--run-id",
        plan.run_id,
        "--prepared-worktree",
    ]
    completed = subprocess.run(
        command,
        cwd=plan.path,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout.strip()
    pr_url = next(
        (line.strip() for line in reversed(output.splitlines()) if "/pull/" in line),
        None,
    )
    return {
        "issue_number": plan.issue_number,
        "run_id": plan.run_id,
        "branch": plan.branch,
        "status": "pr-open" if completed.returncode == 0 and pr_url else "failed",
        "pr_url": pr_url,
        "error": (
            None
            if completed.returncode == 0 and pr_url
            else completed.stderr[-2000:].strip() or "worker did not report a PR URL"
        ),
    }


def run_batch(
    issue_numbers: list[int],
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Run up to three independent paper intakes and leave merging serialized."""

    plans = _batch_worktree_plan(issue_numbers, runner=runner)
    prepared: list[BatchWorktree] = []
    try:
        for plan in plans:
            _create_batch_worktree(plan, runner=runner)
            prepared.append(plan)
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_RUNS, len(plans))) as executor:
            futures = {executor.submit(_run_batch_worker, plan): plan for plan in plans}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: issue_numbers.index(int(item["issue_number"])))
        return {
            "max_parallel": MAX_PARALLEL_RUNS,
            "merge_policy": "sequential",
            "results": results,
            "failed": sum(item["status"] != "pr-open" for item in results),
        }
    finally:
        for plan in prepared:
            _remove_batch_worktree(plan, runner=runner)


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


def _clean_prepared_worktree(*, runner: CommandRunner = subprocess.run) -> str:
    if _git("status", "--porcelain", runner=runner):
        raise LocalIntakeError("prepared paper-intake worktree is not clean")
    branch = _git("branch", "--show-current", runner=runner)
    if not branch.startswith("paper-intake/"):
        raise LocalIntakeError("prepared worktree must use a paper-intake branch")
    return _git("rev-parse", "HEAD", runner=runner)


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
    base_sha = (
        _clean_current_main(runner=runner)
        if require_clean_main
        else _clean_prepared_worktree(runner=runner)
    )
    issue = _issue(issue_number, runner=runner)
    source_url, rights_confirmed, discovered = _source_details(issue)
    sections = parse_issue_form(issue["body"])
    preferred_pdf_pages = _focus_pdf_pages(
        sections.get("Relevant tables, figures, or sections", "")
    )
    source = retrieve_source(
        source_url,
        rights_confirmed=rights_confirmed,
        discovered=discovered,
        preferred_pdf_pages=preferred_pdf_pages,
    )
    try:
        if source.content_type == "application/pdf" and shutil.which("pdftoppm") is None:
            raise LocalIntakeError(
                "PDF visual review requires pdftoppm (Poppler); install it before intake"
            )
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
    current = _json_command([
        "gh", "label", "list", "--repo", REPOSITORY,
        "--limit", "100", "--json", "name",
    ], runner=runner)
    existing = {str(item.get("name")) for item in current}
    for name, (color, description) in definitions.items():
        if name in existing:
            continue
        _gh(
            "label", "create", name, "--repo", REPOSITORY,
            "--color", color, "--description", description,
            runner=runner,
        )


def _claim_issue(
    issue: dict[str, Any],
    run_id: str,
    base_sha: str,
    *,
    conflict_resolution: dict[str, Any] | None = None,
    runner: CommandRunner,
) -> None:
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
    if conflict_resolution and "needs-human-review" in labels:
        arguments.extend(["--remove-label", "needs-human-review"])
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


@contextmanager
def _run_state_lock() -> Any:
    """Serialize local slot reservations across independent intake processes."""

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with RUN_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _process_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _active_run_states() -> list[dict[str, Any]]:
    """Return live reserved/reviewing runs and mark exited owners stale."""

    if not RUN_ROOT.exists():
        return []
    active: list[dict[str, Any]] = []
    for path in sorted(RUN_ROOT.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") not in ACTIVE_RUN_STATUSES:
            continue
        if _process_alive(payload.get("process_pid")):
            active.append(payload)
            continue
        payload["status"] = "stale"
        payload["stale_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return active


def _reserve_run(
    *,
    run_id: str,
    issue_number: int,
    base_sha: str,
    process_pid: int | None = None,
) -> None:
    """Reserve one of three local slots and enforce one active run per Issue."""

    with _run_state_lock():
        active = _active_run_states()
        duplicate = next(
            (item for item in active if int(item.get("issue_number", -1)) == issue_number),
            None,
        )
        if duplicate is not None:
            raise LocalIntakeError(
                f"issue #{issue_number} already has active local run {duplicate.get('run_id')}"
            )
        if len(active) >= MAX_PARALLEL_RUNS:
            identifiers = ", ".join(str(item.get("run_id")) for item in active)
            raise LocalIntakeError(
                f"local intake concurrency limit is {MAX_PARALLEL_RUNS}; active runs: {identifiers}"
            )
        _save_state(run_id, {
            "run_id": run_id,
            "issue_number": issue_number,
            "base_sha": base_sha,
            "status": "reserved",
            "process_pid": process_pid or os.getpid(),
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        })


def _update_state(run_id: str, **changes: Any) -> None:
    with _run_state_lock():
        payload = _load_state(run_id)
        payload.update(changes)
        payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _save_state(run_id, payload)


def _validate_generated_output(*, runner: CommandRunner) -> None:
    if not (ROOT / "node_modules").exists():
        _run(["pnpm", "install", "--frozen-lockfile", "--prefer-offline"], runner=runner)
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
    current_branch = _git("branch", "--show-current", runner=runner)
    if current_branch == "main":
        _git("switch", "-c", branch, runner=runner)
    elif current_branch != branch:
        raise LocalIntakeError(
            f"prepared worktree branch is {current_branch}, expected {branch}"
        )
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
    prepared_worktree: bool = False,
    runner: CommandRunner = subprocess.run,
) -> str:
    selected_run_id = run_id or str(uuid.uuid4())
    preflight = preflight_issue(
        issue_number,
        runner=runner,
        require_clean_main=not prepared_worktree,
    )
    if preflight.existing_pr_url:
        raise LocalIntakeError(f"an intake PR already exists: {preflight.existing_pr_url}")
    if preflight.golden_status != "current":
        raise LocalIntakeError(preflight.golden_status)
    issue = _issue(issue_number, runner=runner)
    conflict_resolution = _owner_conflict_resolution(issue)
    _reserve_run(
        run_id=selected_run_id,
        issue_number=issue_number,
        base_sha=preflight.base_sha,
    )
    try:
        _claim_issue(
            issue,
            selected_run_id,
            preflight.base_sha,
            conflict_resolution=conflict_resolution,
            runner=runner,
        )
        _update_state(selected_run_id, status="reviewing")
        records, source, result = process_issue(
            issue["body"],
            discovered="paper-candidate" in _issue_labels(issue),
            extractor_model=DEFAULT_MODEL,
            verifier_model=DEFAULT_MODEL,
            write=True,
            local_run_id=selected_run_id,
            owner_conflict_resolution=conflict_resolution,
        )
        _update_state(
            selected_run_id,
            status="reviewed",
            source_sha256=source.content_sha256,
            extractor_thread_id=result.extractor_thread_id,
            verifier_thread_id=result.verifier_thread_id,
            codex_cli_version=result.codex_cli_version,
        )
        _update_state(selected_run_id, status="validating")
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
        if conflict_resolution:
            summary += (
                "\nOwner-approved conflict handling: preserve the independently supported "
                f"root total `{conflict_resolution['benchmark_total']}`; exclude all conflicted "
                "benchmark subcounts and publish the inventory caveat.\n"
            )
            if conflict_resolution.get("exclude_creator_evaluation"):
                summary += (
                    "The creator-paper evaluation is published only as a partial relationship: "
                    "conflicted version, scope, protocol, metric, and result claims are excluded "
                    "pending manual reconciliation.\n"
                )
        _update_state(selected_run_id, status="publishing")
        pr_url = _publish_records(
            issue=issue,
            work_id=work_id,
            run_id=selected_run_id,
            summary=summary,
            runner=runner,
        )
        _mark_issue_success(issue_number, pr_url, runner=runner)
        _update_state(
            selected_run_id,
            status="pr-open",
            pr_url=pr_url,
            source_sha256=source.content_sha256,
            extractor_thread_id=result.extractor_thread_id,
            verifier_thread_id=result.verifier_thread_id,
            codex_cli_version=result.codex_cli_version,
        )
        return pr_url
    except Exception:
        try:
            _update_state(selected_run_id, status="failed")
        except Exception:
            pass
        raise


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
        if name == "run":
            command.add_argument("--run-id", help=argparse.SUPPRESS)
            command.add_argument(
                "--prepared-worktree",
                action="store_true",
                help=argparse.SUPPRESS,
            )
    batch = subparsers.add_parser("batch")
    batch.add_argument("--issue", type=int, action="append", required=True)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    subparsers.add_parser("golden")
    status = subparsers.add_parser("status")
    status.add_argument("--run-id")
    args = parser.parse_args()

    issue_number: int | None = None
    selected_run_id: str | None = None
    try:
        if args.command == "status":
            print(json.dumps(
                heartbeat_status(run_id=args.run_id),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ))
            return 0
        if args.command == "golden":
            receipt = run_golden(output=GOLDEN_RECEIPT)
            print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "batch":
            result = run_batch(args.issue)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 1 if result["failed"] else 0
        if args.command == "resume":
            state = _load_state(args.run_id)
            issue_number = int(state["issue_number"])
            selected_run_id = args.run_id
            if state.get("status") == "pr-open" and state.get("pr_url"):
                print(state["pr_url"])
                return 0
            print(run_issue(issue_number, run_id=args.run_id))
            return 0
        issue_number = _resolve_issue_argument(args, runner=subprocess.run)
        if args.command == "preflight":
            print(json.dumps(asdict(preflight_issue(issue_number)), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        selected_run_id = args.run_id or str(uuid.uuid4())
        print(run_issue(
            issue_number,
            run_id=selected_run_id,
            prepared_worktree=args.prepared_worktree,
        ))
        return 0
    except Exception as error:
        if issue_number is not None and args.command in {"run", "resume"}:
            state_exists = selected_run_id is not None and _state_path(selected_run_id).exists()
            if state_exists:
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
