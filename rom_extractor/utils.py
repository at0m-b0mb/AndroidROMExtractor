"""Shared helpers: logging, sizes, hashing."""
from __future__ import annotations

import hashlib
import logging
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


class CommandError(RuntimeError):
    """Raised when an external command (adb/fastboot) fails."""

    def __init__(self, cmd: list[str], code: int, stderr: Optional[str] = None):
        self.cmd = cmd
        self.code = code
        self.stderr = stderr or ""
        super().__init__(
            f"command failed (exit {code}): {' '.join(cmd)}\n{self.stderr}"
        )
