# Registry mapping

- Reuse Work by DOI, then preprint ID, canonical URL, and title fingerprint.
- Reuse Benchmark only through its permanent ID, alias, creator identifier, or an independently verified exact identity.
- Create a BenchmarkUse for every actual relation. Ignore pure background citations.
- Create an EvaluationRun only when benchmark version, scope, realized `n`, exact model, and metric are independently supported.
- Split runs whenever subset, prompt, tools, budget, grader, repeats, harness, or reasoning settings differ.
- Keep delta metrics distinct from absolute values and require the baseline model for a delta.
- For a new benchmark, require an independently supported creator source and official repository or dataset. Pin a repository to a resolved commit.
- Map only the most specific supported Scientific Task. Do not add both a parent and child task.
- Let deterministic code create permanent IDs and YAML. Do not hand-edit generated entity IDs.
- Keep one target paper per PR. Creator records required for a newly introduced benchmark may be included in that PR.
