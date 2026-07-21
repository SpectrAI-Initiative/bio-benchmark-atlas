# Contributing to BioBench Atlas

Thank you for improving the evidence layer around life-science evaluation. Contributions are reviewed for provenance and protocol completeness before coverage volume.

## v1 source policy

Production records may come from:

1. a benchmark creator's paper or formal technical report; or
2. an official model provider's system card, release page, or research page.

Independent reproductions have a reserved schema class but are not publishable in v1. Ordinary blogs, secondary leaderboards, and unsourced score collections are out of scope.

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

## v1.1 family audit rules

- One benchmark family is audited per pull request; formal child tracks are audited with their parent family.
- Use permanent resource, evidence, and version IDs. Audited evidence uses an RFC 6901 field path such as `/task_counts/total` and a typed locator.
- `audited` means there are no unresolved field claims. Use `audited-with-caveats` plus `field_status` for every provisional or conflicted value.
- A repository or dataset resource must be pinned to a commit, tag, release, version, or immutable snapshot.
- Every published numeric result in an audited family needs `status`, `confidence`, and one or more run evidence IDs.
- Production validation rejects undeclared legacy records. A deliberately deferred root family must have a dated, reasoned entry in `registry/meta.yaml` under `audit_exemptions`; remove that entry in the same pull request that completes its audit.
- Attach the completed field audit table from `docs/audit-playbook.md` to the pull request.

## Rights and safety

Do not add third-party questions, restricted benchmark artifacts, model outputs, or paper snapshots unless redistribution is explicitly permitted. Metadata contributions are CC BY 4.0; code contributions are Apache-2.0. Upstream assets retain their licenses.

## Review and publication

All changes use pull requests. The `@SpectrAI-Initiative/biobench-maintainers` team reviews registry, schema, taxonomy, scripts, and workflow changes. A non-author approval is required. Draft entities are available to PR previews but only verified entities enter production exports. Deprecated records are retained with history and successor links rather than deleted.
