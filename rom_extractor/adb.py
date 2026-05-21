"""Thin wrapper around the `adb` binary."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .utils import CommandError

log = logging.getLogger(__name__)


def _adb_binary() -> str:
    path = shutil.which("adb")
    if not path:
        raise RuntimeError(
            "`adb` not found on PATH. Install Android platform-tools "
            "(macOS: brew install android-platform-tools)."
        )
    return path


def run(args: list[str], capture: bool = True, check: bool = True,
        timeout: Optional[float] = None, serial: Optional[str] = None) -> subprocess.CompletedProcess:
    cmd = [_adb_binary()]
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
        raise CommandError(cmd, proc.returncode, proc.stderr)
    return proc


def shell(cmd: str, *, serial: Optional[str] = None, check: bool = True,
          timeout: Optional[float] = None) -> str:
    """Run a shell command on-device, return stdout."""
    proc = run(["shell", cmd], serial=serial, check=check, timeout=timeout)
    return proc.stdout


def shell_root(cmd: str, *, serial: Optional[str] = None, check: bool = True,
               timeout: Optional[float] = None) -> str:
    """Run a shell command as root on-device via `su -c`."""
    # Wrap in su -c, escaping single quotes in cmd.
    escaped = cmd.replace("'", "'\"'\"'")
    return shell(f"su -c '{escaped}'", serial=serial, check=check, timeout=timeout)


def has_root(serial: Optional[str] = None) -> bool:
    """Return True if `su -c id` reports uid=0."""
    try:
        out = shell_root("id", serial=serial, check=False)
        return "uid=0" in out
    except (CommandError, subprocess.TimeoutExpired):
        return False


def list_devices() -> list[tuple[str, str]]:
    """Return [(serial, state), ...] for all attached devices."""
    proc = run(["devices"])
    devices = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def reboot(target: Optional[str] = None, serial: Optional[str] = None) -> None:
    """Reboot device. `target` can be None, 'bootloader', 'recovery', 'sideload'."""
    args = ["reboot"]
    if target:
        args.append(target)
    run(args, serial=serial)


def sideload(zip_path: Path, serial: Optional[str] = None) -> None:
    run(["sideload", str(zip_path)], serial=serial, capture=False)
