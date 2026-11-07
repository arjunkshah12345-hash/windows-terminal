# Changelog

## 0.3.0

- `fetch`: download genuine Windows 11 (ARM64 or x64) from Microsoft's Media Creation Tool catalog and CDN,
  SHA-1 verified, and build a bootable ISO with wimlib + xorriso.
- `fetch --setup-only`: range-download only Windows PE + Setup (~0.56 GB of a 4.5 GB ESD) into a sparse
  file, verified chunk by chunk against the ESD integrity table.
- `init --unattended`: answer disc with autounattend.xml (local account, auto-logon, Compact OS, OOBE skips).
- Boot order: disk first via `bootindex` on x86; on ARM, edk2's default order with PXE disabled (ArmVirt drops
  USB CDs it cannot match to a bootindex). Fetched ISOs need no "Press any key"; `up` presses it for other
  ISOs while the disk is blank.
- Fetched ISOs get a `startup.nsh` inside the El Torito FAT image (tiny built-in FAT12 writer), so ARM
  firmware that falls back to the UEFI shell still boots Windows Setup.
- ARM guests use a USB RNDIS NIC (Windows on Arm has no e1000 driver).
- Disks pass guest TRIM through (`discard=unmap`, `detect-zeroes=unmap`).
- Demo re-recorded on genuine Windows 11 ARM64.

## 0.2.0

- `--boot bios` for legacy x86 guests (SeaBIOS, i440FX, IDE): Windows 7 and earlier, ReactOS.
- `up --view` accepts `--protocol` and `--fps`, like `view`.
- `WINDOWS_TERMINAL_ROOT` and `WINDOWS_TERMINAL_VM` environment defaults.
- Checkpoints support BIOS guests (disk + config, no UEFI files).
- Reproducible demo recorder (`demo/record.py`, `demo/render.py`).
- MIT license and CI (unit, live QEMU, release checks).

## 0.1.0

- Persistent QEMU VMs with per-VM UEFI variables and optional TPM 2.0.
- Terminal viewer (Kitty, iTerm2, half-block), keyboard/mouse passthrough, safe paste.
- CLI computer-use commands and a nine-tool MCP server.
- Offline checkpoints restored into new VMs, with SHA256 verification.
