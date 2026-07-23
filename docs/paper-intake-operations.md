# Local paper intake operations

GitHub Actions discovers candidate papers only. Production intake starts only after `wang422003` explicitly selects an Issue or URL in local Codex. No GitHub App or repository model API credential is required.

## 1. Local prerequisites

The owner machine needs:

- a clean clone of `SpectrAI-Initiative/bio-benchmark-atlas`;
- `gh` authenticated as `wang422003`;
- Codex CLI authenticated through the existing Codex/ChatGPT login;
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
python scripts/local_paper_intake.py resume --run-id <id>
```

Preflight checks local Git, `gh`, Codex CLI, source rights, MIME, the 45 MiB / 150-page limits, duplicate Work/Issue/branch/PR records, a current local golden receipt, and exact synchronization between `main` and `origin/main`. It writes no Registry data.

At run start the orchestrator labels the Issue `local-intake-in-progress` and posts a claim comment containing a random local run ID, base SHA, and timestamp. A second active run for the same Issue stops. An existing branch or PR is resumed rather than duplicated.

## 3. Local double pass

The orchestrator launches two separate, ephemeral `codex exec` sessions:

1. an extractor with high reasoning and the `PaperEvidenceDraft` output schema;
2. an independent verifier with max reasoning and the `PaperEvidenceVerification` output schema.

Both use a read-only sandbox, ignore repository-specific user configuration, receive no network tools, and treat paper content as untrusted data. The sessions have different thread IDs. The verifier receives the original source, Registry context, and extractor claims, but not the extractor conversation.

Only claims supported with high confidence in both passes can reach deterministic generation. Unsupported, conflicted, or not-verifiable claims are withheld. Unknown benchmark version, model identity, or subset size produces a partial `BenchmarkUse`; it cannot be upgraded by inference.

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

The receipt is stored at `~/.codex/biobench-atlas/golden.json`. It contains only the date, prompt/schema hash, requested model, Codex CLI version, and pass/fail results. Production requires a successful receipt no older than 35 days, an identical prompt/schema/model hash, and the same Codex CLI major version.

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

## 6. Recovery

- `needs-human-review`: source rights, identity, version, creator evidence, or a critical claim is unresolved. Correct the source or make the decision explicitly; do not convert the problem to `not_reported`.
- `intake-failed`: local CLI, network retrieval, or another transient technical step failed. Fix the technical cause before resuming.
- stale candidate: discovery may close an unselected candidate after 60 days. Reopen it before selecting it locally.
- existing run/branch/PR: use `resume --run-id`, never start a second intake.

When the PR merges, close the Issue and remove in-progress labels. Candidate Issues and local working material remain outside the Registry.
