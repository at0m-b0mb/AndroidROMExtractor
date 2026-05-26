"""Verify a backup directory against its manifest."""
from __future__ import annotations

import gzip
import hashlib
import logging
from pathlib import Path

from .manifest import Manifest, MANIFEST_FILENAME
from .utils import console, human_size, sha256_file

log = logging.getLogger(__name__)


def _sha256_uncompressed(path: Path, chunk: int = 1 << 20) -> tuple[str, int]:
    """SHA-256 of a gzip file's uncompressed contents. Streaming — never
    materializes the full image. Returns (hex_digest, uncompressed_size)."""
    h = hashlib.sha256()
    size = 0
    with gzip.open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
    return h.hexdigest(), size


def verify_backup(backup_dir: Path) -> bool:
    """Return True iff every file in the manifest exists and matches its sha256.

    Handles both uncompressed (`*.img`/`*.bin`) and compressed (`*.gz`) entries.
    The manifest's `sha256` is always the *uncompressed* content hash, so
    compressed files are stream-decompressed during verification."""
    manifest_path = backup_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]No manifest at {manifest_path}[/red]")
        return False

    manifest = Manifest.read(manifest_path)
    ok = True

    for entry in manifest.partitions:
        file_path = backup_dir / entry["file"]
        if not file_path.exists():
            console.print(f"[red]MISSING[/red] {entry['file']}")
            ok = False
            continue

        compressed = entry.get("compression") == "gzip"
        size = file_path.stat().st_size

        # Size check: for compressed entries we compare against the recorded
        # compressed size (if present); for uncompressed, against size_bytes.
        expected_size = (entry.get("compressed_size_bytes") if compressed
                         else entry["size_bytes"])
        if expected_size is not None and size != expected_size:
            console.print(
                f"[red]SIZE   [/red] {entry['file']} expected {expected_size} "
                f"got {size}"
            )
            ok = False
            continue

        if compressed:
            actual, uncomp_size = _sha256_uncompressed(file_path)
            if uncomp_size != entry["size_bytes"]:
                console.print(
                    f"[red]UNZSIZ [/red] {entry['file']} "
                    f"uncompressed expected {entry['size_bytes']} "
                    f"got {uncomp_size}"
                )
                ok = False
                continue
        else:
            actual = sha256_file(file_path)

        if actual != entry["sha256"]:
            console.print(
                f"[red]HASH   [/red] {entry['file']}\n"
                f"        expected {entry['sha256']}\n"
                f"        got      {actual}"
            )
            ok = False
            continue

        tag = " (gz)" if compressed else ""
        console.print(
            f"[green]OK     [/green] {entry['name']:<14} {human_size(size)}{tag}")

    return ok
