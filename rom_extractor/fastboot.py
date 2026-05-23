"""Thin wrapper around the `fastboot` binary."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .utils import CommandError

log = logging.getLogger(__name__)


def _fastboot_binary() -> str:
    path = shutil.which("fastboot")
    if not path:
        raise RuntimeError(
            "`fastboot` not found on PATH. Install Android platform-tools."
        )
    return path


def run(args: list[str], capture: bool = True, check: bool = True,
        serial: Optional[str] = None,
        timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    cmd = [_fastboot_binary()]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    log.debug("$ %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, (proc.stderr or "") + (proc.stdout or ""))
    return proc


def list_devices() -> list[str]:
    """Return list of serials in fastboot mode."""
    proc = run(["devices"])
    serials = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "fastboot":
            serials.append(parts[0])
    return serials


def getvar(name: str, serial: Optional[str] = None) -> str:
    """`fastboot getvar <name>` — value is on stderr in older fastboot."""
    proc = run(["getvar", name], serial=serial, check=False)
    # fastboot prints `name: value` on stderr historically.
    blob = (proc.stderr or "") + (proc.stdout or "")
    for line in blob.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


def flash(partition: str, image: Path, serial: Optional[str] = None) -> None:
    run(["flash", partition, str(image)], serial=serial, capture=False)


def boot(image: Path, serial: Optional[str] = None) -> None:
    """Boot an image without flashing it (useful for test-driving a recovery)."""
    run(["boot", str(image)], serial=serial, capture=False)


def reboot(target: Optional[str] = None, serial: Optional[str] = None) -> None:
    """Reboot from fastboot. `target` can be None (=system), 'bootloader',
    'fastboot' (fastbootd), or 'recovery'. Sideload is not reachable from
    fastboot directly — boot to recovery first."""
    args = ["reboot"]
    if target:
        args.append(target)
    run(args, serial=serial, capture=False)
