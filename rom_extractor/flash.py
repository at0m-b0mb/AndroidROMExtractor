"""Flashing logic: fastboot flash, fastboot boot, adb sideload, manifest restore."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from . import adb, fastboot, partitions
from .manifest import Manifest, MANIFEST_FILENAME
from .partitions import is_dangerous
from .utils import CommandError, console, confirm, human_size, sha256_file

log = logging.getLogger(__name__)


def _ensure_fastboot(serial: Optional[str], timeout: float = 30.0) -> str:
    """Wait until a device shows up in fastboot mode. Returns the serial used."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        serials = fastboot.list_devices()
        if serials:
            if serial and serial in serials:
                return serial
            return serials[0]
        time.sleep(1)
    raise RuntimeError("No device in fastboot mode (waited 30s).")


def flash_image(
    partition: str,
    image: Path,
    serial: Optional[str] = None,
    boot_only: bool = False,
    dry_run: bool = False,
    force: bool = False,
    assume_yes: bool = False,
) -> None:
    """Flash a single image to a partition (or boot it without flashing)."""
    if not image.exists():
        raise FileNotFoundError(image)

    if is_dangerous(partition) and not force:
        raise RuntimeError(
            f"Refusing to flash {partition!r} — listed as dangerous. "
            f"Pass --i-know-what-im-doing to override."
        )

    sha = sha256_file(image)
    console.print(
        f"[bold]Image:[/bold]     {image}\n"
        f"[bold]Size:[/bold]      {human_size(image.stat().st_size)}\n"
        f"[bold]SHA-256:[/bold]   {sha}\n"
        f"[bold]Target:[/bold]    {'BOOT (no flash)' if boot_only else f'flash {partition}'}"
    )

    if not confirm(
        f"Proceed with {'booting' if boot_only else f'flashing'} this image?",
        assume_yes=assume_yes,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return

    if dry_run:
        op = "boot" if boot_only else f"flash {partition}"
        console.print(f"[dim]DRY[/dim] fastboot {op} {image}")
        return

    fb_serial = _ensure_fastboot(serial)
    if boot_only:
        fastboot.boot(image, serial=fb_serial)
    else:
        fastboot.flash(partition, image, serial=fb_serial)


def sideload_zip(zip_path: Path, serial: Optional[str] = None,
                 dry_run: bool = False, assume_yes: bool = False) -> None:
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)
    console.print(
        f"[bold]ZIP:[/bold]  {zip_path}\n"
        f"[bold]Size:[/bold] {human_size(zip_path.stat().st_size)}\n"
        f"[bold]SHA-256:[/bold] {sha256_file(zip_path)}\n"
        "[yellow]Device must already be in `adb sideload` mode "
        "(Recovery -> Apply update from ADB).[/yellow]"
    )
    if not confirm("Sideload this ZIP?", assume_yes=assume_yes):
        console.print("[yellow]Aborted.[/yellow]")
        return
    if dry_run:
        console.print(f"[dim]DRY[/dim] adb sideload {zip_path}")
        return
    adb.sideload(zip_path, serial=serial)


def restore_backup(
    backup_dir: Path,
    serial: Optional[str] = None,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    dry_run: bool = False,
    force: bool = False,
    assume_yes: bool = False,
    include_userdata: bool = False,
) -> None:
    """
    Re-flash a backup directory using its manifest.
    By default skips dangerous partitions, `userdata`, and any *_a/_b counterparts
    unless explicitly opted into.
    """
    manifest_path = backup_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json missing in {backup_dir}")

    manifest = Manifest.read(manifest_path)

    include_set = set(include) if include else None
    exclude_set = set(exclude or [])

    queue: list[tuple[str, Path]] = []
    for entry in manifest.partitions:
        name = entry["name"]
        if include_set is not None and name not in include_set:
            continue
        if name in exclude_set:
            continue
        if is_dangerous(name) and not force:
            console.print(f"[yellow]Skipping dangerous partition {name}[/yellow]")
            continue
        if name in ("userdata", "data") and not include_userdata:
            console.print(f"[yellow]Skipping {name} (pass --include-userdata to flash)[/yellow]")
            continue

        img = backup_dir / entry["file"]
        if not img.exists():
            console.print(f"[red]Missing image {img}, skipping {name}[/red]")
            continue

        # Pre-flash hash check.
        actual = sha256_file(img)
        if actual != entry["sha256"]:
            console.print(
                f"[red]REFUSING to flash {name}: hash mismatch with manifest[/red]"
            )
            continue

        queue.append((name, img))

    if not queue:
        console.print("[yellow]Nothing to restore.[/yellow]")
        return

    console.print("[bold]Will flash:[/bold]")
    for name, img in queue:
        console.print(f"  {name:<14} <- {img.name}")

    if not confirm("Restore these partitions?", assume_yes=assume_yes):
        console.print("[yellow]Aborted.[/yellow]")
        return

    if dry_run:
        for name, img in queue:
            console.print(f"[dim]DRY[/dim] fastboot flash {name} {img}")
        return

    fb_serial = _ensure_fastboot(serial)
    for name, img in queue:
        console.print(f"[bold blue]>>[/bold blue] flashing {name}")
        try:
            fastboot.flash(name, img, serial=fb_serial)
        except CommandError as e:
            console.print(f"[red]flash {name} failed:[/red] {e}")
