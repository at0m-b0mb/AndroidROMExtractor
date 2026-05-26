"""Streamed backup of partitions from a rooted Android device over ADB."""
from __future__ import annotations

import gzip
import hashlib
import logging
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from rich.progress import (
    BarColumn, Progress, TextColumn, TimeElapsedColumn, TransferSpeedColumn,
    DownloadColumn,
)

from . import adb, partitions
from .adb import _adb_binary
from .device import Device
from .partitions import Partition
from .utils import CommandError, console, human_size

# Event dicts pushed to a caller-supplied callback (used by the GUI).
# Types:
#   {"type": "start",   "name": str, "total": int|None}
#   {"type": "advance", "name": str, "bytes": int}
#   {"type": "done",    "name": str, "written": int, "sha256": str,
#                        "sha256_on_device": str|None}
#   {"type": "error",   "name": str, "error": str}
EventCallback = Callable[[dict], None]


class BackupCancelled(RuntimeError):
    """Raised when a caller-supplied cancel event is set mid-backup."""

log = logging.getLogger(__name__)

CHUNK = 1 << 20  # 1 MiB read chunks from adb stdout pipe


def _stream_dd(serial: Optional[str], partition: Partition, out_file: Path,
               on_chunk: Callable[[int], None],
               cancel: Optional[threading.Event] = None,
               compress: bool = False) -> tuple[int, int, str]:
    """
    Pipe `su -c dd if=<block> bs=1M` over `adb exec-out`, write to out_file,
    hash as we go.  Calls on_chunk(n_bytes) per uncompressed chunk so the
    caller can drive a progress UI.

    When `compress` is True the on-disk file is gzip-compressed but the hash
    is computed on the *uncompressed* bytes so it still matches what
    `sha256sum /dev/block/...` reports on-device.

    If `cancel` is set mid-stream, kills the subprocess and raises
    BackupCancelled.

    Returns (uncompressed_bytes, on_disk_bytes, sha256_hex). When not
    compressed, both byte counts are equal.
    """
    # `adb exec-out` is critical here — it bypasses the PTY line-mode mangling
    # that `adb shell` does, so binary data passes through cleanly.
    inner = f"dd if={shlex.quote(partition.block_path)} bs=1048576 2>/dev/null"
    su_cmd = f"su -c {shlex.quote(inner)}"
    args = [_adb_binary()]
    if serial:
        args += ["-s", serial]
    args += ["exec-out", su_cmd]

    log.debug("$ %s", " ".join(args))

    h = hashlib.sha256()
    uncompressed = 0
    out_file.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    cancelled = False
    try:
        raw = out_file.open("wb")
        # GzipFile takes a file object and adds the gzip layer transparently;
        # we still get the on-disk size from raw.tell() at the end.
        sink = gzip.GzipFile(fileobj=raw, mode="wb",
                             compresslevel=6) if compress else raw
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                buf = proc.stdout.read(CHUNK)
                if not buf:
                    break
                sink.write(buf)
                h.update(buf)
                uncompressed += len(buf)
                on_chunk(len(buf))
        finally:
            if compress:
                sink.close()  # flush gzip footer
            on_disk = raw.tell()
            raw.close()

        if cancelled:
            proc.kill()
            raise BackupCancelled(f"cancelled while backing up {partition.name}")
        ret = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()

    if ret != 0:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise CommandError(args, ret, stderr)

    return uncompressed, on_disk, h.hexdigest()


def _on_device_sha256(serial: Optional[str], block_path: str) -> Optional[str]:
    """Compute SHA-256 of a block device on the phone itself (for cross-check)."""
    try:
        out = adb.shell_root(
            f"sha256sum {shlex.quote(block_path)}",
            serial=serial, check=False,
        ).strip()
        if out:
            return out.split()[0]
    except CommandError:
        pass
    return None


def _entry_filename(p: Partition, compress: bool) -> str:
    return p.filename() + ".gz" if compress else p.filename()


