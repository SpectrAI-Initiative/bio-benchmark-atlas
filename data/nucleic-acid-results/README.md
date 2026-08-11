# Nucleic-acid results snapshots

This directory contains the immutable, audit-ready source package used to build the
static nucleic-acid benchmark results explorer. It does not contain benchmark data,
model weights, papers, or training records.

## Snapshot layout

- `2026-08-05/source-csvs.zip` contains the 16 CSV tables needed by the web build.
- `2026-08-05/source-manifest.json` pins every source member by byte count, row
  count, ordered columns, and SHA-256, and pins the deterministic ZIP itself.
- `2026-08-06/` is the gap-closure snapshot. Its 17-table package adds
  `track_result_coverage.csv`, preserves every 2026-08-05 result ID, and exposes
  the reviewed B07/B08/B18/B23 numeric additions without changing the old package.
- `crosswalks.json` is the reviewed bridge to the core BioBench Atlas Registry.
  Unreviewed similarities stay explicitly unmapped.

CSV values are preserved as strings so reported precision, `NR`, and empty source
cells are not silently changed. Client code may parse numeric display fields only
after consulting the metric definition.

## Rebuild and validate

```bash
python3 scripts/validate_nucleic_acid_results.py
python3 scripts/build_nucleic_acid_results.py
python3 -m pytest tests/test_nucleic_acid_results.py
```

The build writes ignored artifacts under `site/public/data/nucleic-acids/`, copies
the public JSON Schema to `site/public/schema/`, and writes uncompressed build-time
indexes under `site/src/generated/nucleic-acid-results/`. Public JSON is canonical,
gzip timestamps are fixed to zero, and filenames are addressed by the SHA-256 of
their compressed bytes.

To reproduce the tracked source archive from an authoritative staging directory:

```bash
python3 scripts/package_nucleic_acid_results.py \
  --snapshot-date 2026-08-06 \
  --source /path/to/nucleic_acid_benchmark_results_20260806
```

The source archive is only replaced after review. The build validates every date
listed by the manifest-driven snapshot registry, emits both immutable route families,
and updates only `latest.json` to select the newest date.
