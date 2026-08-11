#!/usr/bin/env python3
"""Build deterministic, public-safe nucleic-acid result release assets.

The browser data package intentionally excludes the large audited workbook and
the human-readable QC material.  This script combines the tracked CSV archive
with those local, owner-audited files without publishing raw source snapshots or
machine-local provenance paths.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
QC_FILES = (
    "validation_summary.json",
    "workbook_validation.json",
    "independent_qa_receipt.json",
    "programmatic_import_summary.json",
)
TEXT_ARCHIVE_SUFFIXES = {
    ".csv",
    ".json",
    ".md",
    ".rels",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
LOCAL_PATH_MARKERS = ("/Users/", "/home/", "/mnt/")
LOCAL_PATH_RE = re.compile(r"/(?:Users|home|mnt)/[^\s<>\"'`]+")
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:token|access_token|id_token|auth_token|signature|sig|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|x-goog-credential)="
)


class ReleasePackagingError(RuntimeError):
    """Raised when release inputs are incomplete, unsafe, or inconsistent."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_archive_name(name: str, *, label: str) -> None:
    path = PurePosixPath(name)
    if not name or name.startswith(("/", "\\")) or ".." in path.parts or "\\" in name:
        raise ReleasePackagingError(f"{label} contains unsafe archive member {name!r}")


def _path_replacement(raw: str, source_root: Path) -> str:
    candidate = Path(raw.rstrip("/"))
    try:
        relative = candidate.relative_to(source_root)
    except ValueError:
        return candidate.name
    rendered = relative.as_posix()
    return rendered if rendered and rendered != "." else source_root.name


def sanitize_public_text(text: str, source_root: Path) -> str:
    """Replace local paths with snapshot-relative paths and reject credentials."""

    source_root = source_root.resolve()
    root_text = source_root.as_posix()
    text = text.replace(f"{root_text}/", "").replace(root_text, source_root.name)
    text = LOCAL_PATH_RE.sub(lambda match: _path_replacement(match.group(0), source_root), text)
    assert_safe_public_text(text, label="sanitized release text")
    return text


def sanitize_public_value(value: Any, source_root: Path) -> Any:
    if isinstance(value, str):
        return sanitize_public_text(value, source_root)
    if isinstance(value, list):
        return [sanitize_public_value(item, source_root) for item in value]
    if isinstance(value, dict):
        return {
            key: sanitize_public_value(item, source_root)
            for key, item in value.items()
        }
    return value


def assert_safe_public_text(text: str, *, label: str) -> None:
    for marker in (*LOCAL_PATH_MARKERS, "file://"):
        if marker.lower() in text.lower():
            raise ReleasePackagingError(f"{label} contains forbidden public value {marker!r}")
    match = SENSITIVE_VALUE_RE.search(text)
    if match:
        raise ReleasePackagingError(
            f"{label} contains a token or signed-URL query key: {match.group(0)!r}"
        )


