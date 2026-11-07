"""Persistent, headless Windows VMs on macOS, Linux, and WSL.

ARM Windows installation media must match aarch64. NVMe uses the guest's
inbox storage driver; ramfb provides a basic
firmware framebuffer, not an accelerated Windows display driver. No host ports
are forwarded, and QMP is exposed only through a private Unix socket.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Iterator
import uuid


@dataclass
class VMConfig:
    name: str = "windows"
    arch: str = field(default_factory=lambda: (
        "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    ))
    memory_mb: int = 4096
    cpus: int = 4
    disk_gb: int = 64
    iso: str | None = None
    firmware: str | None = None
    network: str = "isolated"
    tpm: bool = False
    boot: str = "uefi"


class VMManager:
    """Manage QEMU processes without ever signaling a PID from a config file."""

    def __init__(self, root: Path | None = None):
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        self.root = Path(root if root is not None else data_home / "windows-terminal").expanduser().resolve()
        self._processes: dict[str, subprocess.Popen] = {}
        self._tpm_processes: dict[str, subprocess.Popen] = {}

    @staticmethod
    def _validate(config: VMConfig) -> None:
        if not isinstance(config.name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", config.name):
            raise ValueError("VM name must be 1–48 letters, digits, underscores or hyphens; start with a letter or digit")
        if config.arch not in {"aarch64", "x86_64"}:
            raise ValueError("arch must be aarch64 or x86_64")
        if config.network not in {"isolated", "nat"}:
            raise ValueError("network must be isolated or nat")
        if type(config.tpm) is not bool:
            raise ValueError("tpm must be a boolean")
        if config.boot not in {"uefi", "bios"}:
            raise ValueError("boot must be uefi or bios")
        if config.boot == "bios" and config.arch != "x86_64":
            raise ValueError("Legacy BIOS boot is only available for x86_64 guests; aarch64 always uses UEFI")
        if config.boot == "bios" and config.firmware is not None:
            raise ValueError("A UEFI firmware path cannot be combined with legacy BIOS boot")
        for label, value, minimum, maximum in (
            ("memory_mb", config.memory_mb, 512, 1048576),
            ("cpus", config.cpus, 1, 256),
            ("disk_gb", config.disk_gb, 16, 16384),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")

    def _directory(self, name: str) -> Path:
        self._validate(VMConfig(name=name))
        directory = self.root / name
        if directory.is_symlink():
            raise ValueError("VM directories must not be symbolic links")
        return directory

    @staticmethod
    def _existing_file(value: str | Path, label: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"{label} does not exist or is not a file: {path}")
        return path

    @staticmethod
    def _executable(name: str) -> str:
        executable = shutil.which(name)
        if not executable:
            raise RuntimeError(f"{name} not found. Install QEMU: brew install qemu (macOS), or your Linux distribution's qemu packages.")
        return executable

    @staticmethod
    def _firmware(arch: str, supplied: str | None) -> tuple[Path, Path]:
        """Find a matching CODE/VARS pair, never a shared writable VARS file."""
        roots = [
            Path("/opt/homebrew/share/qemu"), Path("/usr/local/share/qemu"),
            Path("/usr/share/qemu"), Path("/usr/share/OVMF"),
            Path("/usr/share/edk2/ovmf"), Path("/usr/share/edk2/aarch64"),
            Path("/usr/share/AAVMF"), Path("/usr/share/edk2-ovmf/x64"),
        ]
        qemu = shutil.which(f"qemu-system-{arch}")
        if qemu:
            roots.insert(0, Path(qemu).resolve().parent.parent / "share/qemu")
        pairs = (
            [("edk2-aarch64-code.fd", "edk2-arm-vars.fd"),
             ("AAVMF_CODE.fd", "AAVMF_VARS.fd"),
             ("QEMU_EFI-pflash.raw", "vars-template-pflash.raw")]
            if arch == "aarch64" else
            [("edk2-x86_64-code.fd", "edk2-i386-vars.fd"),
             ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
             ("OVMF_CODE.fd", "OVMF_VARS.fd")]
        )
        if supplied:
            code = VMManager._existing_file(supplied, "UEFI firmware")
            vars_names = [variables for filename, variables in pairs if filename == code.name]
            vars_names += [code.name.replace("CODE", "VARS").replace("code", "vars")]
            for variables in dict.fromkeys(vars_names):
                template = code.parent / variables
                if template != code and template.is_file():
                    return code, template.resolve()
            raise ValueError(f"No matching UEFI VARS template beside {code}; supply a standard EDK2/OVMF CODE file and its matching VARS file")
        for directory in roots:
            for filename, variables in pairs:
                code, template = directory / filename, directory / variables
                if code.is_file() and template.is_file():
                    return code.resolve(), template.resolve()
        raise RuntimeError(
            f"No {arch} UEFI CODE/VARS firmware found. Install QEMU firmware "
            "(brew install qemu; Linux: ovmf for x86_64, qemu-efi-aarch64 for ARM), "
            "or pass firmware='/path/to/CODE.fd' with its matching VARS template beside it."
        )

    @staticmethod
    def _write(directory: Path, record: dict) -> None:
        temporary = directory / f".config-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(record, stream, indent=2)
                stream.write("\n")
            temporary.replace(directory / "config.json")
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        directory = self._directory(name)
        with (directory / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def qmp_path(self, name: str) -> Path:
        candidate = self._directory(name) / "qmp.sock"
        if len(os.fsencode(candidate)) <= 100 and "," not in str(candidate):
            return candidate
        runtime = Path(tempfile.gettempdir()) / f"wt-{os.getuid()}"
        if len(os.fsencode(runtime)) + 30 > 100 or "," in str(runtime):
            runtime = Path("/tmp") / f"wt-{os.getuid()}"
        try:
            runtime.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = runtime.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError(f"Unsafe QMP runtime directory: {runtime}")
        os.chmod(runtime, 0o700)
        digest = hashlib.sha256(os.fsencode(candidate)).hexdigest()[:24]
        result = runtime / f"{digest}.sock"
        if len(os.fsencode(result)) > 100:
            raise RuntimeError("Temporary directory is too long for QMP Unix sockets; set TMPDIR to a shorter private path")
        return result

    @staticmethod
    def _copy_firmware(source: Path, destination: Path) -> None:
        with source.open("rb") as original, destination.open("xb") as copied:
            while chunk := original.read(65536):
                if chunk.count(0) == len(chunk):
                    copied.seek(len(chunk), os.SEEK_CUR)
                else:
                    copied.write(chunk)
            copied.truncate()

    def create(self, config: VMConfig) -> dict:
        self._validate(config)
        if config.iso is None:
            raise ValueError("A Windows installation ISO is required; supply iso='/path/to/windows.iso'")
        iso = self._existing_file(config.iso, "Windows installation ISO")
        code, variables = self._firmware(config.arch, config.firmware) if config.boot == "uefi" else (None, None)
        qemu_img = self._executable("qemu-img")
        if config.tpm:
            if "," in str(self.root):
                raise ValueError("TPM storage paths cannot contain commas; choose another --root.")
            self._swtpm_executable()
        directory = self._directory(config.name)
        self.root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        try:
            if code is not None:
                self._copy_firmware(code, directory / "uefi-code.fd")
                self._copy_firmware(variables, directory / "uefi-vars.fd")
            result = subprocess.run(
                [qemu_img, "create", "-f", "qcow2", str(directory / "disk.qcow2"), f"{config.disk_gb}G"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"qemu-img failed: {result.stderr.strip() or result.stdout.strip()}")
            record = asdict(config)
            record.update(iso=str(iso), firmware=str(code) if code else None, state="stopped", pid=None,
                          qmp_socket=str(self.qmp_path(config.name)), uuid=str(uuid.uuid4()))
            self._write(directory, record)
            return record
        except BaseException:
            shutil.rmtree(directory)
            raise

    def load(self, name: str) -> dict:
        path = self._directory(name) / "config.json"
        try:
            with path.open(encoding="utf-8") as stream:
                record = json.load(stream)
            self._validate_record(record, name)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"Invalid VM config {path}: {error}") from None
        record["qmp_socket"] = str(self.qmp_path(name))
        return record

    @staticmethod
    def _validate_record(record, name):
        if not isinstance(record, dict):
            raise ValueError("expected a JSON object")
        required = {"name", "arch", "memory_mb", "cpus", "disk_gb", "iso", "uuid"}
        missing = required - record.keys()
        if missing:
            raise ValueError("missing fields: " + ", ".join(sorted(missing)))
        config = VMConfig(**{key: record[key] for key in VMConfig.__dataclass_fields__ if key in record})
        VMManager._validate(config)
        if config.name != name:
            raise ValueError("VM config name does not match its directory")
        if "boot" in record and not isinstance(record["boot"], str):
            raise ValueError("boot must be uefi or bios")
        for key in ("iso", "firmware"):
            if record.get(key) is not None and not isinstance(record[key], str):
                raise ValueError(f"{key} must be a path string or null")
        if not isinstance(record["uuid"], str):
            raise ValueError("uuid must be a UUID string")
        uuid.UUID(record["uuid"])

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        return [self.status(directory.name) for directory in sorted(self.root.iterdir())
                if directory.is_dir() and not directory.is_symlink() and (directory / "config.json").is_file()]

    @staticmethod
    def _acceleration(arch: str) -> str:
        machine = platform.machine().lower()
        host_arch = "aarch64" if machine in {"arm64", "aarch64"} else machine
        if host_arch == arch and platform.system() == "Darwin":
            return "hvf"
        if host_arch == arch and platform.system() == "Linux" and os.access("/dev/kvm", os.R_OK | os.W_OK):
            return "kvm"
        return "tcg"

    def command(self, name: str) -> list[str]:
        record = self.load(name)
        directory = self._directory(name)
        arch = record["arch"]
        bios = record.get("boot", "uefi") == "bios"
        acceleration = self._acceleration(arch)
        machine = "virt" if arch == "aarch64" else "pc" if bios else "q35"
        command = [self._executable(f"qemu-system-{arch}"), "-name", name,
                   "-uuid", record["uuid"], "-machine", machine,
                   "-accel", acceleration, "-cpu", "host" if acceleration != "tcg" else "max",
                   "-m", str(record["memory_mb"]), "-smp", str(record["cpus"]),
                   "-display", "none", "-monitor", "none", "-serial", "none",
                   "-qmp", f"unix:{self.qmp_path(name)},server=on,wait=off",
                   "-rtc", "base=localtime"]
        for filename, readonly in () if bios else (("uefi-code.fd", True), ("uefi-vars.fd", False)):
            path = self._existing_file(directory / filename, "VM UEFI firmware")
            command += ["-drive", f"if=pflash,format=raw,readonly={'on' if readonly else 'off'},file={self._escape(path)}"]
        disk = self._existing_file(directory / "disk.qcow2", "VM disk")
        # discard/detect-zeroes let guest TRIM shrink the qcow2 instead of only ever growing it.
        command += ["-drive", f"if=none,id=osdisk,format=qcow2,discard=unmap,detect-zeroes=unmap,file={self._escape(disk)}"]
        if arch == "aarch64":
            command += ["-device", "nvme,drive=osdisk,serial=WT0001", "-device", "ramfb",
                        "-device", "qemu-xhci", "-device", "usb-net,netdev=net0",
                        # ArmVirt edk2 cannot translate bootindex paths for USB disks and would drop the CD,
                        # so ARM relies on edk2's default order (NVMe before USB) and disables PXE delays.
                        "-fw_cfg", "name=opt/org.tianocore/IPv4PXESupport,string=n",
                        "-fw_cfg", "name=opt/org.tianocore/IPv6PXESupport,string=n"]
            # Windows on Arm has no e1000 driver; its inbox RNDIS driver binds usb-net.
        elif bios:
            # Legacy guests (older Windows, ReactOS) expect i440FX IDE, UHCI USB and PS/2.
            command += ["-device", "ide-hd,drive=osdisk,bus=ide.0,bootindex=0", "-vga", "std",
                        "-usb", "-device", "e1000,netdev=net0"]
        else:
            command += ["-device", "ich9-ahci,id=sata", "-device", "ide-hd,drive=osdisk,bus=sata.0,bootindex=0",
                        "-vga", "std", "-device", "qemu-xhci", "-device", "e1000,netdev=net0"]
        command += ["-device", "usb-tablet", *(() if bios else ("-device", "usb-kbd")),
                    "-netdev", "user,id=net0" + (",restrict=on" if record.get("network", "isolated") == "isolated" else "")]
        if record.get("tpm"):
            tpm_device = "tpm-tis-device,tpmdev=tpm0" if arch == "aarch64" else "tpm-tis,tpmdev=tpm0"
            if arch == "aarch64" and acceleration == "hvf":
                tpm_device += ",ppi=off"
            command += ["-chardev", f"socket,id=chrtpm,path={self.qmp_path(name).with_suffix('.tpm')}",
                        "-tpmdev", "emulator,id=tpm0,chardev=chrtpm", "-device",
                        tpm_device]
        if record["iso"]:
            iso = self._existing_file(record["iso"], "Windows installation ISO")
            command += ["-drive", f"if=none,id=install,format=raw,media=cdrom,readonly=on,file={self._escape(iso)}"]
            if arch == "aarch64":
                command += ["-device", "usb-storage,drive=install"]
            elif bios:
                command += ["-device", "ide-cd,drive=install,bus=ide.1,bootindex=1"]
            else:
                command += ["-device", "ide-cd,drive=install,bus=sata.1,bootindex=1"]
        answer = directory / "answer.iso"
        if answer.is_file() and not bios:
            # autounattend.xml on a second, non-bootable disc; Windows Setup scans removable media for it.
            command += ["-drive", f"if=none,id=answer,format=raw,media=cdrom,readonly=on,file={self._escape(answer)}",
                        "-device", "usb-storage,drive=answer" if arch == "aarch64" else "ide-cd,drive=answer,bus=sata.2"]
        return command

    def disk_is_blank(self, name: str) -> bool:
        """True until the guest has written to its disk, i.e. before an OS is installed."""
        disk = self._directory(name) / "disk.qcow2"
        result = subprocess.run([self._executable("qemu-img"), "info", "--output=json", "-U", str(disk)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            return False
        return json.loads(result.stdout).get("actual-size", 1 << 30) < 4 * 1024 * 1024

    @staticmethod
    def _escape(path: Path) -> str:
        return str(path).replace(",", ",,")

    @staticmethod
    def _swtpm_executable() -> str:
        executable = shutil.which("swtpm")
        if not executable:
            raise RuntimeError("TPM 2.0 requires swtpm. Install it with brew install swtpm or your Linux package manager, then retry.")
        return executable

    def _start_tpm(self, name: str, log) -> subprocess.Popen:
        directory = self._directory(name) / "tpm"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = self.qmp_path(name).with_suffix(".tpm")
        previous = path.lstat() if path.exists() else None
        command = [self._swtpm_executable(), "socket", "--tpm2",
                   "--tpmstate", f"dir={directory},mode=0600,lock",
                   "--ctrl", f"type=unixio,path={path},mode=0600,terminate"]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"swtpm exited; see {self._directory(name) / 'qemu.log'}")
                if path.exists():
                    metadata = path.lstat()
                    if stat.S_ISSOCK(metadata.st_mode) and (previous is None or (previous.st_ino, previous.st_mtime_ns) != (metadata.st_ino, metadata.st_mtime_ns)):
                        self._tpm_processes[name] = process
                        return process
                time.sleep(0.05)
            raise RuntimeError("TPM startup timed out; inspect qemu.log")
        except BaseException:
            self._terminate_child(process)
            raise

    @staticmethod
    def _terminate_child(process):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _owned(self, record: dict) -> bool:
        pid = record.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        process = self._processes.get(record["name"])
        if process is not None and process.pid == pid:
            return process.poll() is None
        result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False)
        if result.returncode:
            return False
        expected = record.get("launch_command")
        if not isinstance(expected, list) or not all(isinstance(value, str) for value in expected):
            return False
        try:
            arguments = (expected if result.stdout.strip() == " ".join(expected)
                         else shlex.split(result.stdout.strip()))
        except ValueError:
            return False
        return bool(expected and arguments == expected and "-uuid" in arguments
                    and arguments.index("-uuid") + 1 < len(arguments)
                    and arguments[arguments.index("-uuid") + 1] == record["uuid"])

    def status(self, name: str) -> dict:
        record = self.load(name)
        if self._owned(record):
            record["state"] = "stopping" if record.get("state") == "stopping" else "running"
        else:
            record.update(state="stopped", pid=None)
        return record

    def start(self, name: str) -> dict:
        with self._lock(name):
            record = self.status(name)
            if record["state"] in {"running", "stopping"}:
                return record
            command = self.command(name)
            path = self.qmp_path(name)
            if path.exists():
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise RuntimeError(f"Refusing to remove non-socket QMP path: {path}")
                try:
                    self._qmp(path, "query-status")
                except (OSError, RuntimeError) as error:
                    import errno
                    cause = error if isinstance(error, OSError) else error.__cause__
                    if isinstance(cause, OSError) and cause.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                        path.unlink(missing_ok=True)
                    else:
                        raise RuntimeError("QMP socket is busy or ownership is unknown; refusing duplicate start") from error
                else:
                    raise RuntimeError("QMP socket is active but process ownership is unknown; refusing duplicate start")
            directory = self._directory(name)
            log_path = directory / "qemu.log"
            tpm_process = None
            with log_path.open("ab") as log:
                if record.get("tpm"):
                    tpm_process = self._start_tpm(name, log)
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                except BaseException:
                    self._terminate_child(tpm_process)
                    raise
            self._processes[name] = process
            record.update(state="running", pid=process.pid, launch_command=command,
                          started_at=time.time(), log=str(log_path))
            try:
                self._write(directory, record)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"QEMU exited with code {process.returncode}; see {log_path}")
                    try:
                        self._qmp(path, "query-status")
                        return record
                    except (OSError, RuntimeError):
                        time.sleep(0.05)
                raise RuntimeError(f"QEMU QMP startup timed out; see {log_path}")
            except BaseException as error:
                self._terminate_child(process)
                self._terminate_child(tpm_process)
                record.update(state="stopped", pid=None, last_error=str(error))
                self._write(directory, record)
                raise

    def stop(self, name: str, force: bool = False) -> dict:
        with self._lock(name):
            record = self.status(name)
            if record["state"] == "stopped":
                return record
            self._qmp(self.qmp_path(name), "quit" if force else "system_powerdown")
            deadline = time.monotonic() + (3 if force else 1)
            while self._owned(record) and time.monotonic() < deadline:
                time.sleep(0.05)
            if self._owned(record):
                record["state"] = "stopping"
            else:
                record.update(state="stopped", pid=None)
            self._write(self._directory(name), record)
            return record

    @staticmethod
    def _qmp(path: Path, command: str) -> dict:
        from .qmp import QMPClient
        with QMPClient(path, timeout=1) as client:
            return client.execute(command)
