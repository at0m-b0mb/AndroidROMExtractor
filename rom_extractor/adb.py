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


# --- Wireless ADB ------------------------------------------------------------
# Android 11+ requires `adb pair host:pairing_port` with a 6-digit code once
# per host, after which `adb connect host:5555` works. On Android 10 and older
# you USB-attach first, run `adb tcpip 5555`, then `adb connect host:5555`.

def connect(host: str, port: int = 5555) -> str:
    """Connect to a device by IP. Returns adb's status line."""
    proc = run(["connect", f"{host}:{port}"], check=False, timeout=8)
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or "failed" in out.lower() or "cannot" in out.lower():
        raise CommandError(["connect", f"{host}:{port}"], proc.returncode, out)
    return out


def disconnect(host: Optional[str] = None, port: int = 5555) -> str:
    """Disconnect a wireless device. Pass None to disconnect all."""
    args = ["disconnect"]
    if host:
        args.append(f"{host}:{port}")
    proc = run(args, check=False, timeout=8)
    return (proc.stdout or proc.stderr or "").strip()


def pair(host: str, port: int, code: str) -> str:
    """Pair (Android 11+). Code is the 6-digit pairing code shown on-device.
    Pairing port is NOT the connection port — it's the random one shown under
    'Pair device with pairing code' in Wireless debugging settings."""
    if not code.isdigit() or len(code) != 6:
        raise ValueError("Pairing code must be exactly 6 digits.")
    # adb pair reads the code from stdin.
    cmd = [_adb_binary(), "pair", f"{host}:{port}"]
    proc = subprocess.run(
        cmd, input=code + "\n", capture_output=True, text=True, timeout=15)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or "failed" in out.lower() or "failed" in err.lower():
        raise CommandError(cmd, proc.returncode, err or out)
    return out


def tcpip(port: int = 5555, serial: Optional[str] = None) -> str:
    """Restart adbd on the device in TCP/IP mode on `port` (Android <=10 flow)."""
    proc = run(["tcpip", str(port)], serial=serial, check=False, timeout=8)
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        raise CommandError(["tcpip", str(port)], proc.returncode, out)
    return out
