"""Lightweight, dependency-free boot.img / recovery.img analyzer.

What it can answer without unpacking the ramdisk:
  * Is this an Android boot image at all? (magic "ANDROID!" at offset 0)
  * Does it look Magisk-patched? (heuristic byte-string scan)
  * Any AVB / VBMeta footprint?

We deliberately *don't* try to decompress the ramdisk — that brings in
gzip/lz4/xz/zstd and an Android cpio parser. For most use-cases the
byte-scan is enough: Magisk's own binaries and backup files leave
literal strings in the image that survive on disk.

Markers cross-checked against
https://github.com/topjohnwu/Magisk (native/src/boot/cpio.rs).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ANDROID_BOOT_MAGIC = b"ANDROID!"     # offset 0 in boot.img
VBMETA_MAGIC = b"AVB0"               # vbmeta footer

MAGISK_MARKERS = [
    b".backup/.magisk",
    b".magisk_backup_",
    b"init.magisk.rc",
    b"overlay/init.magisk.rc",
    b"magiskinit",
    b"magiskboot",
    b"MAGISK",          # broad — Magisk binary banners / version strings
]

# Conflicting / non-Magisk root frameworks worth surfacing.
OTHER_ROOT_MARKERS = {
    b"sbin/launch_daemonsu.sh": "SuperSU",
    b"init.xposed.rc": "Xposed",
}


@dataclass
class BootImageInfo:
    path: Path
    size: int
    is_boot_image: bool                  # ANDROID! header present
    image_kind: str = "unknown"          # "android-boot" / "android-vendor-boot" / "unknown"
    magisk_patched: bool = False
    magisk_markers_found: list[str] = field(default_factory=list)
    other_root: list[str] = field(default_factory=list)  # e.g. ["SuperSU"]
    has_vbmeta: bool = False
    header_version: int | None = None
    # Friendly summary text, e.g. "Magisk-patched (markers: MAGISK, magiskinit)"
    summary: str = ""

    @property
    def looks_stock(self) -> bool:
        return self.is_boot_image and not self.magisk_patched and not self.other_root


def analyze(path: Path, scan_limit: int = 64 * 1024 * 1024) -> BootImageInfo:
    """Read the file (up to `scan_limit` bytes) and report what we can see.

    Boot images are usually <100 MiB so scan_limit defaults to 64 MiB, which
    is enough for almost every device while keeping the scan fast.
    """
    info = BootImageInfo(path=path, size=path.stat().st_size, is_boot_image=False)
    with path.open("rb") as f:
        header = f.read(min(2048, info.size))
        # Re-read up to scan_limit from start (header is the first 2k of body).
        f.seek(0)
        body = f.read(min(info.size, scan_limit))

    # --- Magic / kind --------------------------------------------------------
    if header.startswith(ANDROID_BOOT_MAGIC):
        info.is_boot_image = True
        info.image_kind = "android-boot"
        # header_version lives at offset 40 (little-endian uint32) in v3+;
        # in v0/v1/v2 that location holds os_version. Treat <= 4 as the new
        # versioned format, else "0".
        try:
            import struct
            (raw_version,) = struct.unpack_from("<I", header, 40)
            info.header_version = raw_version if 0 <= raw_version <= 4 else 0
        except Exception:
            info.header_version = None
    elif b"VNDRBOOT" in header[:64]:
        info.is_boot_image = True
        info.image_kind = "android-vendor-boot"

    # --- VBMeta presence -----------------------------------------------------
    info.has_vbmeta = (VBMETA_MAGIC in body)

    # --- Magisk markers ------------------------------------------------------
    found: list[str] = []
    for m in MAGISK_MARKERS:
        if m in body:
            found.append(m.decode(errors="replace"))
    info.magisk_markers_found = found
    info.magisk_patched = bool(found)

    # --- Other root frameworks ----------------------------------------------
    other: list[str] = []
    for marker, name in OTHER_ROOT_MARKERS.items():
        if marker in body:
            other.append(name)
    info.other_root = other

    # --- Summary line --------------------------------------------------------
    parts = []
    if not info.is_boot_image:
        info.summary = "Not an Android boot/recovery image"
    else:
        kind_label = {"android-boot": "Android boot image",
                      "android-vendor-boot": "Android vendor-boot image"}.get(
                          info.image_kind, "Unknown image")
        parts.append(kind_label)
        if info.header_version is not None and info.image_kind == "android-boot":
            parts.append(f"header v{info.header_version}")
        if info.magisk_patched:
            parts.append(f"Magisk-patched ({', '.join(found[:3])}"
                         f"{'…' if len(found) > 3 else ''})")
        elif info.other_root:
            parts.append("other root: " + ", ".join(info.other_root))
        else:
            parts.append("looks stock")
        if info.has_vbmeta:
            parts.append("AVB/vbmeta present")
        info.summary = " · ".join(parts)

    return info
