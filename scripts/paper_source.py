"""Legally bounded source retrieval for paper intake."""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from pypdf import PdfReader


MAX_SOURCE_BYTES = 45 * 1024 * 1024
MAX_PDF_PAGES = 150
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/xml",
    "text/xml",
    "text/html",
    "text/plain",
}
OPEN_SOURCE_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "europepmc.org",
    "www.ebi.ac.uk",
    "pmc.ncbi.nlm.nih.gov",
}
SOURCE_DOWNLOAD_ATTEMPTS = 3


class SourceAcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrievedSource:
    url: str
    path: Path
    source_access: str
    content_sha256: str
    content_type: str
    retrieved_at: str
    page_count: int | None


def is_automatic_source_allowed(url: str, *, rights_confirmed: bool, discovered: bool) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
    except socket.gaierror:
        return False
    if any(
        (address := ipaddress.ip_address(raw)).is_private
        or address.is_loopback or address.is_link_local or address.is_reserved
        for raw in addresses
    ):
        return False
    if rights_confirmed:
        return True
    return discovered and (host in OPEN_SOURCE_HOSTS or host.endswith(".europepmc.org"))


def _normalized_content_type(value: str | None, url: str) -> str:
    raw = (value or "").split(";", 1)[0].strip().lower()
    if raw == "application/octet-stream":
        guessed = mimetypes.guess_type(url)[0]
        return guessed or raw
    return raw


def _request_source(url: str, *, timeout: float) -> requests.Response:
    headers = {
        "User-Agent": (
            "BioBench-Atlas/1.4 "
            "(+https://github.com/SpectrAI-Initiative/bio-benchmark-atlas)"
        )
    }
    last_error: requests.RequestException | None = None
    for attempt in range(SOURCE_DOWNLOAD_ATTEMPTS):
        try:
            response = requests.get(url, stream=True, timeout=timeout, headers=headers)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            retryable = (
                isinstance(error, (requests.ConnectionError, requests.Timeout))
                or status == 429
                or (isinstance(status, int) and status >= 500)
            )
            if not retryable:
                raise SourceAcquisitionError(
                    f"source download failed: HTTP {status or 'error'}"
                ) from error
            last_error = error
            if attempt + 1 < SOURCE_DOWNLOAD_ATTEMPTS:
                time.sleep(2**attempt)
    raise SourceAcquisitionError(
        f"source download failed after {SOURCE_DOWNLOAD_ATTEMPTS} attempts"
    ) from last_error


def retrieve_source(
    url: str,
    *,
    rights_confirmed: bool,
    discovered: bool = False,
    timeout: float = 45,
) -> RetrievedSource:
    if not is_automatic_source_allowed(url, rights_confirmed=rights_confirmed, discovered=discovered):
        raise SourceAcquisitionError("source use was not confirmed and the URL is not a recognized open source")

    response = _request_source(url, timeout=timeout)
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > MAX_SOURCE_BYTES:
        raise SourceAcquisitionError("source exceeds the 45 MiB download limit")
    content_type = _normalized_content_type(response.headers.get("Content-Type"), response.url)
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise SourceAcquisitionError(f"unsupported source content type: {content_type or 'missing'}")

    suffix = ".pdf" if content_type == "application/pdf" else ".txt"
    temporary = tempfile.NamedTemporaryFile(prefix="biobench-paper-", suffix=suffix, delete=False)
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise SourceAcquisitionError("source exceeds the 45 MiB download limit")
                digest.update(chunk)
                temporary.write(chunk)
        path = Path(temporary.name)
        page_count = None
        if content_type == "application/pdf":
            if path.read_bytes()[:5] != b"%PDF-":
                raise SourceAcquisitionError("source declared as PDF but has no PDF signature")
            try:
                page_count = len(PdfReader(path).pages)
            except Exception as error:  # pypdf raises several format-specific exceptions
                raise SourceAcquisitionError(f"PDF could not be parsed: {error}") from error
            if page_count > MAX_PDF_PAGES:
                raise SourceAcquisitionError("PDF exceeds the 150-page extraction limit")
        return RetrievedSource(
            url=response.url,
            path=path,
            source_access="open-url" if discovered else "submitted-pdf",
            content_sha256=digest.hexdigest(),
            content_type=content_type,
            retrieved_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            page_count=page_count,
        )
    except Exception:
        Path(temporary.name).unlink(missing_ok=True)
        raise
