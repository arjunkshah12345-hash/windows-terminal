"""Immutable offline VM checkpoints, restored without overwriting a VM."""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .vm import VMConfig, VMManager


class Checkpoints:
    def __init__(self, manager: VMManager):
        self.manager = manager

    def _root(self, name):
        root = self.manager._directory(name) / "checkpoints"
        if root.is_symlink():
            raise ValueError("Checkpoint directories must not be symbolic links")
        return root

    def _path(self, name, checkpoint):
        self.manager._validate(VMConfig(name=checkpoint))
        path = self._root(name) / checkpoint
        if path.is_symlink():
            raise ValueError("Checkpoints must not be symbolic links")
        return path

    @contextmanager
    def _offline(self, name):
        with self.manager._lock(name):
            record = self.manager.status(name)
            if record["state"] != "stopped":
                raise RuntimeError("Shut down the VM before using checkpoints")
            path = self.manager.qmp_path(name)
            if path.exists():
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise RuntimeError("QMP path is not a socket; refusing checkpoint")
                try:
                    self.manager._qmp(path, "query-status")
                except (OSError, RuntimeError) as error:
                    cause = error if isinstance(error, OSError) else error.__cause__
                    if not isinstance(cause, OSError) or cause.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                        raise RuntimeError("QMP ownership is unknown; refusing checkpoint") from error
                else:
                    raise RuntimeError("QMP is active; refusing checkpoint")
            if record.get("tpm"):
                directory = self.manager._directory(name) / "tpm"
                if directory.is_symlink():
                    raise ValueError("TPM state must not be a symbolic link")
                directory.mkdir(mode=0o700, exist_ok=True)
                with (directory / ".lock").open("a+b") as lock:
                    try:
                        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        raise RuntimeError("TPM state is busy; wait for swtpm to exit") from error
                    yield record
            else:
                yield record

    @staticmethod
    def _digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _copy_disk(self, source, destination):
        result = subprocess.run(
            [self.manager._executable("qemu-img"), "convert", "-f", "qcow2", "-O", "qcow2", str(source), str(destination)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Checkpoint disk copy failed: {result.stderr.strip() or result.stdout.strip()}")
        destination.chmod(0o600)

    @staticmethod
    def _sync_directory(directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _sync_tree(self, directory):
        directories = [directory]
        for path in directory.rglob("*"):
            if path.is_dir():
                directories.append(path)
            else:
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for path in reversed(directories):
            self._sync_directory(path)

    @staticmethod
    def _files(directory):
        names = ("disk.qcow2",) if Checkpoints._boot(directory) == "bios" else ("disk.qcow2", "uefi-code.fd", "uefi-vars.fd")
        files = [directory / filename for filename in names]
        tpm = directory / "tpm"
        if tpm.is_symlink():
            raise ValueError("TPM state must not be a symbolic link")
        if tpm.exists():
            files.extend(path for path in tpm.iterdir() if path.name != ".lock")
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Checkpoint source must be a regular file: {path}")
        return files

    @staticmethod
    def _boot(directory):
        try:
            return json.loads((directory / "config.json").read_text(encoding="utf-8")).get("boot", "uefi")
        except (OSError, ValueError, AttributeError):
            return "uefi"

    def save(self, name, checkpoint):
        destination = self._path(name, checkpoint)
        with self._offline(name) as record:
            if destination.exists():
                raise FileExistsError(f"Checkpoint already exists: {checkpoint}")
            root = self._root(name)
            root.mkdir(mode=0o700, exist_ok=True)
            directory = self.manager._directory(name)
            with tempfile.TemporaryDirectory(prefix=".saving-", dir=root) as temporary:
                staging = Path(temporary)
                digests = {}
                for source in self._files(directory):
                    relative = source.relative_to(directory)
                    target = staging / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if relative == Path("disk.qcow2"):
                        self._copy_disk(source, target)
                    else:
                        self.manager._copy_firmware(source, target)
                        target.chmod(0o600)
                    digests[str(relative)] = self._digest(target)
                config = {key: record[key] for key in VMConfig.__dataclass_fields__ if key in record}
                config.update(uuid=record["uuid"], iso=None, firmware=None, state="stopped", pid=None)
                self.manager._write(staging, config)
                digests["config.json"] = self._digest(staging / "config.json")
                manifest = {"version": 1, "name": checkpoint, "created_at": datetime.now(timezone.utc).isoformat(), "sha256": digests}
                manifest_path = staging / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                manifest_path.chmod(0o600)
                self._sync_tree(staging)
                staging.rename(destination)
                self._sync_directory(root)
                self._sync_directory(directory)
            return {"name": checkpoint, "path": str(destination), "created_at": manifest["created_at"]}

    def list(self, name):
        self.manager.load(name)
        root = self._root(name)
        if not root.exists():
            return []
        return [{"name": path.name, "path": str(path)} for path in sorted(root.iterdir())
                if not path.name.startswith(".") and not path.is_symlink() and path.is_dir() and (path / "manifest.json").is_file()]

    def _verified_files(self, directory):
        path = directory / "manifest.json"
        if path.is_symlink():
            raise ValueError("Checkpoint manifest must not be a symbolic link")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            digests = manifest["sha256"]
            if manifest["version"] != 1 or not isinstance(digests, dict):
                raise ValueError("unsupported manifest")
            required = {"disk.qcow2", "uefi-code.fd", "uefi-vars.fd", "config.json"}
            if self._boot(directory) == "bios":
                required = {"disk.qcow2", "config.json"}
            if not required <= digests.keys():
                raise ValueError("missing state files")
            for filename, digest in digests.items():
                relative = Path(filename)
                if filename not in required and not (len(relative.parts) == 2 and relative.parts[0] == "tpm" and relative.parts[1] not in {"..", ".lock"}):
                    raise ValueError("unexpected state path")
                source = directory / relative
                if any(parent.is_symlink() for parent in (source, source.parent)) or not source.is_file():
                    raise ValueError(f"unsafe state file: {filename}")
                if self._digest(source) != digest:
                    raise ValueError(f"checksum mismatch: {filename}")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid checkpoint {directory.name}: {error}") from None
        return digests

    def restore(self, name, checkpoint, target_name):
        source = self._path(name, checkpoint)
        destination = self.manager._directory(target_name)
        with self._offline(name):
            files = self._verified_files(source)
            record = json.loads((source / "config.json").read_text(encoding="utf-8"))
            self.manager._validate_record(record, name)
            config = VMConfig(**{key: record[key] for key in VMConfig.__dataclass_fields__ if key in record})
            if config.tpm and "," in str(self.manager.root):
                raise ValueError("TPM storage paths cannot contain commas")
            destination.mkdir(mode=0o700)
            try:
                with self.manager._lock(target_name):
                    for filename in files:
                        if filename == "config.json":
                            continue
                        target = destination / filename
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        if filename == "disk.qcow2":
                            self._copy_disk(source / filename, target)
                        else:
                            self.manager._copy_firmware(source / filename, target)
                            target.chmod(0o600)
                    record = {key: record[key] for key in VMConfig.__dataclass_fields__ if key in record} | {"uuid": record["uuid"]}
                    record.update(name=target_name, iso=None, firmware=None, state="stopped", pid=None,
                                  qmp_socket=str(self.manager.qmp_path(target_name)))
                    self.manager._write(destination, record)
                    result = self.manager.load(target_name)
                    self._sync_tree(destination)
                    self._sync_directory(self.manager.root)
            except BaseException:
                shutil.rmtree(destination)
                raise
            return result
