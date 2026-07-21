# Benchmark family audit playbook

This playbook is the required checklist for each v1.1 family pull request. A family PR updates the benchmark, formal child tracks, creator or provider works, evaluation runs, exact models, taxonomy, regression tests, and site presentation together.

## Fixed sequence

1. LifeSciBench
2. ProteinGym
3. CASP
4. CAMEO
5. FLIP
6. ProteinLMBench
7. Biology-Instructions
8. LAB-Bench and qualifying formal tracks
9. GeneBench-Pro
10. BioMysteryBench
11. CompBioBench
12. BixBench
13. BLADE
14. SciGym
15. VirBench

## Source inventory

Use only a benchmark creator's versioned release/dataset, final paper or formal report, commit-pinned official repository, living official website, or preprint—in that precedence order. Official model-provider system cards, model release pages, and research pages may support provider evaluations. Secondary papers and blogs are discovery aids only.

## Field audit table

Copy this table into the pull request and add rows as needed.

| Registry path | Before | After | Status | Evidence ID | Locator | Notes |
|---|---|---|---|---|---|---|
| `/name` |  |  | verified |  |  |  |
| `/organizations` |  |  | verified |  |  |  |
| `/release_date` |  |  | verified |  |  |  |
| `/latest_version` |  |  | verified |  |  |  |
| `/kind` |  |  | verified |  |  |  |
| `/domains` |  |  | verified |  |  |  |
| `/capabilities` |  |  | verified |  |  |  |
| `/modalities` |  |  | verified |  |  |  |
| `/task_counts/total` |  |  | verified |  |  |  |
| `/task_counts/basis` |  |  | verified |  |  |  |
| `/task_counts/subsets` |  |  | verified |  |  |  |
| `/access/level` |  |  | verified |  |  |  |
| `/access/license` |  |  | verified |  |  |  |
| `/resources` |  |  | verified |  |  |  |

Allowed status values in this review table are `verified`, `provisional`, `conflicted`, `not_reported`, and `not_applicable`. Only provisional and conflicted claims are copied into registry `field_status`.

## Evaluation closure

For each linked official evaluation, verify the work, exact model identity, benchmark version, scope, realized `n`, metrics and aggregation, prompt/shots/turns, browser/internet/database/code/container tools, token/time/cost budget, repeats/seeds, grader/human review, statistical reporting, contamination statement, and result locators. Split a run whenever any comparability-critical setting differs.

## Required checks

```bash
pnpm registry:validate
pnpm registry:build
pnpm test:data
pnpm site:build
pnpm --dir site test
```

After merge, verify the canonical Pages record and JSON/CSV exports. The weekly source monitor reports moves, fingerprint drift, and three consecutive failures as issues; it never changes registry facts.
