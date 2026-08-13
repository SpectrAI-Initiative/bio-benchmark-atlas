# Source notes for the v1.4.0 normalized-readiness audit

Access date: 2026-08-13

## Method

This was a targeted readiness audit, not a new paper extraction. It checked the already published Registry records against primary papers and official, versioned artifacts to determine whether a future single-paper re-audit could satisfy the Registry's `EvaluationRun` contract.

The review used DOI/PMCID/arXiv identity, paper text, labelled tables and figures, and official repository files. It did not estimate numbers from chart geometry. Full text, rendered pages, and working extracts were held only in a Git-ignored local directory and are not part of this report.

## Sources checked

| Work | Primary source | Official artifact checked | Relevant finding |
|---|---|---|---|
| PPB-Affinity | [Scientific Data article](https://doi.org/10.1038/s41597-024-03997-4), PMCID `PMC11615212` | [Data-preparation repository at `f1a1698`](https://github.com/Huatsing-Lau/PPB-Affinity-DataPrepWorkflow/tree/f1a1698ba868365af8a55807f8ae5b0d9653fa97) and [Zenodo record](https://zenodo.org/doi/10.5281/zenodo.11070823) | The article prints whole-dataset Pearson `0.701` and Spearman `0.691`, but the checked repository does not contain the model evaluation implementation or a pinned test split. |
| BioSecBench-Surveillance | [arXiv v1](https://arxiv.org/abs/2607.19262) | [Repository at `8d53fd8`](https://github.com/latchbio/biosecbench-surveillance/tree/8d53fd8517cc74202eb18b618e8b39b4ffaf0c87), especially [`results/config_results.csv`](https://github.com/latchbio/biosecbench-surveillance/blob/8d53fd8517cc74202eb18b618e8b39b4ffaf0c87/results/config_results.csv) | The paper states 100 evaluations, three trials, execution resources, internet access, deterministic grading, and aggregation. The pinned CSV gives exact model–harness strings, endpoint pass rates, confidence intervals, and gradable `n` for all 16 configurations. |
| Crafted experiments | [NAR Genomics and Bioinformatics article](https://doi.org/10.1093/nargab/lqaf023), PMCID `PMC11920870` | [Zenodo record](https://zenodo.org/records/13830885) | The 24 experiments are explicit, but existing double-pass review found conflicting model/repeat claims and the main comparison figure does not print reusable result values. |
| Multiobjective protein sequence design | [PLOS Computational Biology article](https://doi.org/10.1371/journal.pcbi.1011953), PMCID `PMC11265717` | [Repository at `b16a0ef`](https://github.com/luhong88/int_seq_des/tree/b16a0ef3d5c44f65714b1a6c51826f6b4bdaa998) | Task definitions and methods are clear, but no formal benchmark snapshot or realized evaluation `n` is published and the headline outcomes are plot-based. |
| MoleculeNet auxiliary learning | [Journal of Cheminformatics article](https://doi.org/10.1186/s13321-024-00880-7), PMCID `PMC11270959` | [GraphTA repository at `0eef8c9`](https://github.com/vishaldeyiiest/GraphTA/tree/0eef8c948bce040e27f01dc18f9fdbbbad8468a6) and [MoleculeNet creator paper](https://doi.org/10.1039/C7SC02664A) | Table 1 reports eight named datasets, scaffold splitting, ten seeds, and ROC-AUC mean/standard deviation. The paper links public checkpoints, but the evaluated MoleculeNet data revision and exact checkpoint revisions are not fixed in the current record. |
| CRISPRn guide-RNA design | [BMC Genomics article](https://doi.org/10.1186/s12864-025-11386-3), PMCID `PMC11863645` | [Zenodo record](https://zenodo.org/records/11164566) | Library construction is supported, but the existing intake could not establish versioned algorithm identities, per-comparison realized `n`, or a complete labelled numeric result artifact. |
| Differential transcript usage | [NAR Genomics and Bioinformatics article](https://doi.org/10.1093/nargab/lqaf117) | [Repository at `e411654`](https://github.com/yollct/diffIsoUsage_benchmark/tree/e4116544113740084c9118a1192e4de2a347edd3) | The source describes a scenario matrix rather than one uniform benchmark run. The owner-approved record deliberately excludes conflicted settings and outcomes. |

## Evidence boundaries

- A printed score without a supported benchmark snapshot, scope, realized `n`, and exact system identity is not sufficient for an `EvaluationRun`.
- An official repository can resolve a paper gap only when a stable commit can be tied to the evaluated artifact.
- Machine-readable result files are preferred to plots.
- Missing information remains `not_reported`; unresolved extraction or source conflict is not converted to `not_reported`.
- No Registry data value was changed as part of this readiness audit.

## Follow-up outcome

The BioSecBench-Surveillance recommendation was completed in PR #363. The
official repository snapshot is represented as a separate Work with three
normalized protocol runs and 16 supported results. The original preprint uses
remain partial because its own reporting gaps were not overwritten by the
later machine-readable artifact.
