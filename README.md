# AndroidROMExtractor

A no-frills, scriptable toolkit for **extracting full ROM backups from Android devices** and **flashing them back**. Designed for tinkerers who want raw partition images, deterministic backups with checksums, and a single CLI for both extraction and recovery.

> Works on rooted Android devices over ADB. Flashing happens over Fastboot (and optionally `adb sideload` for OTA-style ZIPs). MediaTek-specific helpers are included.

---

## Features

- **Partition discovery** — auto-enumerates `/dev/block/by-name/*` and reports sizes
- **Full / selective backup** — `boot`, `recovery`, `system`, `vendor`, `userdata`, `persist`, `nvram`, `nvdata`, `proinfo`, `metadata`, `dtbo`, `vbmeta`, etc.
- **Streamed `dd` over ADB** — no need to fit images in `/sdcard`, pipes directly to host
- **SHA-256 verification** — every image is hashed on-device and re-verified on the host
- **JSON manifest** — every backup writes a manifest with sizes, hashes, device props, partition map
- **Flash** — `fastboot flash`, `fastboot boot` (test without flashing), and `adb sideload`
- **Restore from manifest** — re-flash an entire backup with one command
- **MediaTek extras** — preserves `nvram`, `nvdata`, `proinfo`, `protect_f`, `protect_s` (critical for IMEI/keys)
- **Dry-run mode** for every destructive operation

---

## Requirements

- Python 3.9+
- `adb` and `fastboot` on `$PATH` (install via `brew install android-platform-tools` on macOS)
- A device with:
  - USB debugging enabled, and
  - Either **root** (for full partition backup via `dd`) or **an unlocked bootloader** (for fastboot-side dumps of unlocked partitions)

---

## Install

```bash
git clone <this-repo>
cd AndroidROMExtractor
pip install -e .
```

This installs the `arom` command.

---

## Usage

### 1. Detect device & list partitions

```bash
arom devices
arom partitions
```

### 2. Backup

Backup specific partitions:

```bash
arom backup --out ./backup-2026-05-21 --partitions boot,recovery,system,vendor,nvram
```

Backup *everything* the device exposes under `/dev/block/by-name/`:

```bash
arom backup --out ./full-backup --all
```

A manifest at `<out>/manifest.json` is written with SHA-256 of every image plus device properties (`ro.product.model`, `ro.build.fingerprint`, etc.).

### 3. Verify a backup

```bash
arom verify ./full-backup
```

### 4. Flash

Flash a single image:

```bash
arom flash --image ./full-backup/boot.img --partition boot
```

Boot an image *without* flashing (great for testing custom recoveries):

```bash
arom flash --image ./twrp.img --partition boot --boot-only
```

Restore an entire backup directory from manifest:

```bash
arom restore ./full-backup
```

Sideload an OTA-style ZIP via recovery:

```bash
arom sideload ./ota.zip
```

### 5. Dry runs

Append `--dry-run` to any destructive command to see the exact `adb` / `fastboot` commands without executing them.

---

## Safety notes

- **Always backup `nvram`, `nvdata`, `proinfo` on MediaTek devices before flashing.** Losing them destroys your IMEI / Wi-Fi MAC / sensor calibration.
- Restoring `userdata` is opt-in (it's huge and rarely what you want).
- This tool does **not** unlock bootloaders. Do that yourself, with vendor tools, and understand what you're doing.
- A wrong flash to `preloader`, `tee`, or `lk` on MTK can hard-brick. The tool refuses to flash these partitions unless you pass `--i-know-what-im-doing`.

---

## Project layout

```
rom_extractor/
  cli.py         - command-line entry points
  adb.py         - thin wrapper around `adb`
  fastboot.py    - thin wrapper around `fastboot`
  device.py      - device discovery, property/state queries
  partitions.py  - partition enumeration and sizing
  backup.py      - streamed dd-over-adb backup logic
  flash.py       - fastboot/sideload flashing logic
  manifest.py    - JSON manifest read/write + verification
  verify.py      - SHA-256 verification
  utils.py       - shared helpers (logging, sizes, hashing)
```

---

## License

MIT. Use at your own risk. The author is not responsible for bricked devices.
