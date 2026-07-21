# BioBench Atlas

BioBench Atlas is a source-grounded registry of benchmarks for protein science, omics, bioinformatics, and applied life-science research. It separates benchmark definitions from the works that evaluate them and records whether an evaluation used the full benchmark, which metrics it reported, and what tools, graders, repetitions, and subsets were used.

The website is published at **https://spectrai-initiative.github.io/bio-benchmark-atlas/**.

The immutable `v1.0.0` release remains available while the registry is upgraded family by family under the development version `1.1.0-dev`. Production entities expose an audit status so downstream users can distinguish legacy records from completed field-level audits.

## Principles

- Primary sources only in v1: benchmark-creator publications and official model-provider reports.
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

The registry deliberately separates four things that are often conflated:

- **Benchmark** — scientific task definition, version, access, taxonomy, counts, and implementations.
- **Work** — the creator publication or official provider document making an evaluation claim.
- **EvaluationRun** — one coherent scope and protocol, including prompts, tools, budgets, repeats, grader, and metrics.
- **Model** — the exact model or agent-and-model identity reported by a source.

Changing a subset, tool, prompt, reasoning setting, budget, grader, or repeat count requires a separate run. Models are only charted together within an explicit `comparability_group`.

Audited benchmarks additionally carry structured version history, permanent resource and evidence IDs, precise locators, and machine-readable `field_status` warnings. Provisional or conflicted totals cannot establish `scope: full`; provisional/conflicted result rows remain downloadable but are excluded from comparison charts.

## Public data interfaces

Every production build publishes:

- `/data/registry.json`
- `/data/benchmarks.json`
- `/data/works.json`
- `/data/evaluation-runs.json`
- `/data/models.json`
- `/data/benchmarks.csv`
- `/data/evaluation-results.csv`
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
