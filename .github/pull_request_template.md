## What changed

<!-- Describe new or modified registry entities and website behavior. -->

## Primary / official sources

<!-- Link creator publications or official model-provider sources and include locators. -->

## Field audit (family PRs)

<!-- Paste the completed audit table from docs/audit-playbook.md. Include before/after values and every provisional/conflicted claim. -->

- Audit status: <!-- legacy / audited / audited-with-caveats -->
- Benchmark version(s):
- Unreported fields:
- Provisional/conflicted paths:

## Evaluation completeness

- [ ] Scope is explicitly `full`, `subset`, `track`, or `unknown`.
- [ ] Realized `n` and benchmark version are recorded when reported.
- [ ] Metrics, aggregation, tools, repeats, grader, and model identity are recorded.
- [ ] Every unknown field uses `null` plus `reporting_status: not_reported`.
- [ ] No third-party task, restricted artifact, or paper text is mirrored.
- [ ] Every audited critical field uses structured evidence with a resolvable `/path`.
- [ ] Provisional/conflicted values have adjacent `field_status` warnings.
- [ ] Numeric results have status, confidence, and evidence IDs.

## Validation

- [ ] `pnpm registry:validate`
- [ ] `pnpm registry:build`
- [ ] `pnpm test:data`
- [ ] `pnpm site:build`
