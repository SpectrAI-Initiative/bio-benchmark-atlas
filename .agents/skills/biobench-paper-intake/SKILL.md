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
3. Start from a clean, current `main`. For PDF sources, ensure Poppler
   `pdftoppm` is installed so visual labels can be reviewed. Run a preflight:

   ```bash
   python3 scripts/local_paper_intake.py preflight --issue <number>
   ```

   For a direct URL, use `--url <paper-url>`. The command must create or reuse a GitHub issue before production.
4. Stop if source rights, full text, duplicate state, local authentication, or the local golden receipt is unresolved. Do not bypass the gate or silently change models.
   PDFs over 150 pages may proceed only when the owner-selected Issue field names
   explicit physical page ranges totaling at most 40 pages. The workflow hashes the
   complete PDF, then gives both local Codex passes only page-marked text and images
   from those selected pages. Without that bounded focus, the 150-page stop remains.
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
- `status`: inspect the current stage heartbeat and detect a stale or exited local run.

Long extractor and verifier stages write privacy-safe liveness metadata every
60 seconds to `~/.codex/biobench-atlas/heartbeat.json`. The heartbeat contains
only the run label, stage, process ID, timestamps, elapsed time, and terminal
status. It must never contain paper text, source paths, claims, excerpts, or model
output.

## Stop conditions

- Stop with `needs-human-review` for source conflicts, ambiguous benchmark identity/version/count, missing creator evidence, unparseable sources, refusals, or invalid structured output.
- A count conflict may continue only after `wang422003` posts the exact Issue comment
  `/resolve-paper-conflict benchmark-total=<N> exclude=benchmark-subcounts`.
  The approved root total must still be independently supported with high
  confidence in both passes. The generator omits all conflicted benchmark and
  scientific-task subcounts, records a machine-readable conflicted
  `field_status`, and marks the benchmark `audited-with-caveats`. This command
  cannot override benchmark identity/version, model, protocol, metric, or result
  conflicts.
- If the same creator paper also contains an evaluation whose version, scope,
  settings, metrics, or results conflict, the owner may instead post
  `/resolve-paper-conflict benchmark-total=<N> exclude=benchmark-subcounts,creator-evaluation`.
  This does not resolve those claims: it removes all evaluation settings and
  outcomes and publishes only the independently verified relation (plus exact
  model identities, when supported) as a partial `BenchmarkUse`. Creation
  identity/version and relation conflicts remain blocking.
- Use `intake-failed` only for local CLI, network, or temporary technical failures.
- Never treat a failed extraction as `not_reported`.
- Never retry by switching to a different model or remote API.
- Never merge automatically.
