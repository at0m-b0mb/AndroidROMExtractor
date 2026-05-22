<div align="center">

# Android ROM Extractor

**A modern, scriptable toolkit for backing up and flashing Android phones.**
A polished dark-mode GUI and a fully-featured CLI on top of one shared engine.

```
arom-gui       arom backup       arom restore      arom verify
   GUI            extract             flash            checksum
```

Streamed `dd`-over-ADB, SHA-256 verification, fastboot flashing,
adb sideload, partition discovery, live logcat, and a device-properties browser —
all in one tool, designed for tinkerers who want raw images and reproducible backups.

</div>

---

## Highlights

- **Streamed backup** — `dd` over `adb exec-out` straight into a host file. No staging on `/sdcard`, no temporary `tmp` files on the device.
- **SHA-256 on host and device** — every image is hashed in-flight on the Mac and cross-checked against an on-device `sha256sum`, so silent corruption is caught immediately.
- **JSON manifest** — every backup writes `manifest.json` with sizes, hashes, device properties, fingerprints, and timestamps.
- **Restore from manifest** — re-flash a whole backup directory in one command, with safety guards.
- **Beautiful GUI** — sidebar navigation, status pill, toast notifications, confirm dialogs, live progress + ETA + transfer speed, search/filter, recent backups, partition tags.
- **MediaTek-aware** — recognizes MTK platforms and flags `nvram`, `nvdata`, `proinfo`, `protect_f`, `protect_s` as critical (loss = no IMEI / no Wi-Fi MAC / no calibration).
- **Safety rails** — refuses to flash `preloader`, `lk`, `tee` etc. unless you opt in; destructive operations require typing the partition name to confirm.
- **Live logcat** with level filtering, substring search, save-to-file.
- **Device properties browser** — searchable view of every `ro.*` system property.
- **Persistent settings** — last output dir, auto-verify toggle, recent backups, window geometry are all remembered between sessions.

---

## Requirements

- macOS, Linux, or Windows
- Python 3.9+
- `adb` and `fastboot` on `$PATH`
  - macOS: `brew install android-platform-tools`
  - Linux: `apt install android-tools-adb android-tools-fastboot` (or distro equivalent)
- A device with USB debugging enabled, and
  - **root** for full partition backup, **or**
  - an unlocked bootloader for fastboot-side dumps

---

## Install

```bash
git clone <repo-url>
cd AndroidROMExtractor
pip install -e .
```

That installs two commands:

| Command   | Purpose |
|-----------|---------|
| `arom`    | The CLI |
| `arom-gui`| The GUI |

---

## The GUI — `arom-gui`

```
┌──────────────────┬─────────────────────────────────────────────────────┐
│ ◆ ROM Extractor  │  ⤓ Backup                                           │
│   v0.1.0         │  Stream partition images from your phone to this Mac│
│                  │  ─────────────────────────────────────────────────  │
│ ● Connected      │  ┌─ Output directory ──────────────────────────┐    │
│ Galaxy A50       │  │ ~/arom-backups/galaxy-a50-2026-05-21        │    │
│ MT6750 • MTK     │  │ [✓] Auto-verify backup when complete        │    │
│ [ ↻ Refresh ⌘R ] │  └─────────────────────────────────────────────┘    │
│                  │  ┌─ Partitions ──────────────────────────────  ┐    │
│ NAVIGATION       │  │ [Default] [All] [None]    [search……    ]    │    │
│ ▸ ⤓ Backup       │  │ ☑ boot       64 MiB                         │    │
│   ⚡ Flash       │  │ ☑ recovery   64 MiB                         │    │
│   ↩ Restore      │  │ ☑ system     3 GiB                          │    │
│   ✓ Verify       │  │ ☐ userdata   56 GiB              [LARGE]    │    │
│   ⇪ Sideload     │  │ ☑ nvram      5 MiB               [MTK]      │    │
│   ≡ Logcat       │  │ ☐ preloader  1 MiB            [DANGER]      │    │
│   ⓘ Properties   │  └─────────────────────────────────────────────┘    │
│   ⎘ Logs         │  ┌─ 12 partitions selected · ~4.2 GiB ──────────┐   │
│                  │  │                              [Start backup →]│   │
│ POWER            │  └──────────────────────────────────────────────┘   │
│ ⏻ System         │                                                     │
│ ⏻ Bootloader     │  ┌─ Backing up system                      63% ─┐   │
│ ⏻ Recovery       │  │ 2.6 GiB / 4.2 GiB                             │   │
│ ⏻ Sideload       │  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░                │   │
│                  │  │ 18.4 MiB/s · ETA 01:27 · elapsed 02:13 [Cancel]│  │
│ ⚠ Verify backups │  └────────────────────────────────────────────────┘ │
│   before flash   │                                                     │
│                  │                                                     │
│             About├──────────────────────────────────────────────────── │
└──────────────────┴─────────────────────────────────────────────────────┘
 ● Backing up system…   18.4 MiB/s · ETA 01:27                  Logs (8)
```

