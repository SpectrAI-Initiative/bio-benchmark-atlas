---
name: biobench-paper-intake
description: Audit a life-science or chemistry paper into BioBench Atlas with two independent local Codex evidence passes and deterministic Registry generation. Use for a paper URL, DOI, preprint, GitHub paper-candidate or paper-intake issue, a request to check whether a paper's benchmarks are already covered, or a request to add its benchmark uses and evaluation settings.
---

# BioBench Paper Intake

Use the local workflow only after the repository owner explicitly selects a paper or issue. Keep discovery and production separate: candidates may be queued automatically, but only a local run may create Registry records.

Accepted invocations are `$biobench-paper-intake issue 44` and `$biobench-paper-intake https://doi.org/...`.

## Workflow

1. Read [evidence-policy.md](references/evidence-policy.md) before reviewing claims.
2. Read [registry-mapping.md](references/registry-mapping.md) before generating records.
3. Start from a clean, current `main`. Run a preflight:

   ```bash
   python3 scripts/local_paper_intake.py preflight --issue <number>
   ```

   For a direct URL, use `--url <paper-url>`. The command must create or reuse a GitHub issue before production.
4. Stop if source rights, full text, duplicate state, local authentication, or the local golden receipt is unresolved. Do not bypass the gate or silently change models.
5. Run the intake:

   ```bash
   python3 scripts/local_paper_intake.py run --issue <number>
   ```

   The command claims the issue, runs two fresh `codex exec` sessions, generates records, validates them, creates `paper-intake/<work-id>-<issue-number>`, and opens a Ready PR.
6. Inspect the diff and PR audit summary. Confirm that no paper, excerpt, transcript, draft JSON, verification JSON, or temporary file is tracked.
7. Wait for `validate` and `playwright`. The owner then comments:

   ```text
   /approve-paper-intake <full-current-head-sha>
   ```

   Any later push invalidates this approval.

## Commands

- `preflight --issue N`: inspect identity, source, duplicates, tools, and the golden gate without changing Registry.
- `preflight --url URL`: reuse or create the canonical intake issue, then inspect it.
- `run --issue N`: execute the local double pass, generate records, validate, commit, push, and open a PR.
- `resume --run-id ID`: restart from the stored issue reference; full text and model drafts are reacquired, never persisted.
- `golden`: run the local precision regression groups and save a sanitized receipt under `~/.codex/biobench-atlas/`.

## Stop conditions

- Stop with `needs-human-review` for source conflicts, ambiguous benchmark identity/version/count, missing creator evidence, unparseable sources, refusals, or invalid structured output.
- Use `intake-failed` only for local CLI, network, or temporary technical failures.
- Never treat a failed extraction as `not_reported`.
- Never retry by switching to a different model or remote API.
- Never merge automatically.
