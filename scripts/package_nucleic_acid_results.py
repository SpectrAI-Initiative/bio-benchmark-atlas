from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from pathlib import Path

from nucleic_acid_results import (
    SCHEMA_VERSION,
    SNAPSHOT_DATE,
    SOURCE_ARCHIVE,
    SOURCE_MANIFEST,
    SOURCE_SNAPSHOT_ID,
    SOURCE_TABLES,
    canonical_json_bytes,
    sha256_bytes,
)


def _row_metadata(data: bytes) -> tuple[int, list[str]]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    return sum(1 for _ in reader), list(reader.fieldnames)


def package(source: Path, archive_path: Path, manifest_path: Path) -> dict[str, object]:
    missing = [filename for filename in SOURCE_TABLES if not (source / filename).is_file()]
    if missing:
        raise FileNotFoundError(f"source snapshot is missing required files: {missing}")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for filename in SOURCE_TABLES:
            data = (source / filename).read_bytes()
            info = zipfile.ZipInfo(filename=filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    files: dict[str, dict[str, object]] = {}
    for filename in SOURCE_TABLES:
        data = (source / filename).read_bytes()
        row_count, columns = _row_metadata(data)
        files[filename] = {
            "sha256": sha256_bytes(data),
            "bytes": len(data),
            "row_count": row_count,
            "columns": columns,
        }
    archive_bytes = archive_path.read_bytes()
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": SNAPSHOT_DATE,
        "literature_cutoff": SNAPSHOT_DATE,
        "source_snapshot_id": SOURCE_SNAPSHOT_ID,
        "archive": {
            "path": archive_path.name,
            "sha256": sha256_bytes(archive_bytes),
            "bytes": len(archive_bytes),
            "compression": "zip-deflate-level-9",
        },
        "files": files,
    }
    manifest_path.write_bytes(canonical_json_bytes(manifest, pretty=True))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the deterministic nucleic-acid results source package.")
    parser.add_argument("--source", required=True, type=Path, help="Authoritative snapshot directory")
    parser.add_argument("--archive", type=Path, default=SOURCE_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=SOURCE_MANIFEST)
    args = parser.parse_args()
    manifest = package(args.source.resolve(), args.archive.resolve(), args.manifest.resolve())
    print(json.dumps({"archive": manifest["archive"], "files": len(manifest["files"])}, indent=2))


if __name__ == "__main__":
    main()
