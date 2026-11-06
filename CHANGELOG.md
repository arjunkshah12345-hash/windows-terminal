# Changelog

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
