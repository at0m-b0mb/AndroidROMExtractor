"""List installed APKs on a device and pull them via ADB.

Works on unrooted devices for user-installed packages; root may be needed
for some system packages depending on Android version and SELinux policy.
"""
from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import adb
from .utils import CommandError

log = logging.getLogger(__name__)


@dataclass
class AppPackage:
    package: str
    paths: list[str]  # APK paths on-device (multiple for split APKs)
    is_system: bool

    @property
    def primary_path(self) -> Optional[str]:
        return self.paths[0] if self.paths else None


def list_packages(serial: Optional[str] = None,
                  include_system: bool = False) -> list[AppPackage]:
    """List installed packages. Returns AppPackage objects with full APK paths."""
    flags = "-f"  # include APK paths
    if not include_system:
        flags += " -3"  # third-party only

    out = adb.shell(f"pm list packages {flags}", serial=serial, check=False)
    by_pkg: dict[str, list[str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        # Format: package:/data/app/.../base.apk=com.example
        body = line[len("package:"):]
        m = re.match(r"^(.+?)=(.+)$", body)
        if not m:
            continue
        path, pkg = m.group(1), m.group(2)
        by_pkg.setdefault(pkg, []).append(path)

    # If we want extra paths for split APKs, query pm path; pm list -f
    # already gives the base path so this is usually unnecessary.
    packages: list[AppPackage] = []
    for pkg, paths in sorted(by_pkg.items()):
        is_system = any(p.startswith("/system") or p.startswith("/product")
                        or p.startswith("/system_ext") or p.startswith("/vendor")
                        for p in paths)
        packages.append(AppPackage(package=pkg, paths=paths, is_system=is_system))
    return packages


def split_apk_paths(package: str, serial: Optional[str] = None) -> list[str]:
    """Return all APK paths for `package` (handles split APKs)."""
    out = adb.shell(f"pm path {shlex.quote(package)}",
                    serial=serial, check=False)
    paths: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            paths.append(line[len("package:"):])
    return paths


def pull_apk(package: str, out_dir: Path,
             serial: Optional[str] = None) -> list[Path]:
    """Pull every APK that belongs to `package` into out_dir. Returns local paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = split_apk_paths(package, serial=serial)
    if not paths:
        raise RuntimeError(f"No APK paths found for {package!r}.")

    pulled: list[Path] = []
    for i, path in enumerate(paths):
        # Name files like com.example.base.apk / com.example.split_config.en.apk
        leaf = Path(path).name
        # Prefix with package so unrelated splits don't collide across pulls.
        local_name = f"{package}.{leaf}" if not leaf.startswith(package) else leaf
        local = out_dir / local_name
        args = ["pull", path, str(local)]
        adb.run(args, serial=serial)
        pulled.append(local)
    return pulled
