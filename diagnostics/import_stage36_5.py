"""Import the original Stage 36.5 JSON files into a compact, sanitized archive.

The source directory remains untouched. SHA-256 hashes of the original files
are recorded so the sanitized archive can be checked against the historical
run. Query values resembling credentials are redacted before publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zipfile import ZIP_DEFLATED, ZipFile


SENSITIVE = re.compile(r"(?i)(token|secret|password|passwd|session|cookie|signature|api[_-]?key|access[_-]?key|authorization|auth[_-]?code|client[_-]?id|state)")


def sanitize(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE.search(key) else sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    # URLs can appear inside titles and explanations, so redact embedded
    # credential-like URL query parameters as well as standalone URL fields.
    def clean_url(match: re.Match) -> str:
        original = match.group(0)
        parts = urlsplit(original)
        if not parts.query:
            return original
        query = urlencode([
            (key, "[REDACTED]" if SENSITIVE.search(key) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    return re.sub(r"https?://[^\s\"'<>]+", clean_url, value)


def import_files(source: Path, archive_path: Path, manifest_path: Path) -> None:
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("Refusing to overwrite historical archive or manifest")
    files = [source / f"{i:02d}.json" for i in range(1, 51)]
    if any(not path.is_file() for path in files):
        raise FileNotFoundError("Expected exactly the historical 01.json through 50.json")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            raw = path.read_bytes()
            data = json.loads(raw)
            hashes[path.name] = hashlib.sha256(raw).hexdigest()
            archive.writestr(path.name, json.dumps(sanitize(data), ensure_ascii=False, separators=(",", ":")))
    manifest_path.write_text(json.dumps({
        "source_code_commit": "bd9238e",
        "original_files_sha256": hashes,
        "sanitization": "credential-like JSON keys and URL query values redacted",
        "original_file_count": len(files),
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")


def audit_archive(archive_path: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == manifest["archive_sha256"]
    with ZipFile(archive_path) as archive:
        assert archive.namelist() == [f"{i:02d}.json" for i in range(1, 51)]
        for name in archive.namelist():
            item = json.loads(archive.read(name))
            assert item["idx"] == int(name[:2])
            assert not _contains_sensitive_key(item)
            assert not _contains_sensitive_url(item)


def _contains_sensitive_key(value) -> bool:
    if isinstance(value, dict):
        return any(
            (SENSITIVE.search(key) and item != "[REDACTED]")
            or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _contains_sensitive_url(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_sensitive_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_url(item) for item in value)
    if not isinstance(value, str):
        return False
    for match in re.finditer(r"https?://[^\s\"'<>]+", value):
        for key, item in parse_qsl(urlsplit(match.group(0)).query):
            if SENSITIVE.search(key) and item not in ("[REDACTED]", "%5BREDACTED%5D"):
                return True
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.audit:
        if len(args.paths) != 2:
            parser.error("--audit requires ARCHIVE MANIFEST")
        audit_archive(*args.paths)
        print("archive audit OK")
    elif len(args.paths) == 3:
        import_files(*args.paths)
    else:
        parser.error("import requires SOURCE ARCHIVE MANIFEST")
