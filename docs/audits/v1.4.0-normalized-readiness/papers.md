# v1.4.0 normalized-readiness audit

Audit date: 2026-08-13

Registry base: `main` at `5bea4aa`

## Decision summary

Seven target papers have been merged for the v1.4.0 queue. Their first-pass intake records contributed 18 partial evaluation relationships. A dedicated BioSecBench-Surveillance re-audit has since added an official, commit-pinned result-snapshot Work with three normalized `EvaluationRun` entities and 16 machine-readable results, while preserving the preprint's three partial relationships.

The original release gate was internally unreachable as written:

- 7 of the intended 10 papers are already partial.
- The gate required at least 6 normalized papers and no more than 4 partial papers.
- With only 3 nominal paper slots left, the maximum possible number of normalized papers is 3 unless existing records are re-audited and upgraded.
- Even if all 3 remaining papers normalize, the existing 7 partial papers already exceed the maximum of 4.

The maintainer has adopted the following replacement release policy:

- v1.4.0 must contain at least 10 target papers with at least one actual benchmark use; related-work-only citations do not count;
- at least 6 target paper or benchmark-family entries must be backed by one or more normalized `EvaluationRun` entities, which may be supported by a versioned official result Work associated with the target paper;
- evidence-supported partial relationships are retained without a numeric cap and remain excluded from comparability charts;
- the original 4/2/2/2 subject balance remains a selection goal for the first 10 target papers, not a reason to weaken evidence requirements;
- a paper can contribute to the 10-paper content minimum even when its use remains partial, but it cannot contribute to the six-normalized minimum.

Under this policy, BioSecBench-Surveillance is the first completed normalization target. The release remains blocked by the remaining content and normalized-run minimums, rather than by the number of honest partial records.

## Readiness matrix

| Work | Area | Current evaluation uses | Normalized runs | Readiness verdict | Main blocker or opportunity | Next action |
|---|---|---:|---:|---|---|---|
| PPB-Affinity | Protein | 1 partial | 0 | Not ready | The paper prints whole-dataset Pearson and Spearman values, but the evaluated split, realized test `n`, exact trained system version, repeats, and seed are not reported. The pinned repository contains data-preparation artifacts rather than the benchmark training/evaluation implementation. | Keep partial. Revisit only if the authors publish the evaluation split and runnable benchmark code. |
| BioSecBench-Surveillance | DNA/RNA | 3 preprint partial + 3 normalized release uses | 3 | Normalized | The independent repository re-audit pinned commit `8d53fd8`, separated the paper and result-snapshot Works, and admitted 16 supported results across Pi, Claude Code, and OpenAI Codex protocols. | Complete. Preserve the preprint's partial relationships; use only the normalized release runs for within-group result displays. |
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

1. Re-audit MoleculeNet auxiliary learning, beginning with Table 1 only. Stop if the MoleculeNet data snapshot or pretrained checkpoint cannot be pinned.
2. Select additional source-rich papers specifically for normalized runs until at least six target papers or benchmark families are backed by normalized runs.
3. Keep PPB-Affinity, Crafted experiments, multiobjective protein design, CRISPRn guide design, and differential transcript usage as partial until new primary artifacts resolve their blockers.
4. Do not count a paper toward the normalized target merely because it prints a score; all normalization fields above must be supported.

## Release gate

Under the adopted policy, v1.4.0 readiness is:

- merged target papers: 7;
- normalization targets completed: 1 (BioSecBench-Surveillance, via its official result-snapshot Work);
- remaining normalized minimum: 5;
- remaining paper-content minimum: 3;
- partial relationships are retained as evidence, not treated as a release-gate failure;
- release status: blocked until both the 10-paper minimum and six-normalized minimum are met.
