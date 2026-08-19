# Local paper intake operations

GitHub Actions discovers candidate papers only. Production intake starts only after `wang422003` explicitly selects an Issue or URL in local Codex. No GitHub App or repository model API credential is required.

## 1. Local prerequisites

The owner machine needs:

- a clean clone of `SpectrAI-Initiative/bio-benchmark-atlas`;
- `gh` authenticated as `wang422003`;
- Codex CLI authenticated through the existing Codex/ChatGPT login;
- Poppler `pdftoppm`, used to create temporary physical-page images so both
  independent sessions can inspect labels that are absent from a PDF text layer;
- Python 3.10+, Node 24, and pnpm.

Do not configure a repository model API key. The local orchestrator removes API-key and paper-model environment overrides before launching its child sessions.

Validate the shared repository Skill:

```bash
python3 /Users/aaronwang/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/biobench-paper-intake
```

## 2. Candidate and preflight

Weekly discovery and external submissions create `paper-candidate` Issues. Selecting a paper in Codex changes it to `ready-for-local-intake`; discovery never starts extraction.

The normal entry points are:

```text
$biobench-paper-intake issue 44
$biobench-paper-intake https://doi.org/...
```

The Skill runs one of:

```bash
python scripts/local_paper_intake.py preflight --issue 44
python scripts/local_paper_intake.py run --issue 44
python scripts/local_paper_intake.py run --url https://doi.org/...
python scripts/local_paper_intake.py batch --issue 46 --issue 42 --issue 55
python scripts/local_paper_intake.py resume --run-id <id>
```

Preflight checks local Git, `gh`, Codex CLI, Poppler for PDF sources, source
rights, MIME, the 45 MiB / 150-page limits, duplicate Work/Issue/branch/PR
records, a current local golden receipt, and exact synchronization between
`main` and `origin/main`. It writes no Registry data. PDF page images are created
only under the ignored intake temporary directory and are deleted with the other
private extraction artifacts.

Each extractor or verifier invocation has a 45-minute wall-clock limit. A stage
that exceeds it stops as a technical `intake-failed` condition; it is not treated
as evidence that the paper omitted a field, and the orchestrator does not launch
another expensive inference attempt automatically.

PDF sources are preprocessed locally into deterministic text packets with
`=== DOCUMENT PAGE N ===` markers tied to the original 1-based physical pages.
For ordinary PDFs the extractor receives all page text; an over-limit PDF still
receives only the explicitly owner-selected pages. Figure, table, and image-only
pages are rendered temporarily when visual evidence may not survive text
extraction. The downloaded PDF, prepared text, and page images remain private
temporary artifacts and are deleted in `finally`.

At run start the orchestrator labels the Issue `local-intake-in-progress` and posts a claim comment containing a random local run ID, base SHA, and timestamp. A second active run for the same Issue stops. An existing branch or PR is resumed rather than duplicated.

### Safe batch mode

`batch` accepts up to three owner-selected Issues. It creates a separate Git
worktree under `~/.codex/biobench-atlas/worktrees/` for each paper and runs the
independent double-pass pipelines concurrently. A file-locked local state store
enforces both invariants:

- one active run per Issue;
- no more than three active runs across the repository.

Heartbeats are stored separately under
`~/.codex/biobench-atlas/heartbeats/<run-id>.json`, so one verifier cannot
overwrite another run's liveness state. `status` lists all runs; use
`status --run-id <id>` for a single run.

Batching changes latency, not evidence policy. Each paper still has its own
source packet, extractor session, verifier session, deterministic generation,
branch, PR, CI, and exact-SHA owner approval. Do not place two papers that may
modify the same Registry entities in one batch. PRs are merged sequentially;
after the first merge, rebase the next PR on the latest `main` and obtain a new
owner approval for its new head SHA.

## 3. Local double pass

The orchestrator launches two separate, ephemeral `codex exec` sessions:

1. an extractor with high reasoning and the `PaperEvidenceDraft` output schema;
2. an independent verifier with max reasoning and the `PaperEvidenceVerification` output schema.

Both use a read-only sandbox, ignore repository-specific user configuration, receive no network tools, and treat paper content as untrusted data. The sessions have different thread IDs. The verifier receives the original source, Registry context, and extractor claims, but not the extractor conversation.

