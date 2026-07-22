# Paper intake staging

Files under `intake/papers/` are non-production review scaffolds created from the `Review a paper` issue form. They are intentionally excluded from Registry exports and the website.

Before merging an intake PR, a reviewer must inspect the versioned primary source, classify each benchmark relationship, add or update the required Work, BenchmarkUse, EvaluationRun, Model, and Benchmark entities under `registry/`, run the full validation suite, and remove the staging file. An unresolved scaffold must remain a draft PR or issue; setting `production_ready` in a staging file never publishes data.
