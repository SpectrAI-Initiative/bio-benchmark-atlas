from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_paper_owner_gate import has_current_owner_approval  # noqa: E402


HEAD = "a" * 40
COMMIT = "2026-07-23T01:00:00Z"


def comment(
    body: str,
    *,
    user: str = "wang422003",
    created_at: str = "2026-07-23T01:01:00Z",
) -> dict[str, object]:
    return {
        "user": {"login": user},
        "body": body,
        "created_at": created_at,
    }


def test_exact_current_sha_owner_comment_passes() -> None:
    assert has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment(f"/approve-paper-intake {HEAD}")],
    )


def test_wrong_user_short_wrong_or_stale_sha_comments_fail() -> None:
    assert not has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment(f"/approve-paper-intake {HEAD}", user="someone-else")],
    )
    assert not has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment(f"/approve-paper-intake {HEAD[:7]}")],
    )
    assert not has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment("/approve-paper-intake " + "b" * 40)],
    )
    assert not has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment(f"/approve-paper-intake {HEAD}", created_at="2026-07-23T00:59:59Z")],
    )
    assert not has_current_owner_approval(
        head_sha=HEAD,
        commit_time=COMMIT,
        comments=[comment(f" /approve-paper-intake {HEAD}")],
    )
