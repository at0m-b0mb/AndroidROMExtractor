"""Verify a backup directory against its manifest."""
from __future__ import annotations

import logging
from pathlib import Path

from .manifest import Manifest, MANIFEST_FILENAME
from .utils import console, human_size, sha256_file

log = logging.getLogger(__name__)


def verify_backup(backup_dir: Path) -> bool:
    """Return True iff every file in the manifest exists and matches its sha256."""
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

        size = file_path.stat().st_size
        if size != entry["size_bytes"]:
            console.print(
                f"[red]SIZE   [/red] {entry['file']} expected {entry['size_bytes']} "
                f"got {size}"
            )
            ok = False
            continue

        actual = sha256_file(file_path)
        if actual != entry["sha256"]:
            console.print(
                f"[red]HASH   [/red] {entry['file']}\n"
                f"        expected {entry['sha256']}\n"
                f"        got      {actual}"
            )
            ok = False
            continue

        console.print(f"[green]OK     [/green] {entry['name']:<14} {human_size(size)}")

    return ok
