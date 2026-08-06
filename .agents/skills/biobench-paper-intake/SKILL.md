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
   PDF review uses a deterministic page-anchored text companion. The extractor
   receives the complete permitted document plus selected figure/table images;
   the verifier receives complete text for cited pages and bounded adjacent-page
   context plus only the cited images. These packets are temporary source views,
   not summaries, and are deleted with the other local evidence artifacts.
   For two or three independent papers, use the safe batch entry:

   ```bash
   python3 scripts/local_paper_intake.py batch \
     --issue <number-1> --issue <number-2> --issue <number-3>
   ```

   The batch coordinator creates one worktree and branch per Issue, enforces one
   active run per Issue and at most three active runs across the repository, and
   executes the double passes concurrently. Each paper still creates a separate
   PR. Review, rebase when necessary, approve, and merge those PRs sequentially.
6. Inspect the diff and PR audit summary. Confirm that no paper, excerpt, transcript, draft JSON, verification JSON, or temporary file is tracked.
7. Wait for `validate` and `playwright`. Registry tests, paper-intake tests, and
   deterministic registry/site builds run as parallel `validate` shards while a
   final aggregate preserves the required check name. The owner then comments:

   ```text
   /approve-paper-intake <full-current-head-sha>
   ```

   Any later push invalidates this approval.

## Commands

- `preflight --issue N`: inspect identity, source, duplicates, tools, and the golden gate without changing Registry.
- `preflight --url URL`: reuse or create the canonical intake issue, then inspect it.
- `run --issue N`: execute the local double pass, generate records, validate, commit, push, and open a PR.
- `batch --issue N --issue M [--issue K]`: run up to three independent intakes concurrently in separate worktrees; never merge them automatically.
- `resume --run-id ID`: restart from the stored issue reference; full text and model drafts are reacquired, never persisted.
- `golden`: run the local precision regression groups and save a sanitized receipt under `~/.codex/biobench-atlas/`.
- `status`: inspect every current stage heartbeat; add `--run-id ID` for one run.

Long extractor and verifier stages write privacy-safe liveness metadata every
60 seconds to `~/.codex/biobench-atlas/heartbeats/<run-id>.json`. Each heartbeat contains
only the run label, stage, process ID, timestamps, elapsed time, and terminal
status. It must never contain paper text, source paths, claims, excerpts, or model
output.

## Stop conditions

- The verifier must classify disagreements as `extractor-error`,
  `source-internal`, or `cross-source`. An `extractor-error` rejects the affected
  claim but does not block an otherwise publishable partial use. Genuine
  `source-internal` and `cross-source` conflicts remain blocking.
- Stop with `needs-human-review` for genuine source conflicts, ambiguous benchmark identity/version/count, missing creator evidence, unparseable sources, refusals, or invalid structured output.
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
- When the independently verified root-total claim is correctly `Not reported`
  for a reusable scenario matrix, simulator, or rolling benchmark, and the only
  conflicts are inside the creator-paper evaluation, the owner may post
  `/resolve-paper-conflict benchmark-total=not-reported exclude=creator-evaluation`.
  This retains the supported null root total and benchmark metadata, publishes
  the evaluation only as a partial `BenchmarkUse`, and drops every evaluation
  setting and outcome. It cannot override benchmark identity, creation version,
  creator source, official resource, license, root-total, or relation conflicts.
- If that same intake has a source-located `kind=suite` claim with extractor
  confidence `medium` but verifier verdict `supported`, verifier confidence
  `high`, and a resolved locator, the owner may additionally post
  `/resolve-paper-metadata benchmark-kind=suite status=provisional`. The command
  is valid only together with an accepted conflict-resolution command. It
  preserves the source-backed value with a machine-readable `/kind` provisional
  warning and excludes it from unqualified kind summaries. It cannot change a
  different extracted value or authorize any other field.
- If the source instead leaves only the Atlas-controlled access label unstated,
  the owner may additionally post
  `/resolve-paper-metadata benchmark-access=fully-open status=provisional`.
  This command is valid only with the same accepted conflict-resolution command
  and exactly one verified official resource plus independent high/high,
  source-located descriptions of the public tasks, artifacts, and grader. It
  cannot override a different extracted access value. The generated
  `/access/level` carries a machine-readable provisional warning, appears with a
  warning badge, and is excluded from unqualified fully-open summary counts.
- Use `intake-failed` only for local CLI, network, or temporary technical failures.
- Never treat a failed extraction as `not_reported`.
- Never retry by switching to a different model or remote API.
- Never merge automatically.
- Do not run papers that may create or modify the same Benchmark, Work, or Model
  in the same batch. In particular, papers that both update MoleculeNet belong
  in consecutive batches.