To reduce repeated document parsing without weakening claim independence, the
extractor reads the complete permitted page-anchored source, while the verifier
reads a deterministic packet containing every extractor-cited physical page,
page 1, and adjacent-page context (up to 60 pages). The verifier packet contains
PDF text extracted directly from the source, not an extractor summary. Only
cited page images are attached to the verifier. A citation outside an
owner-selected long-PDF range or a packet exceeding the safety bound stops
intake instead of silently dropping evidence.

Only claims supported with high confidence in both passes can reach deterministic generation. Unsupported, conflicted, or not-verifiable claims are withheld. Unknown benchmark version, model identity, or subset size produces a partial `BenchmarkUse`; it cannot be upgraded by inference.

Verifier disagreements are machine-classified. `extractor-error` means the
source is consistent but the first pass misread or over-interpreted it; the claim
is rejected without blocking the whole paper. `source-internal` means the source
itself is irreconcilable, and `cross-source` means authoritative artifacts
disagree without a versioned explanation. The latter two remain blocking. New
verifier output uses this structured taxonomy; the legacy free-text blocking list
is retained only for old local receipts and fixtures.

The deterministic PR summary includes a normalization-readiness line for every
evaluation. It reports `normalized-ready` or `partial-only` and lists exact
blockers such as an unregistered benchmark version, unknown scope, missing
realized `n`, unresolved model identity, missing metric, or missing numeric
result. This report is an audit aid, not evidence, and cannot promote a claim.

### Owner-reviewed count conflicts

When official sources independently support a root benchmark total but conflict
on a supposedly exhaustive subcount inventory, intake first stops with
`needs-human-review`. The repository owner may choose the narrow
“verified-total only” policy by posting this exact Issue comment:

```text
/resolve-paper-conflict benchmark-total=<positive-integer> exclude=benchmark-subcounts
```

Only a comment authored by `wang422003` is accepted. The command does not turn
the owner's preference into evidence. A verified root total still requires both
local passes to support it with high confidence. If the root-total claim itself
is conflicted, the extractor must locate the exact value with at least medium
confidence and the independent verifier must return high confidence with a
resolved locator. A medium extractor claim is retained only as machine-readable
`conflicted`, never as verified, and cannot support `scope: full`. The generator
then:

- retains the independently supported root total;
- omits all conflicted benchmark and scientific-task subcounts;
- records the omitted inventory as a machine-readable conflicted field;
- marks the benchmark `audited-with-caveats`.

The exception is intentionally limited to a newly created benchmark's counts. It
cannot override conflicts in benchmark identity or version, models, protocols,
metrics, or results. A fresh double-pass run and the normal exact-head-SHA PR
approval are still required.

If the creator paper's own evaluation also contains irreconcilable setting or
result claims, the owner may choose a stricter omission policy:

```text
/resolve-paper-conflict benchmark-total=<positive-integer> exclude=benchmark-subcounts,creator-evaluation
```

This command still cannot resolve or override evidence. It retains the supported
benchmark-creation record, omits conflicted subcounts, and reduces the creator
evaluation to a partial `BenchmarkUse`. Benchmark version, scope and realized
`n`, protocol, metrics, and numeric results are all withheld for later manual
reconciliation. Independently supported exact model identities may remain.
Creator-evaluation mentions are linked by their extracted benchmark-identity
claims rather than free-form display labels, so assay- or track-qualified labels
for the same benchmark are omitted without affecting evaluations of other
benchmarks in the paper.
Creation identity/version conflicts and relation conflicts always stop intake.

For a reusable scenario matrix, simulator, or rolling benchmark whose single
root-total claim is independently verified as `Not reported`, the owner may
instead omit only the conflicted creator evaluation:

```text
/resolve-paper-conflict benchmark-total=not-reported exclude=creator-evaluation
```

This command is accepted only when there is exactly one new benchmark, exactly
one high-confidence null root-total claim, and every conflict belongs to the
creator-paper evaluation. The benchmark creation record and supported null total
remain intact; evaluation version, scope, realized `n`, protocol, metrics, and
results are withheld and the relation is published as a partial `BenchmarkUse`.
Benchmark identity, creation version, creator source, official resource, license,
root-total, and relation conflicts remain non-overridable.

If the same intake has a source-located `kind=suite` claim that the extractor
rates `medium` while the independent verifier returns `supported`, `high`, and a
resolved locator, the owner may add this exact comment:

```text
/resolve-paper-metadata benchmark-kind=suite status=provisional
```

