"""Partition enumeration on the device."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import adb
from .utils import CommandError

log = logging.getLogger(__name__)

# Partitions that are dangerous to flash on most devices.
DANGEROUS_PARTITIONS = {
    "preloader", "preloader_a", "preloader_b",
    "lk", "lk_a", "lk_b",
    "tee1", "tee2", "tee_a", "tee_b",
    "sec1", "seccfg",
    "efuse",
}

# Partitions whose loss on MediaTek is catastrophic (IMEI/MAC/calibration).
MTK_CRITICAL = {"nvram", "nvdata", "proinfo", "protect_f", "protect_s", "persist"}

# Default set of partitions to back up if user does not specify --all.
DEFAULT_BACKUP_SET = [
    "boot", "recovery", "dtbo", "vbmeta",
    "system", "vendor", "product",
    "persist", "modem", "md1img",
    # MTK critical partitions (skipped silently if not present)
    "nvram", "nvdata", "proinfo", "protect_f", "protect_s",
]


@dataclass
class Partition:
    name: str           # e.g. "boot", "system_a"
    block_path: str     # e.g. "/dev/block/by-name/boot" (or sda6 fallback)
    size_bytes: Optional[int] = None  # may be None if unknown

    def filename(self) -> str:
        # Use .img for filesystem-bearing or boot-style partitions, .bin for raw blobs.
        if self.name in ("nvram", "nvdata", "proinfo", "protect_f", "protect_s",
                         "preloader", "lk", "tee1", "tee2"):
            return f"{self.name}.bin"
        return f"{self.name}.img"


def list_partitions(serial: Optional[str] = None) -> list[Partition]:
    """Enumerate partitions by reading /dev/block/by-name/. Falls back to sysfs."""
    # Primary method: /dev/block/by-name/ symlinks (works on most modern Android).
    try:
        out = adb.shell_root(
            "ls -la /dev/block/by-name/ 2>/dev/null",
            serial=serial, check=False,
        )
    except CommandError:
        out = ""

    parts: dict[str, Partition] = {}
    for line in out.splitlines():
        # `lrwxrwxrwx 1 root root 20 2020-01-01 00:00 boot -> /dev/block/sda10`
        m = re.search(r"\s(\S+)\s*->\s*(\S+)\s*$", line)
        if not m:
            continue
        name, target = m.group(1), m.group(2)
        if name in (".", ".."):
            continue
        parts[name] = Partition(name=name, block_path=f"/dev/block/by-name/{name}")

    # Determine size via blockdev for each one.
    for name, part in list(parts.items()):
        try:
            size_out = adb.shell_root(
                f"blockdev --getsize64 {part.block_path}",
                serial=serial, check=False,
            ).strip()
            if size_out.isdigit():
                part.size_bytes = int(size_out)
        except CommandError:
            pass

    return sorted(parts.values(), key=lambda p: p.name)


def find(name: str, partitions: list[Partition]) -> Optional[Partition]:
    for p in partitions:
        if p.name == name:
            return p
    return None


def is_dangerous(name: str) -> bool:
    # str.rstrip removes any *character* in the argument, not a suffix —
    # e.g. "banana_a".rstrip("_a") -> "bn". Use removesuffix for the real
    # A/B-slot strip, then check both the original and the stripped name.
    if name in DANGEROUS_PARTITIONS:
        return True
    base = name
    for suf in ("_a", "_b"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    return base in DANGEROUS_PARTITIONS
