"""Shared helpers: logging, sizes, hashing."""
from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        console.print(f"[yellow]{prompt}[/yellow] [dim](auto-confirmed)[/dim]")
        return True
    answer = console.input(f"[yellow]{prompt}[/yellow] [y/N]: ").strip().lower()
    return answer in ("y", "yes")


_log = logging.getLogger(__name__)


def notify(title: str, message: str, sound: bool = False) -> None:
    """Best-effort native notification. macOS uses osascript; Linux uses
    notify-send if available. Never raises — notifications are nice-to-have.

    Note: macOS Notification Center sometimes silently drops messages from
    osascript if the user has 'Script Editor' notifications disabled. There's
    no reliable way around this without code-signing a helper app."""
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            # Escape double quotes for AppleScript.
            t = title.replace('"', '\\"')
            m = message.replace('"', '\\"')
            script = f'display notification "{m}" with title "{t}"'
            if sound:
                script += ' sound name "Glass"'
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=4,
            )
        elif sys_name == "Linux" and shutil.which("notify-send"):
            args = ["notify-send", title, message]
            if sound:
                args.insert(1, "--urgency=normal")
            subprocess.run(args, capture_output=True, timeout=4)
    except Exception as e:
        # Don't let notification failures break the calling code.
        _log.debug("notify() failed: %s", e)


class CommandError(RuntimeError):
    """Raised when an external command (adb/fastboot) fails."""

    def __init__(self, cmd: list[str], code: int, stderr: Optional[str] = None):
        self.cmd = cmd
        self.code = code
        self.stderr = stderr or ""
        super().__init__(
            f"command failed (exit {code}): {' '.join(cmd)}\n{self.stderr}"
        )