### Views

| View       | What it does |
|------------|-------------|
| **Backup**     | Pick an output dir, select partitions (Default / All / None + search), watch live progress with speed + ETA, optionally auto-verify when done. Disk-space check fires before any write. |
| **Flash**      | Single-image flash via fastboot. Image picker computes SHA-256 in a background thread; partition combobox auto-fills from the device. Boot-only mode for testing custom recoveries without writing. |
| **Restore**    | Re-flash an entire backup directory using its manifest. "Recent ▾" picker shows the last 8 backups. Requires typing `RESTORE` to confirm. |
| **Verify**     | Re-hash every file in a backup and compare to the manifest. One-click; per-file ✓/✕ readout. |
| **Sideload**   | adb sideload a ZIP into recovery. SHA-256 in background. |
| **Logcat**     | Live `adb logcat -v threadtime` with level filter (V/D/I/W/E/F), substring search, clear, and save-to-file. |
| **Properties** | Searchable browser of every device system property (`ro.*`, `persist.*`, etc). Copy-as-JSON. |
| **Logs**       | Every internal event from the session — backup events, fastboot output, errors. |

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `⌘1` … `⌘8` | Switch to the corresponding view |
| `⌘R`        | Refresh devices |
| `Enter`     | Confirm in dialogs |
| `Esc`       | Cancel dialogs |

On Linux / Windows substitute `Ctrl` for `⌘`.

### Safety features

The GUI doesn't pretend dangerous things are safe:

- "Dangerous partitions" (`preloader`, `lk`, `tee*`, `efuse`, …) are excluded from "Select all". Tag them red.
- Flashing **any** partition requires typing the partition name verbatim into a modal.
- Restoring a backup requires typing `RESTORE`.
- MediaTek-critical partitions (`nvram`, `nvdata`, `proinfo`, `protect_f`, `protect_s`) are tagged violet, encouraging you to back them up before any flash.
- A disk-space precheck warns if the target volume can't hold the backup with 20% headroom.
- Long backups can be **cancelled** mid-stream; the `dd` subprocess is killed and the partial image is left on disk for inspection.

---

## The CLI — `arom`

The CLI does everything the GUI does, scriptable.

```bash
# Devices
arom devices                                # adb + fastboot, one table
arom partitions                             # enumerate by-name partitions

# Backup
arom backup -o ./backup-$(date +%F) \
    --partitions boot,recovery,system,vendor,nvram

# Same, everything the device exposes
arom backup -o ./full --all

# Verify any backup
arom verify ./backup-2026-05-21

# Flash a single image
arom flash --image ./boot.img --partition boot

# Test-boot an image without writing it (great for custom recoveries)
arom flash --image ./twrp.img --partition boot --boot-only

# Re-flash an entire backup
arom restore ./backup-2026-05-21

# OTA-style ZIP via recovery sideload
arom sideload ./ota.zip

# Reboot helpers
arom reboot bootloader
arom reboot recovery
arom reboot sideload
arom reboot system
```

Every destructive command accepts `--dry-run` to print the exact `adb`/`fastboot` commands without running them.

---

## How a backup works

```
                  Host (Mac)                                    Device (rooted)
┌──────────────────────────────┐                       ┌─────────────────────────────┐
│ arom backup                  │                       │                             │
│                              │                       │                             │
│  ┌────────────────────────┐  │  adb exec-out         │  su -c "dd if=/dev/block/   │
│  │ Popen + read stdout    │◀─┼─────────────────────  │      by-name/boot bs=1M"    │
│  │   chunk by chunk        │  │   (raw binary pipe)  │                             │
│  │   write to boot.img     │  │                      │                             │
│  │   update SHA-256        │  │                      │                             │
│  └────────────┬───────────┘  │                       │                             │
│               │              │                       │                             │
│               ▼              │                       │                             │
│  manifest.json:              │                       │                             │
│   { "boot": {                │  adb shell            │  sha256sum                  │
│     "size_bytes": 67108864,  │─────────────────────▶ │   /dev/block/by-name/boot   │
│     "sha256": "ab12…",       │                       │                             │
│     "sha256_on_device": "ab… │◀───── compare ───────│                             │
│   }, … }                     │                       │                             │
└──────────────────────────────┘                       └─────────────────────────────┘
```

