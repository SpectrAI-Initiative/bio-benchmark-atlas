#!/usr/bin/env python3
"""Evaluate an exact-head-SHA paper-intake approval comment.

This helper consumes GitHub API data only. It never executes comment content and
does not read pull-request files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
OWNER = "wang422003"


def _timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def has_current_owner_approval(
    *,
    head_sha: str,
    commit_time: str,
    comments: list[dict[str, Any]],
    owner: str = OWNER,
) -> bool:
    """Return true only for an owner's exact command posted after the head commit."""

    if not FULL_SHA.fullmatch(head_sha):
        return False
    command = f"/approve-paper-intake {head_sha}"
    committed_at = _timestamp(commit_time)
    for comment in comments:
        user = comment.get("user") or {}
        if user.get("login") != owner or comment.get("body") != command:
            continue
        created_at = comment.get("created_at")
        if created_at and _timestamp(created_at) > committed_at:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--commit-time", required=True)
    parser.add_argument("--comments-file", type=Path, required=True)
    args = parser.parse_args()
    comments = json.loads(args.comments_file.read_text(encoding="utf-8"))
    if not isinstance(comments, list):
        raise SystemExit("comments payload must be a JSON array")
    return 0 if has_current_owner_approval(
        head_sha=args.head_sha,
        commit_time=args.commit_time,
        comments=comments,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