This command is valid only alongside an accepted `/resolve-paper-conflict`
command. It does not promote the mapping to verified: `/kind` is recorded in
`field_status` as provisional, displayed beside the value, exported in JSON/CSV,
and excluded from unqualified kind summaries. A different extracted kind, a low
confidence claim, a non-high verifier result, or an unresolved locator still
stops intake.

If the only missing field is the Atlas-controlled access label, the owner may
add this exact comment:

```text
/resolve-paper-metadata benchmark-access=fully-open status=provisional
```

This access-only resolution may accompany either the `not-reported` creator
evaluation policy or a verified positive root total whose conflict policy is
`exclude=benchmark-subcounts,creator-evaluation`. It never changes the root
total or restores excluded evaluation claims. Generation still requires the two
local passes to independently support source-located public task, artifact, and
grader/resource facts, and it rejects any extracted access value other than
`fully-open`. The resulting `/access/level` is machine-readable as provisional,
shown with a warning, and excluded from unqualified fully-open summaries.

Sources, short excerpts, transcripts, and structured drafts live only under the ignored `.paper-intake-tmp/` directory and are deleted in cleanup. They must never appear in Git diff, Actions artifacts, Pages, or a Release.

## 4. Local golden gate

Run:

```bash
python scripts/local_paper_intake.py golden
```

The gate checks:

- LifeSciBench 750 / 136 / 62 and no invented binding count;
- BioMysteryBench 99 / 76 / 23 and five repeats;
- distinct SpatialBench 146 and 159 versions;
- Anthropic × BixBench as a partial relationship without an invented score.

For a new benchmark, put creator-controlled GitHub repositories and Hugging Face
datasets in the Issue Form's `Official artifact` field. Local intake reads only
bounded public API metadata: identity, immutable revision, visibility/gating,
license, and up to 500 file paths. It does not treat an artifact inventory as
paper evidence for counts, protocols, or results. Unsupported hosts remain a
hard stop.

The receipt is stored at `~/.codex/biobench-atlas/golden.json`. It contains only
the date, prompt/schema/source-input-protocol hash, requested model, Codex CLI
version, and pass/fail results. Production requires a successful receipt no older
than 35 days, an identical prompt/schema/source-input/model hash, and the same
Codex CLI major version.

The golden runner checkpoints each completed regression case in
`~/.codex/biobench-atlas/golden-progress.json`. The checkpoint contains only
case names, the input hash, the Codex CLI version, official-source SHA256 values,
and timestamps—never claims, excerpts, or model output. A resumed run skips a
case only when its source fingerprints and all input versions still match. The
checkpoint is deleted after the final receipt is written.

For HTML sources, the checkpoint SHA256 is computed from the same deterministic
visible-text view used for review, so unrelated site scripts and build metadata
do not invalidate a scientific-content checkpoint. Registry provenance continues
to store the SHA256 of the complete original download.

## 5. PR and exact-SHA owner gate

One paper produces one Ready PR from:

```text
paper-intake/<work-id>-<issue-number>
```

The PR contains normalized Registry records and an audit summary, never the source or model drafts. After `validate` and `playwright` pass, retrieve the full current head SHA and comment:

```text
/approve-paper-intake <full-40-character-head-sha>
```

`paper-owner-gate` accepts only an exact comment by `wang422003` whose timestamp is later than the current head commit. Other users, abbreviated or stale SHAs, edited mismatches, and old comments fail. A new push changes the SHA and requires another comment. Auto-merge remains disabled.

Several Ready PRs may exist after a safe batch, but they are never bulk-merged.
Merge the first clean PR, rebase and revalidate the second, approve its current
full SHA, then continue to the third.

The `Validate` workflow keeps the existing required `validate` check but runs
three expensive paths concurrently: Registry tests, paper-intake tests, and the
Registry/Astro deterministic build. A final lightweight aggregate job fails
unless every shard succeeds. A newer push cancels obsolete validation for the
same PR. Full browser tests and post-merge Pages deployment remain independent
gates.

## 6. Recovery

- `needs-human-review`: source rights, identity, version, creator evidence, or a critical claim is unresolved. Correct the source or make the decision explicitly; do not convert the problem to `not_reported`.
- `intake-failed`: local CLI, network retrieval, or another transient technical step failed. Fix the technical cause before resuming.
- stale candidate: discovery may close an unselected candidate after 60 days. Reopen it before selecting it locally.
- existing run/branch/PR: use `resume --run-id`, never start a second intake.

When the PR merges, close the Issue and remove in-progress labels. Candidate Issues and local working material remain outside the Registry.