- **No staging.** Data never lands on `/sdcard` or in `/tmp` on the phone. It pipes straight from the block device to the host file.
- **`adb exec-out`**, not `adb shell`. The former is a binary-clean pipe; the latter mangles bytes with TTY line-mode translation.
- **Hash twice.** Once host-side as bytes stream past; once device-side via `sha256sum` of the block device. If they disagree, the image is corrupt and the manifest records both for forensics.

---

## Manifest format

`manifest.json` is the single source of truth for any backup. Example:

```json
{
  "tool": "android-rom-extractor",
  "tool_version": "0.1.0",
  "created_at": "2026-05-21T18:42:13.108Z",
  "host": { "system": "Darwin", "release": "25.5.0", "python": "3.13.1" },
  "device": {
    "serial": "ABCD1234XYZ",
    "model": "Galaxy A50",
    "fingerprint": "samsung/a50/A505FN:9/abc/build:user/release-keys",
    "chipset": "mt6750",
    "is_mediatek": true,
    "properties": { "ro.product.model": "Galaxy A50", "...": "..." }
  },
  "partitions": [
    {
      "name": "boot",
      "block_path": "/dev/block/by-name/boot",
      "size_bytes": 67108864,
      "file": "boot.img",
      "sha256": "ab12…",
      "sha256_on_device": "ab12…",
      "started_at":  "2026-05-21T18:42:13.108Z",
      "finished_at": "2026-05-21T18:42:18.992Z"
    }
  ]
}
```

---

## Safety notes — read before flashing

- **Always** back up `nvram`, `nvdata`, `proinfo` (and `protect_f`/`protect_s` if present) on MediaTek devices *before any flash*. Losing them = no IMEI, no Wi-Fi MAC, no sensor calibration, and no easy way back.
- `userdata` is **not** flashed by default during `restore` — it's huge and rarely what you want. Use `--include-userdata` if you really mean it.
- This tool does not unlock bootloaders. Do that yourself with vendor tools, knowing what you're doing.
- A bad flash to `preloader`, `tee`, `lk`, or `efuse` on MTK can hard-brick a device. The tool refuses these by default; you have to pass `--i-know-what-im-doing` (CLI) or toggle the danger switch (GUI).
- The author is not responsible for bricked devices.

---

## Project layout

```
rom_extractor/
├── __init__.py
├── __main__.py        - `python -m rom_extractor` entry
├── adb.py             - `adb` wrapper
├── backup.py          - streamed dd-over-adb, hashing, cancellation
├── cli.py             - click-based CLI
├── device.py          - device discovery, getprop, MTK detection
├── fastboot.py        - `fastboot` wrapper
├── flash.py           - flash, sideload, restore-from-manifest
├── gui.py             - customtkinter desktop app
├── manifest.py        - JSON manifest reader/writer
├── partitions.py      - partition enumeration, danger lists
├── settings.py        - persistent user preferences
├── utils.py           - logging, sha256, sizes, errors
└── verify.py          - manifest-based verification
```

---

## Settings

The GUI persists state at `~/.config/arom/settings.json` (or `$XDG_CONFIG_HOME/arom/settings.json`). It tracks:

- Last output directory (used as the file-picker initial path next time)
- Last backup directory (for restore/verify)
- Recent backups (last 8 paths, surfaced via the **Recent ▾** picker)
- Auto-verify-after-backup toggle
- Default partition selection
- Window geometry

Delete the file to reset.

---

## FAQ

**Q. Does this work without root?**
A. Backing up partitions over `adb` requires root (`su -c dd`). Without root, you can still use the Flash and Sideload features over fastboot/recovery, and the Properties browser.

**Q. Can I back up `userdata`?**
A. Yes, but think hard. It includes every app's private data, is encrypted with your lockscreen secret, and is hostile to flash back unless to the same device with the same key.

**Q. Can I flash an A/B device with this?**
A. Yes — the partition enumerator picks up `_a` / `_b` suffixes from `/dev/block/by-name/`. Pick the right slot.

**Q. What about LineageOS / GSI / custom ROMs?**
A. Out of scope — this tool moves images, it doesn't build them. But you can use `arom sideload` to push their ZIPs through recovery.

**Q. Where do screenshots come from for the README?**
A. They don't yet — fork it, take some, send a PR.

---

## License

MIT. Use at your own risk.
