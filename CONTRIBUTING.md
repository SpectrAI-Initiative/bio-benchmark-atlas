# Contributing to BioBench Atlas

Thank you for improving the evidence layer around life-science evaluation. Contributions are reviewed for provenance and protocol completeness before coverage volume.

## Source policy

Production records may come from:

1. a benchmark creator's paper or formal technical report;
2. an official model provider's system card, release page, or research page;
3. a peer-reviewed paper; or
4. a stable versioned arXiv, bioRxiv, medRxiv, or ChemRxiv preprint.

Independent evaluations use `source_class: independent_reproduction`. Ordinary blogs, secondary leaderboards, news summaries, and unsourced score collections are out of scope.

## Paper intake workflow

Use the `Review a paper` Issue Form for a DOI, preprint, or publisher link plus a legal open or submitter-authorized full-text source. External submissions and weekly discovery both remain `paper-candidate` issues until `wang422003` explicitly selects one in local Codex. Candidate issues do not enter Registry or Pages.

Start a selected paper with `$biobench-paper-intake issue <number>` or `$biobench-paper-intake <URL>`. The local orchestrator uses two fresh, ephemeral, read-only `codex exec` sessions with schema-constrained output. It uses the owner's existing Codex login and never reads a repository API key. The extractor emits claims, not YAML. The verifier receives the original source and draft claims in a separate session and independently rechecks every locator. Only high-confidence agreement reaches deterministic ID/YAML generation; unclear versions, model identities, or subset sizes stay partial, and unlabeled graph values are discarded.

One paper normally uses one Ready PR. If the paper introduces an unregistered benchmark, creator and official repository/dataset evidence must be established in the same PR; otherwise the generator stops for human review. Local Codex cannot merge. `paper-owner-gate` requires an exact comment from `wang422003` after the latest push:

```text
/approve-paper-intake <full-current-head-sha>
```

Any later push changes the head SHA and requires a new approval comment.

Repository-owner setup and recovery procedures are documented in [`docs/paper-intake-operations.md`](docs/paper-intake-operations.md).

## Contribution workflow

1. Open the matching structured issue form or fork the repository.
2. Add or update human-maintained YAML under `registry/`.
3. Include an evidence locator for every critical count, protocol setting, metric, and result.
4. Run the commands below.
5. Open a pull request and complete the checklist.

```bash
python3 -m pip install -e '.[test]'
pnpm install
pnpm registry:validate
pnpm registry:build
pnpm test:data
pnpm site:build
pnpm --dir site test
```

## Evaluation-run rules

- Use `full` only when realized `n` matches the total for the stated benchmark version.
- A subset needs a stable subset ID, filter, and realized `n`.
- Use `unknown` when the official source does not establish scope.
- Split runs when prompt, tools, internet, code execution, budget, reasoning mode, grader, repeats, or subset changes.
- Preserve the source metric name, unit, direction, aggregation, threshold, and tolerance.
- Never merge similar model names unless the primary source establishes identity.
- Encode missing values as `null` with `reporting_status: not_reported`.
- Use a partial `BenchmarkUse` when benchmark usage is certain but realized scope or metrics are not sufficiently reported for an EvaluationRun.
- Mark third-party results quoted by a Work as `external-result-summary`; never count them as the Work author's own rerun.
- Store labeled improvements as delta metrics with an explicit baseline model. Do not infer absolute values from plot geometry.

## v1.1 family audit rules

- One benchmark family is audited per pull request; formal child tracks are audited with their parent family.
- Use permanent resource, evidence, and version IDs. Audited evidence uses an RFC 6901 field path such as `/task_counts/total` and a typed locator.
- `audited` means there are no unresolved field claims. Use `audited-with-caveats` plus `field_status` for every provisional or conflicted value.
- A repository or dataset resource must be pinned to a commit, tag, release, version, or immutable snapshot.
- Every published numeric result in an audited family needs `status`, `confidence`, and one or more run evidence IDs.
- Production validation rejects undeclared legacy records. A deliberately deferred root family must have a dated, reasoned entry in `registry/meta.yaml` under `audit_exemptions`; remove that entry in the same pull request that completes its audit.
- Attach the completed field audit table from `docs/audit-playbook.md` to the pull request.

## Scientific Task mapping rules

- Maintain task mappings in the curation-area files under `registry/scientific_task_classifications/`; the loader normalizes them into each public Benchmark object.
- Select the most specific task supported by a creator source, formal track, or locatable official artifact. Do not infer a task from the benchmark name or a potential downstream use.
- Keep Domain, Capability, Modality, task format, and Scientific Task separate. In particular, fitness prediction is not automatically protein design.
- Use `partial` when a mixed suite has no exhaustive official task taxonomy. A `complete` mapping must cover the formal task inventory.
- Preserve the original count unit and basis. Never add questions, examples, assays, targets, systems, tracks, and problems together.
- A reported count needs a value; an unreported count must be `null`. Explicit exclusion is `not-in-scope` with a reported zero.
- Low-confidence mappings require `field_status` and are excluded from coverage aggregation.
- Zero-coverage taxonomy terms are intentional registry gap signals and should not be deleted merely because no current benchmark maps to them.

## Rights and safety

Do not add third-party questions, restricted benchmark artifacts, model outputs, or paper snapshots unless redistribution is explicitly permitted. Local source retrieval is limited to 45 MiB and 150 PDF pages, records SHA256/content type/retrieval time, and deletes source files and structured drafts from the ignored temporary directory during cleanup. Metadata contributions are CC BY 4.0; code contributions are Apache-2.0. Upstream assets retain their licenses.

## Review and publication

All production changes use pull requests. Ordinary PRs follow the repository's current owner-managed rules. Every `paper-intake/*` PR additionally requires `validate`, `playwright`, and an exact current-head-SHA approval comment from `wang422003` through `paper-owner-gate`. Local Codex never auto-merges. Candidate issues, source documents, short verification excerpts, session transcripts, structured drafts, and rejected claims never enter production exports. Deprecated records are retained with history and successor links rather than deleted.
