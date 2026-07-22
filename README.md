# BioBench Atlas

BioBench Atlas is a source-grounded registry of benchmarks for protein science, omics, bioinformatics, chemistry, and applied life-science research. It separates benchmark definitions, versioned source Works, Work→Benchmark usage claims, normalized evaluation runs, and exact model identities.

The website is published at **https://spectrai-initiative.github.io/bio-benchmark-atlas/**.

The immutable `v1.2.0` release adds a Scientific Task layer that distinguishes problems such as protein folding, protein sequence design, PPI, protein-ligand binding, DNA regulation, RNA design, small-molecule discovery, omics analysis, and scientific workflows. It also expands the atlas with creator-audited TAPE, Genomic Benchmarks, BEACON, MoleculeNet, ATOM3D, GuacaMol, and scIB records. VirBench's detailed audit and task classification remain intentionally deferred and visibly marked `legacy` / `unclassified`; this is the only CI-enforced exception.

The `v1.3.0` line adds versioned Works, a first-class `BenchmarkUse` relationship, Anthropic/Claude life-science evidence, SpatialBench, and a durable paper-intake workflow. A submitted paper link is identity-normalized and duplicate-checked into a draft PR; it cannot enter production until a reviewer verifies the source-level benchmark relationship and settings.

## Principles

- Publishable sources include benchmark creators, official model providers, peer-reviewed papers, and stable versioned formal preprints.
- Unknown is not zero. Unreported settings remain explicit `null` values with a reporting status.
- Results are compared only inside compatible benchmark/version/scope/protocol groups.
- Third-party tasks, papers, and restricted artifacts are linked, not mirrored.
- There is no cross-benchmark global model leaderboard.

## Repository layout

- `registry/`: human-maintained YAML records for benchmarks, works, models, evaluations, and taxonomy.
- `schema/`: the public JSON Schema for registry entities.
- `scripts/`: validation, deterministic export generation, and source monitoring.
- `site/`: the Astro website deployed to GitHub Pages.
- `exports/`: generated JSON and CSV release assets.

## Data model

The registry deliberately separates five things that are often conflated:

- **Benchmark** — scientific task definition, version, access, taxonomy, counts, and implementations.
- **Work** — the creator publication or official provider document making an evaluation claim.
- **BenchmarkUse** — how a Work uses a Benchmark: creation, evaluation, training, fine-tuning, validation, model selection, or external result summary. Incomplete claims remain partial and never enter comparison charts.
- **EvaluationRun** — one coherent scope and protocol, including exact evaluated model/system IDs, prompts, tools, budgets, repeats, grader, and metrics. A model can be linked even when the source reports no extractable scalar result.
- **Model** — the exact model or agent-and-model identity reported by a source.

Benchmarks also expose `scientific_task_classification`, which maps the benchmark or formal track to the most specific evidence-supported Scientific Task. A mapping records its version, coverage status, method, confidence, original count unit and basis, evidence IDs, and whether the count was reported. Domain, capability, modality, task format, and Scientific Task remain separate axes.

Changing a subset, tool, prompt, reasoning setting, budget, grader, or repeat count requires a separate run. Models are only charted together within an explicit `comparability_group`.

Audited benchmarks additionally carry structured version history, permanent resource and evidence IDs, precise locators, and machine-readable `field_status` warnings. Provisional or conflicted totals cannot establish `scope: full`; provisional/conflicted result rows remain downloadable but are excluded from comparison charts.

## Public data interfaces

Every production build publishes:

- `/data/registry.json`
- `/data/benchmarks.json`
- `/data/works.json`
- `/data/evaluation-runs.json`
- `/data/benchmark-uses.json`
- `/data/models.json`
- `/data/benchmarks.csv`
- `/data/evaluation-results.csv`
- `/data/works.csv`
- `/data/benchmark-uses.csv`
- `/data/scientific-tasks.json`
- `/data/scientific-task-coverage.json`
- `/data/scientific-task-coverage.csv`
- `/schema/registry.schema.json`

These files and the website are generated from the same normalized in-memory object. Do not edit generated exports directly.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[test]'
pnpm install
pnpm build
pnpm site:dev
```

## Contributing

Use the issue forms or open a pull request. Every factual claim must cite a creator publication or an official model-provider source with a page, section, figure, or table locator when available. See [CONTRIBUTING.md](CONTRIBUTING.md), the website methodology, and the contribution guide for the full policy.

## Citation

Use the repository release that you consulted. Release snapshots attach immutable generated JSON and CSV assets; citation metadata is available in `CITATION.cff`.

## Licensing

Code is licensed under Apache-2.0. Original curated metadata is licensed under CC BY 4.0. Referenced benchmark content remains under its original owners' licenses.
