# BioBench Atlas Codex guidance

## Paper intake

- Use the repo skill `$biobench-paper-intake` for paper URLs, DOIs, preprints, or paper-intake GitHub issues.
- Run paper evidence extraction only through `scripts/local_paper_intake.py`; do not call the OpenAI API or read an `OPENAI_API_KEY`.
- Never commit papers, XML/HTML full text, evidence excerpts, Codex transcripts, extraction drafts, verification drafts, or files under `.paper-intake-tmp/`.
- Never estimate a numeric result from an unlabeled chart. Treat parse failures separately from source fields that are genuinely not reported.
- Process one target paper per PR, together with only the creator sources needed to establish a new benchmark.
- Do not assign permanent Registry IDs manually when the deterministic paper generator can assign them.
- Keep VirBench `legacy/unclassified`; paper intake must not refine it incidentally.

## Verification

- Validate Registry changes with `pnpm registry:validate`, `pnpm registry:build`, and `pnpm test:data`.
- Build the site with `pnpm site:build`.
- Paper-intake PRs use `paper-intake/<work-id>-<issue-number>` and require the owner SHA-comment gate described by the skill.