def _is_text_member(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in TEXT_ARCHIVE_SUFFIXES


def assert_safe_artifact_bytes(data: bytes, *, name: str) -> None:
    """Scan a public artifact, including textual members of ZIP/XLSX/GZIP files."""

    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {".zip", ".xlsx"}:
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                seen: set[str] = set()
                for info in archive.infolist():
                    _safe_archive_name(info.filename, label=name)
                    if info.filename in seen:
                        raise ReleasePackagingError(
                            f"{name} contains duplicate archive member {info.filename!r}"
                        )
                    seen.add(info.filename)
                    if info.is_dir() or not _is_text_member(info.filename):
                        continue
                    member = archive.read(info)
                    try:
                        text = member.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        raise ReleasePackagingError(
                            f"{name}!{info.filename} is not valid UTF-8 text"
                        ) from exc
                    assert_safe_public_text(text, label=f"{name}!{info.filename}")
        except BadZipFile as exc:
            raise ReleasePackagingError(f"{name} is not a valid ZIP container") from exc
        return

    if suffix == ".gz":
        try:
            unpacked = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise ReleasePackagingError(f"{name} is not a valid gzip stream") from exc
        try:
            text = unpacked.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ReleasePackagingError(f"{name} does not contain UTF-8 text") from exc
        assert_safe_public_text(text, label=name)
        return

    if suffix in TEXT_ARCHIVE_SUFFIXES or suffix in {"", ".sha256", ".sha256sums"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ReleasePackagingError(f"{name} is not valid UTF-8 text") from exc
        assert_safe_public_text(text, label=name)


def assert_safe_artifact(path: Path) -> None:
    assert_safe_artifact_bytes(path.read_bytes(), name=path.name)


def deterministic_zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            _safe_archive_name(name, label="deterministic ZIP")
            info = ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits |= 0x800
            archive.writestr(info, files[name], compress_type=ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleasePackagingError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleasePackagingError(f"expected a JSON object in {path}")
    return value


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_validated_core_csvs(archive_path: Path, manifest_path: Path) -> dict[str, bytes]:
    manifest = _read_json(manifest_path)
    archive_meta = manifest.get("archive")
    file_meta = manifest.get("files")
    if not isinstance(archive_meta, dict) or not isinstance(file_meta, dict) or not file_meta:
        raise ReleasePackagingError(f"invalid source manifest structure in {manifest_path}")

    data = archive_path.read_bytes()
    expected_sha = archive_meta.get("sha256")
    expected_bytes = archive_meta.get("bytes")
    if expected_sha != sha256_bytes(data):
        raise ReleasePackagingError(f"CSV archive SHA256 does not match {manifest_path}")
    if expected_bytes != len(data):
        raise ReleasePackagingError(f"CSV archive byte size does not match {manifest_path}")

    members: dict[str, bytes] = {}
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if len(names) != len(set(names)):
                raise ReleasePackagingError("CSV archive contains duplicate members")
            if set(names) != set(file_meta):
                missing = sorted(set(file_meta) - set(names))
                extra = sorted(set(names) - set(file_meta))
                raise ReleasePackagingError(
                    f"CSV archive/manifest membership mismatch; missing={missing}, extra={extra}"
                )
            for name in sorted(names):
                _safe_archive_name(name, label=archive_path.name)
                if PurePosixPath(name).suffix.lower() != ".csv":
                    raise ReleasePackagingError(f"non-CSV member in release CSV archive: {name}")
                member = archive.read(name)
                members[name] = member
                metadata = file_meta[name]
                if not isinstance(metadata, dict):
                    raise ReleasePackagingError(f"invalid file metadata for {name}")
                if metadata.get("sha256") != sha256_bytes(member):
                    raise ReleasePackagingError(f"CSV member SHA256 mismatch: {name}")
                if metadata.get("bytes") != len(member):
                    raise ReleasePackagingError(f"CSV member byte-size mismatch: {name}")
    except BadZipFile as exc:
        raise ReleasePackagingError(f"invalid CSV ZIP: {archive_path}") from exc

    assert_safe_artifact_bytes(data, name=archive_path.name)
    return members


def _build_release_csv_archive(
    source_root: Path,
    tracked_archive: Path,
    source_manifest: Path,
) -> bytes:
    tracked = _load_validated_core_csvs(tracked_archive, source_manifest)
    source_files = {
        path.name: path.read_bytes()
        for path in sorted(source_root.glob("*.csv"))
        if path.is_file()
    }
    if not source_files:
        raise ReleasePackagingError(f"no root-level CSV files found in {source_root}")

    missing = sorted(set(tracked) - set(source_files))
    mismatched = sorted(
        name for name, data in tracked.items()
        if name in source_files and source_files[name] != data
    )
    if missing or mismatched:
        raise ReleasePackagingError(
            "authoritative snapshot does not match the tracked web CSV package; "
            f"missing={missing}, byte_mismatches={mismatched}"
        )

    for name, data in source_files.items():
        _safe_archive_name(name, label="release CSV ZIP")
        assert_safe_artifact_bytes(data, name=name)
    release = deterministic_zip(source_files)
    assert_safe_artifact_bytes(release, name="release-csv.zip")
    return release


def _sanitize_xlsx(path: Path, source_root: Path) -> bytes:
    try:
        with ZipFile(path) as source:
            if source.testzip() is not None:
                raise ReleasePackagingError(f"XLSX CRC check failed: {path}")
            members: dict[str, bytes] = {}
            for info in source.infolist():
                if info.is_dir():
                    continue
                _safe_archive_name(info.filename, label=path.name)
                if info.filename in members:
                    raise ReleasePackagingError(
                        f"XLSX contains duplicate member {info.filename!r}"
                    )
                data = source.read(info)
                if _is_text_member(info.filename) and any(
                    marker.lower().encode("utf-8") in data.lower()
                    for marker in (*LOCAL_PATH_MARKERS, "file://", "token=", "signature=")
                ):
                    try:
                        text = data.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        raise ReleasePackagingError(
                            f"cannot sanitize non-UTF-8 XLSX member {info.filename}"
                        ) from exc
                    data = sanitize_public_text(text, source_root).encode("utf-8")
                members[info.filename] = data
    except BadZipFile as exc:
        raise ReleasePackagingError(f"invalid XLSX package: {path}") from exc

    if "[Content_Types].xml" not in members:
        raise ReleasePackagingError(f"XLSX is missing [Content_Types].xml: {path}")
    sanitized = deterministic_zip(members)
    with ZipFile(io.BytesIO(sanitized)) as workbook:
        if workbook.testzip() is not None:
            raise ReleasePackagingError("sanitized XLSX failed its CRC check")
    assert_safe_artifact_bytes(sanitized, name=path.name)
    return sanitized


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _snapshot_date(source_root: Path) -> str:
    summary = _read_json(source_root / "validation_summary.json")
    value = summary.get("built_at")
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ReleasePackagingError("validation_summary.json has no YYYY-MM-DD built_at value")
    return value


def package_release(
    *,
    source_root: Path,
    output_dir: Path | None = None,
    csv_archive: Path | None = None,
    source_manifest: Path | None = None,
) -> list[Path]:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ReleasePackagingError(f"snapshot directory does not exist: {source_root}")

    snapshot_date = _snapshot_date(source_root)
    snapshot_dir = ROOT / "data" / "nucleic-acid-results" / snapshot_date
    csv_archive = (csv_archive or snapshot_dir / "source-csvs.zip").resolve()
    source_manifest = (source_manifest or snapshot_dir / "source-manifest.json").resolve()
    output_dir = (output_dir or ROOT / "release-artifacts" / f"nucleic-acid-results-{snapshot_date}").resolve()

    required = [
        csv_archive,
        source_manifest,
        source_root / "data_dictionary.md",
        source_root / f"nucleic_acid_baseline_sota_{snapshot_date.replace('-', '')}.xlsx",
        *(source_root / name for name in QC_FILES),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ReleasePackagingError(f"missing release inputs: {', '.join(missing)}")

    prefix = f"nucleic-acid-results-{snapshot_date}"
    csv_name = f"{prefix}-csv.zip"
    dictionary_name = f"{prefix}-data-dictionary.md"
    workbook_path = source_root / f"nucleic_acid_baseline_sota_{snapshot_date.replace('-', '')}.xlsx"

    assets: dict[str, bytes] = {
        csv_name: _build_release_csv_archive(source_root, csv_archive, source_manifest),
        workbook_path.name: _sanitize_xlsx(workbook_path, source_root),
        dictionary_name: sanitize_public_text(
            (source_root / "data_dictionary.md").read_text(encoding="utf-8"),
            source_root,
        ).encode("utf-8"),
    }

    qc_values: dict[str, dict[str, Any]] = {}
    for name in QC_FILES:
        qc_values[name] = sanitize_public_value(_read_json(source_root / name), source_root)

    workbook_qc = qc_values["workbook_validation.json"].get("workbook")
    if not isinstance(workbook_qc, dict):
        raise ReleasePackagingError("workbook_validation.json has no workbook object")
    workbook_qc["pre_release_bytes"] = workbook_qc.get("bytes")
    workbook_qc["pre_release_sha256"] = workbook_qc.get("sha256")
    workbook_qc["bytes"] = len(assets[workbook_path.name])
    workbook_qc["sha256"] = sha256_bytes(assets[workbook_path.name])
    workbook_qc["independent_reimport"] = "passed before deterministic path sanitization"
    workbook_qc["release_transform"] = (
        "OOXML text members were rewritten deterministically to remove machine-local paths; "
        "ZIP integrity was rechecked after packaging."
    )

    for name, value in qc_values.items():
        output_name = f"{prefix}-{name.replace('_', '-')}"
        assets[output_name] = _canonical_json(value)

    for name, data in assets.items():
        assert_safe_artifact_bytes(data, name=name)

    checksums = "".join(
        f"{sha256_bytes(data)}  {name}\n"
        for name, data in sorted(assets.items())
    ).encode("ascii")
    assets["SHA256SUMS"] = checksums
    assert_safe_artifact_bytes(checksums, name="SHA256SUMS")

    written: list[Path] = []
    for name, data in sorted(assets.items()):
        path = output_dir / name
        _atomic_write(path, data)
        written.append(path)
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="audited dated snapshot directory")
    parser.add_argument("--output-dir", type=Path, help="release staging directory")
    parser.add_argument("--csv-archive", type=Path, help="tracked deterministic source-csvs.zip")
    parser.add_argument("--source-manifest", type=Path, help="manifest paired with --csv-archive")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        written = package_release(
            source_root=args.source,
            output_dir=args.output_dir,
            csv_archive=args.csv_archive,
            source_manifest=args.source_manifest,
        )
    except (OSError, ReleasePackagingError) as exc:
        raise SystemExit(f"release packaging failed: {exc}") from exc
    for path in written:
        print(f"{sha256_bytes(path.read_bytes())}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
