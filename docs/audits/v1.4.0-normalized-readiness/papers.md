# v1.4.0 normalized-readiness audit

Audit date: 2026-08-11

Registry base: `main` at `28bb36c117f195976af1574d2c75c213b3beb302`

## Decision summary

The seven paper-intake works already merged for the v1.4.0 queue currently contribute 18 evaluation relationships, all recorded as `partial` `BenchmarkUse` entities. None links to an `EvaluationRun`.

This makes the original release gate internally unreachable as written:

- 7 of the intended 10 papers are already partial.
- The gate requires at least 6 normalized papers and no more than 4 partial papers.
- With only 3 nominal paper slots left, the maximum possible number of normalized papers is 3 unless existing records are re-audited and upgraded.
- Even if all 3 remaining papers normalize, the existing 7 partial papers already exceed the maximum of 4.

The v1.4.0 release should therefore remain blocked until maintainers explicitly revise the release policy. The recommended policy is to treat 10 papers as a minimum rather than an exact total, retain all evidence-supported partial relationships, and require at least 6 normalized papers without imposing a retroactive maximum on partial papers.

## Readiness matrix

| Work | Area | Current evaluation uses | Normalized runs | Readiness verdict | Main blocker or opportunity | Next action |
|---|---|---:|---:|---|---|---|
| PPB-Affinity | Protein | 1 partial | 0 | Not ready | The paper prints whole-dataset Pearson and Spearman values, but the evaluated split, realized test `n`, exact trained system version, repeats, and seed are not reported. The pinned repository contains data-preparation artifacts rather than the benchmark training/evaluation implementation. | Keep partial. Revisit only if the authors publish the evaluation split and runnable benchmark code. |
| BioSecBench-Surveillance | DNA/RNA | 3 partial | 0 | Ready for dedicated re-audit | The paper and pinned official repository jointly provide a 100-evaluation snapshot, 16 exact model–harness labels, three trials per evaluation, execution resources, internet access, deterministic grading, endpoint pass-rate aggregation, and a machine-readable result table with per-configuration gradable `n` and confidence intervals. | Run a new local double-pass audit in a single-paper PR and normalize each materially distinct model–harness configuration that passes the claim gate. |
| Crafted experiments | Omics/cell | 1 partial | 0 | Blocked by conflicts | The 24 crafted experiments and metric families are supported, but model/repeat claims conflict, software revisions and seeds are absent, and the principal result values are not numerically labelled in Figure 4. | Keep partial. Seek an author-issued versioned result table or executable snapshot before re-audit. |
| Multiobjective protein sequence design | Protein | 3 partial | 0 | Not ready | RfaH, PapD, and CaM tasks and objective definitions are supported, but no formal benchmark version or realized evaluation `n` is reported and headline numeric outcomes are primarily shown in unlabelled plots. | Keep partial. Do not digitize plots. Revisit only after a versioned result artifact is available. |
| MoleculeNet auxiliary learning | Small molecule | 3 partial | 0 | Targeted re-audit candidate | The paper provides eight named MoleculeNet datasets, scaffold splitting, detailed training settings, ten seeds for Table 1, and tabulated ROC-AUC means and standard deviations. The evaluated MoleculeNet data revision and checkpoint revisions remain unpinned; repeat reporting is incomplete for Tables 2 and 3. | Audit Table 1 first against a pinned GraphTA commit and source checkpoint artifacts. Normalize only the configurations whose dataset and checkpoint provenance can be fixed. |
| CRISPRn guide-RNA design | DNA/RNA | 2 partial | 0 | Not ready | The two libraries and metric families are supported, but the exact evaluated algorithm versions, realized guide counts for each comparison, seeds/uncertainty, and reusable numeric result table are not available; a key metric claim is conflicted. | Keep partial. Revisit if the Zenodo deposit gains versioned per-guide predictions and evaluation outputs. |
| Differential transcript usage | Omics/cell | 5 partial | 0 | Blocked by source conflicts | The work evaluates multiple simulation scenarios rather than one uniform scope. Scenario `n`, seeds, software revisions, and result values cannot be normalized consistently, and the source contains unresolved setting/result conflicts. | Keep the owner-approved provisional suite and partial uses. Do not create a synthetic aggregate run. |

The machine-readable companion is [papers.csv](./papers.csv). Source checks and the normalization criteria used here are recorded in [search-notes.md](./search-notes.md).

## Normalization contract used in this audit

An existing partial relationship is considered eligible for a dedicated normalization PR only when the paper and its official artifacts can independently support all of the following:

1. work version and benchmark snapshot/version;
2. full, track, or subset scope and the realized `n` in a stated unit;
3. exact model or system label and the harness when it changes behavior;
4. prompt/tool/internet/container/budget settings, with unreported optional fields represented explicitly;
5. repeats, grader, metric definition, aggregation, and uncertainty where reported;
6. printed or machine-readable numeric results, never values estimated from plot geometry;
7. locators for every critical setting, metric, and result.

This audit does not upgrade Registry records. Any upgrade must be performed as a separate one-paper PR using the local extractor/verifier workflow so that the new claims receive independent double-pass verification and owner approval of the final head SHA.

## Recommended execution order

1. Re-audit BioSecBench-Surveillance. Its pinned `results/config_results.csv` resolves several gaps in the first intake and makes it the strongest normalization candidate.
2. Re-audit MoleculeNet auxiliary learning, beginning with Table 1 only. Stop if the MoleculeNet data snapshot or pretrained checkpoint cannot be pinned.
3. Replace the exact-ten-paper constraint with a ten-paper minimum and select additional source-rich papers specifically for normalized runs.
4. Keep PPB-Affinity, Crafted experiments, multiobjective protein design, CRISPRn guide design, and differential transcript usage as partial until new primary artifacts resolve their blockers.
5. Do not count a paper toward the normalized target merely because it prints a score; all normalization fields above must be supported.

## Release gate

Until the policy is updated and the normalization work is complete, v1.4.0 readiness is:

- merged target papers: 7;
- normalized papers among those 7: 0;
- partial papers among those 7: 7;
- partial evaluation relationships: 18;
- release status: blocked by an inconsistent acceptance target and insufficient normalized evaluations.