def backup_partitions(
    device: Device,
    parts: Iterable[Partition],
    out_dir: Path,
    verify_on_device: bool = True,
    dry_run: bool = False,
    events: Optional[EventCallback] = None,
    cancel: Optional[threading.Event] = None,
    compress: bool = False,
) -> list[dict]:
    """
    Back up partitions to out_dir.

    If `events` is provided, it is called for every {start, advance, done, error}
    event and Rich progress is suppressed (the GUI uses this).
    If `events` is None, prints a Rich progress bar to the terminal.

    With `compress=True`, every image is written through gzip on the host (the
    on-device dd is unchanged). Disk usage drops ~50% for most partitions and
    the on-device sha256 still cross-checks correctly because the hash is
    computed on uncompressed bytes.

    Returns list of manifest entries:
        { name, block_path, size_bytes, file, sha256, sha256_on_device,
          compression?: "gzip", compressed_size_bytes?: int, ... }
    """
    parts = list(parts)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    if dry_run:
        for p in parts:
            tag = ".gz" if compress else ""
            console.print(
                f"[dim]DRY[/dim] adb exec-out 'su -c dd if={p.block_path} bs=1M' "
                f"{'| gzip ' if compress else ''}> {out_dir / (p.filename() + tag)}"
            )
        return entries

    if events is not None:
        return _backup_with_callback(device, parts, out_dir, verify_on_device,
                                     events, cancel, compress)

    return _backup_with_rich(device, parts, out_dir, verify_on_device,
                             cancel, compress)


def _backup_with_rich(device, parts, out_dir, verify_on_device,
                      cancel: Optional[threading.Event] = None,
                      compress: bool = False) -> list[dict]:
    entries: list[dict] = []
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        for p in parts:
            if cancel is not None and cancel.is_set():
                break
            target = out_dir / _entry_filename(p, compress)
            total = p.size_bytes if p.size_bytes else 0
            task = progress.add_task(p.name, total=total or None)

            started = datetime.now(timezone.utc)
            try:
                uncompressed, on_disk, host_sha = _stream_dd(
                    device.serial, p, target,
                    on_chunk=lambda n: progress.update(task, advance=n),
                    cancel=cancel, compress=compress,
                )
            except BackupCancelled:
                progress.update(task, description=f"[yellow]{p.name} cancelled")
                break
            except CommandError as e:
                progress.update(task, description=f"[red]{p.name} FAILED")
                log.error("backup of %s failed: %s", p.name, e)
                continue

            on_device_sha = None
            if verify_on_device:
                on_device_sha = _on_device_sha256(device.serial, p.block_path)
                if on_device_sha and on_device_sha != host_sha:
                    log.error(
                        "[%s] hash mismatch! host=%s device=%s — image is CORRUPT",
                        p.name, host_sha[:12], on_device_sha[:12],
                    )

            entry = {
                "name": p.name,
                "block_path": p.block_path,
                "size_bytes": uncompressed,
                "file": _entry_filename(p, compress),
                "sha256": host_sha,
                "sha256_on_device": on_device_sha,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            if compress:
                entry["compression"] = "gzip"
                entry["compressed_size_bytes"] = on_disk
            entries.append(entry)

            label = (f"{human_size(uncompressed)} -> {human_size(on_disk)}"
                     if compress else human_size(uncompressed))
            progress.update(
                task,
                description=f"[green]{p.name}[/green] {label}",
                completed=uncompressed, total=uncompressed,
            )

    return entries


def _backup_with_callback(device, parts, out_dir, verify_on_device,
                          events: EventCallback,
                          cancel: Optional[threading.Event] = None,
                          compress: bool = False) -> list[dict]:
    entries: list[dict] = []
    for p in parts:
        if cancel is not None and cancel.is_set():
            events({"type": "cancelled", "name": p.name})
            break
        target = out_dir / _entry_filename(p, compress)
        events({"type": "start", "name": p.name, "total": p.size_bytes})
        started = datetime.now(timezone.utc)
        try:
            uncompressed, on_disk, host_sha = _stream_dd(
                device.serial, p, target,
                on_chunk=lambda n, _name=p.name: events(
                    {"type": "advance", "name": _name, "bytes": n}),
                cancel=cancel, compress=compress,
            )
        except BackupCancelled:
            events({"type": "cancelled", "name": p.name})
            break
        except CommandError as e:
            events({"type": "error", "name": p.name, "error": str(e)})
            continue

        on_device_sha = None
        if verify_on_device:
            on_device_sha = _on_device_sha256(device.serial, p.block_path)
            if on_device_sha and on_device_sha != host_sha:
                events({"type": "error", "name": p.name,
                        "error": f"hash mismatch: host={host_sha[:12]} "
                                 f"device={on_device_sha[:12]}"})

        entry = {
            "name": p.name,
            "block_path": p.block_path,
            "size_bytes": uncompressed,
            "file": _entry_filename(p, compress),
            "sha256": host_sha,
            "sha256_on_device": on_device_sha,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        if compress:
            entry["compression"] = "gzip"
            entry["compressed_size_bytes"] = on_disk
        entries.append(entry)
        events({"type": "done", "name": p.name, "written": uncompressed,
                "compressed_size": on_disk if compress else None,
                "sha256": host_sha, "sha256_on_device": on_device_sha})
    return entries
