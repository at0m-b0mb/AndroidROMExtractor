"""Command-line interface for android-rom-extractor."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click
from rich.table import Table

from . import __version__, adb, backup as backup_mod, device as device_mod
from . import fastboot
from . import flash as flash_mod
from . import partitions as part_mod
from . import verify as verify_mod
from .manifest import Manifest, MANIFEST_FILENAME
from .partitions import DEFAULT_BACKUP_SET, MTK_CRITICAL
from .utils import console, human_size, setup_logging

log = logging.getLogger(__name__)


def _pick_device(serial: Optional[str]) -> device_mod.Device:
    devices = device_mod.discover()
    return device_mod.pick_one(devices, serial)


class _NiceErrorGroup(click.Group):
    """Top-level group that prints RuntimeError / FileNotFoundError as one-liners.

    Click's own control-flow exceptions (UsageError, Exit from --help, Abort)
    must be re-raised so the framework handles them normally.
    """

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except (click.exceptions.ClickException,
                click.exceptions.Exit,
                click.exceptions.Abort):
            raise
        except (RuntimeError, FileNotFoundError) as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)


@click.group(cls=_NiceErrorGroup,
             help="Extract and flash full ROM backups for Android devices.")
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging (debug).")
@click.version_option(__version__, prog_name="arom")
def main(verbose: bool) -> None:
    setup_logging(verbose=verbose)


# ----------------------------------------------------------------------------
# devices
# ----------------------------------------------------------------------------

@main.command(help="List attached devices (adb + fastboot).")
def devices() -> None:
    devs = device_mod.discover()
    if not devs:
        console.print("[yellow]No devices attached.[/yellow]")
        return

    table = Table(title="Attached devices")
    table.add_column("Serial", style="cyan")
    table.add_column("State")
    table.add_column("Model")
    table.add_column("Fingerprint", overflow="fold")
    table.add_column("Root")
    table.add_column("Chipset")

    for d in devs:
        table.add_row(
            d.serial,
            d.state,
            d.model,
            d.fingerprint,
            "[green]yes[/green]" if d.rooted else "[red]no[/red]",
            d.chipset + (" (MTK)" if d.is_mediatek else ""),
        )
    console.print(table)


# ----------------------------------------------------------------------------
# partitions
# ----------------------------------------------------------------------------

@main.command(name="partitions", help="List partitions on the device.")
@click.option("--serial", "-s", help="Device serial (omit if only one device).")
def list_partitions_cmd(serial: Optional[str]) -> None:
    dev = _pick_device(serial)
    if dev.state != "device":
        console.print(f"[red]Device must be in adb mode, currently {dev.state}.[/red]")
        sys.exit(1)
    if not dev.rooted:
        console.print(
            "[red]Device is not rooted; cannot read /dev/block/by-name/.[/red]\n"
            "Root is required to enumerate and dump partitions."
        )
        sys.exit(1)

    parts = part_mod.list_partitions(serial=dev.serial)
    if not parts:
        console.print("[yellow]No partitions found.[/yellow]")
        return

    table = Table(title=f"Partitions on {dev.model} ({dev.serial})")
    table.add_column("Name", style="cyan")
    table.add_column("Block path")
    table.add_column("Size", justify="right")
    table.add_column("Notes")

    for p in parts:
        notes = []
        if part_mod.is_dangerous(p.name):
            notes.append("[red]DANGEROUS[/red]")
        if p.name in MTK_CRITICAL:
            notes.append("[magenta]MTK-critical[/magenta]")
        if p.name in ("userdata", "data"):
            notes.append("[yellow]large[/yellow]")
        table.add_row(
            p.name,
            p.block_path,
            human_size(p.size_bytes) if p.size_bytes else "?",
            ", ".join(notes),
        )
    console.print(table)


# ----------------------------------------------------------------------------
# backup
# ----------------------------------------------------------------------------

@main.command(help="Back up partitions to a directory (with manifest + sha256).")
@click.option("--out", "-o", "out_dir", required=True, type=click.Path(path_type=Path),
              help="Output directory.")
@click.option("--partitions", "-p", "parts_arg",
              help="Comma-separated partition names. Default: a sensible standard set.")
@click.option("--all", "all_parts", is_flag=True,
              help="Back up every partition exposed by /dev/block/by-name/.")
@click.option("--serial", "-s", help="Device serial.")
@click.option("--no-on-device-verify", is_flag=True,
              help="Skip the on-device sha256 cross-check (faster).")
@click.option("--gzip", "gzip_compress", is_flag=True,
              help="Compress each image on the host with gzip "
                   "(roughly halves disk usage; hashes still match on-device).")
@click.option("--dry-run", is_flag=True,
              help="Print what would happen without doing it.")
def backup(out_dir: Path, parts_arg: Optional[str], all_parts: bool,
           serial: Optional[str], no_on_device_verify: bool,
           gzip_compress: bool, dry_run: bool) -> None:
    dev = _pick_device(serial)
    if dev.state != "device":
        console.print(f"[red]Device must be in adb mode, currently {dev.state}.[/red]")
        sys.exit(1)
    if not dev.rooted:
        console.print("[red]Root required for partition backup.[/red]")
        sys.exit(1)

    parts = part_mod.list_partitions(serial=dev.serial)
    if not parts:
        console.print("[red]Could not enumerate partitions.[/red]")
        sys.exit(1)

    if all_parts:
        selected = parts
    else:
        want = set(parts_arg.split(",")) if parts_arg else set(DEFAULT_BACKUP_SET)
        selected = [p for p in parts if p.name in want]
        missing = want - {p.name for p in selected}
        if missing:
            console.print(f"[dim]Not present on device, skipping: "
                          f"{', '.join(sorted(missing))}[/dim]")

    if not selected:
        console.print("[red]Nothing to back up.[/red]")
        sys.exit(1)

    total = sum(p.size_bytes or 0 for p in selected)
    console.print(
        f"[bold]Backing up {len(selected)} partitions[/bold] "
        f"(~{human_size(total)}) -> {out_dir}"
    )

    manifest = Manifest.new(device_info={
        "serial": dev.serial,
        "model": dev.model,
        "fingerprint": dev.fingerprint,
        "chipset": dev.chipset,
        "is_mediatek": dev.is_mediatek,
        "properties": dev.properties,
    })

    entries = backup_mod.backup_partitions(
        device=dev,
        parts=selected,
        out_dir=out_dir,
        verify_on_device=not no_on_device_verify,
        dry_run=dry_run,
        compress=gzip_compress,
    )

    if dry_run:
        return

    manifest.partitions = entries
    manifest.write(out_dir / MANIFEST_FILENAME)
    console.print(f"[green]Wrote manifest:[/green] {out_dir / MANIFEST_FILENAME}")


# ----------------------------------------------------------------------------
# verify
# ----------------------------------------------------------------------------

@main.command(help="Verify a backup directory against its manifest.")
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False,
                                              path_type=Path))
def verify(backup_dir: Path) -> None:
    ok = verify_mod.verify_backup(backup_dir)
    if not ok:
        console.print("[red]Verification FAILED.[/red]")
        sys.exit(1)
    console.print("[green]Verification OK.[/green]")


# ----------------------------------------------------------------------------
# flash
# ----------------------------------------------------------------------------

@main.command(help="Flash one image to a partition via fastboot.")
@click.option("--image", "-i", required=True, type=click.Path(exists=True,
              dir_okay=False, path_type=Path))
@click.option("--partition", "-p", required=True, help="Target partition (e.g. boot).")
@click.option("--boot-only", is_flag=True,
              help="Boot the image without writing it to flash.")
@click.option("--serial", "-s", help="Device serial.")
@click.option("--i-know-what-im-doing", "force", is_flag=True,
              help="Override the dangerous-partition refusal.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip confirmation.")
@click.option("--dry-run", is_flag=True)
def flash(image: Path, partition: str, boot_only: bool, serial: Optional[str],
          force: bool, assume_yes: bool, dry_run: bool) -> None:
    flash_mod.flash_image(
        partition=partition, image=image, serial=serial,
        boot_only=boot_only, dry_run=dry_run, force=force, assume_yes=assume_yes,
    )


# ----------------------------------------------------------------------------
# sideload
# ----------------------------------------------------------------------------

@main.command(help="adb sideload a ZIP (device must already be in sideload mode).")
@click.argument("zip_path", type=click.Path(exists=True, dir_okay=False,
                                            path_type=Path))
@click.option("--serial", "-s", help="Device serial.")
@click.option("--yes", "-y", "assume_yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
def sideload(zip_path: Path, serial: Optional[str],
             assume_yes: bool, dry_run: bool) -> None:
    flash_mod.sideload_zip(zip_path, serial=serial,
                           dry_run=dry_run, assume_yes=assume_yes)


# ----------------------------------------------------------------------------
# restore
# ----------------------------------------------------------------------------

@main.command(help="Restore a backup directory by re-flashing every image.")
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False,
                                              path_type=Path))
@click.option("--only", help="Comma-separated partitions to flash (default: all in manifest).")
@click.option("--skip", help="Comma-separated partitions to skip.")
@click.option("--include-userdata", is_flag=True,
              help="Also flash userdata (off by default).")
@click.option("--i-know-what-im-doing", "force", is_flag=True,
              help="Allow flashing dangerous partitions.")
@click.option("--serial", "-s", help="Device serial.")
@click.option("--yes", "-y", "assume_yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
def restore(backup_dir: Path, only: Optional[str], skip: Optional[str],
            include_userdata: bool, force: bool, serial: Optional[str],
            assume_yes: bool, dry_run: bool) -> None:
    flash_mod.restore_backup(
        backup_dir=backup_dir,
        serial=serial,
        include=only.split(",") if only else None,
        exclude=skip.split(",") if skip else None,
        include_userdata=include_userdata,
        force=force,
        dry_run=dry_run,
        assume_yes=assume_yes,
    )


# ----------------------------------------------------------------------------
# reboot helpers
# ----------------------------------------------------------------------------

@main.command(help="Reboot device to a given target.")
@click.argument("target", type=click.Choice(["system", "bootloader", "recovery",
                                             "sideload", "fastboot"]))
@click.option("--serial", "-s")
def reboot(target: str, serial: Optional[str]) -> None:
    """Dispatch by current device state: fastboot binary for fastboot mode,
    adb binary otherwise. Fails clearly when the requested target isn't
    reachable from the current state."""
    dev = _pick_device(serial)
    target_arg = None if target == "system" else target

    if dev.state == "fastboot":
        if target == "sideload":
            console.print(
                "[red]Sideload isn't reachable from fastboot.[/red] "
                "Reboot to recovery first, then enter sideload from there."
            )
            sys.exit(1)
        fastboot.reboot(target_arg, serial=dev.serial)
    elif dev.state in ("device", "recovery", "sideload"):
        if target == "fastboot":
            # adb has no `reboot fastboot` target — use bootloader.
            target_arg = "bootloader"
        adb.reboot(target_arg, serial=dev.serial)
    else:
        console.print(
            f"[red]Cannot reboot:[/red] device is {dev.state!r}. "
            "Accept the USB-debug prompt on the phone, or replug it."
        )
        sys.exit(1)

    console.print(f"[green]Reboot to {target} issued.[/green]")


if __name__ == "__main__":
    main()
