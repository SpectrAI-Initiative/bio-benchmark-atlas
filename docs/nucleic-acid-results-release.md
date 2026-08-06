# Nucleic-acid results release procedure

The `2026-08-05` nucleic-acid results snapshot is published as a separate,
immutable release. Its tag is `nucleic-acid-results-2026-08-05`; it is not part
of the monthly Registry tag namespace.

The release CSV ZIP contains every root-level CSV in the authoritative snapshot
(21 files in the `2026-08-05` snapshot), including search, screening, review,
candidate-exception, and protocol-summary audit tables. It also contains the
existing audited XLSX workbook, a data dictionary, four QC receipts, and
`SHA256SUMS`. Raw `source_snapshots/`, `research/`, previews, and extraction
scripts are never release assets.

## Preconditions

Run this procedure after the release commit has merged to `main`. Use the
owner-audited snapshot directory whose `validation_summary.json` identifies
`2026-08-05` and reports 47 benchmarks, 58 tasks, 334 protocols, and 55,989
results. The repository copy of
`data/nucleic-acid-results/2026-08-05/source-csvs.zip` must match its tracked
`source-manifest.json`.

```bash
pnpm install --frozen-lockfile
pnpm results:validate
pnpm results:build
pnpm results:release -- \
  --source /absolute/path/to/nucleic_acid_benchmark_results_20260805
```

The last command writes only to the ignored directory
`release-artifacts/nucleic-acid-results-2026-08-05/`. It performs these gates
before writing the checksum manifest:

- verifies the 16 web-core CSVs against `source-manifest.json` and requires
  those bytes to match the corresponding root-level snapshot CSVs;
- sorts and packages every root-level snapshot CSV, including the five
  release-only audit tables, into a deterministic ZIP;
- excludes raw source snapshots and non-public research material;
- rewrites machine-local paths in the dictionary, QC JSON, and OOXML text to
  snapshot-relative paths;
- rejects `/Users/`, `/home/`, `/mnt/`, `file://`, token query keys, and signed
  URL query keys in direct, gzip, ZIP, and XLSX artifacts;
- fixes ZIP entry order, timestamps, permissions, and compression settings;
- rechecks the sanitized XLSX package CRCs and records its release hash in the
  packaged workbook validation receipt.

## Determinism and checksum verification

Package twice into separate empty staging directories and compare the manifests:

```bash
pnpm results:release -- \
  --source /absolute/path/to/nucleic_acid_benchmark_results_20260805 \
  --output-dir /tmp/nucleic-acid-release-a
pnpm results:release -- \
  --source /absolute/path/to/nucleic_acid_benchmark_results_20260805 \
  --output-dir /tmp/nucleic-acid-release-b
diff -u /tmp/nucleic-acid-release-a/SHA256SUMS /tmp/nucleic-acid-release-b/SHA256SUMS
(cd /tmp/nucleic-acid-release-a && shasum -a 256 --check SHA256SUMS)
```

Do not publish if the manifests differ or any checksum fails. The sanitized
workbook intentionally has a different hash from the pre-release XLSX; both
hashes are retained in the packaged workbook validation receipt.

## Publish after merge

From the merged `main` checkout, run this exact command after all checks above
pass:

```bash
gh release create "nucleic-acid-results-2026-08-05" \
  release-artifacts/nucleic-acid-results-2026-08-05/SHA256SUMS \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-csv.zip \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic_acid_baseline_sota_20260805.xlsx \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-data-dictionary.md \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-validation-summary.json \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-workbook-validation.json \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-independent-qa-receipt.json \
  release-artifacts/nucleic-acid-results-2026-08-05/nucleic-acid-results-2026-08-05-programmatic-import-summary.json \
  --target main \
  --title "Nucleic-acid benchmark results — 2026-08-05" \
  --notes "Audited nucleic-acid benchmark result snapshot: 47 benchmarks, 58 tasks, 334 protocols, and 55,989 results. Verify every downloaded asset with SHA256SUMS."
```

After publication, download the release assets into a new temporary directory
and run `shasum -a 256 --check SHA256SUMS` once more. Never reuse or overwrite
this tag; corrections require a new dated snapshot and tag.
