import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from windows_terminal.checkpoints import Checkpoints
from windows_terminal.cli import parser, run
from windows_terminal.vm import VMConfig, VMManager


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = VMManager(self.root)
        self.directory = self.root / "windows"
        self.directory.mkdir()
        self.record = {**vars(VMConfig(iso=None)), "uuid": "0e7a48e8-5106-47ae-bb27-2c2bb7a44ab2", "state": "stopped", "pid": None}
        self.manager._write(self.directory, self.record)
        for filename in ("disk.qcow2", "uefi-code.fd", "uefi-vars.fd"):
            (self.directory / filename).write_bytes(filename.encode())
        self.checkpoints = Checkpoints(self.manager)
        self.converts = []

        def convert(arguments, **options):
            self.converts.append(arguments)
            Path(arguments[-1]).write_bytes(Path(arguments[-2]).read_bytes())
            return subprocess.CompletedProcess(arguments, 0, "", "")

        self.run_patch = patch("windows_terminal.checkpoints.subprocess.run", side_effect=convert)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.executable_patch = patch.object(self.manager, "_executable", return_value="qemu-img")
        self.executable_patch.start()
        self.addCleanup(self.executable_patch.stop)

    def test_save_restore_preserves_complete_state_and_original(self):
        self.record["tpm"] = True
        self.manager._write(self.directory, self.record)
        (self.directory / "tpm").mkdir()
        (self.directory / "tpm/tpm2-00.permall").write_bytes(b"identity")
        saved = self.checkpoints.save("windows", "clean")
        (self.directory / "uefi-vars.fd").write_bytes(b"changed")
        restored = self.checkpoints.restore("windows", "clean", "recovered")
        self.assertEqual(restored["uuid"], self.record["uuid"])
        self.assertEqual(restored["state"], "stopped")
        self.assertIsNone(restored["iso"])
        self.assertEqual((self.root / "recovered/tpm/tpm2-00.permall").read_bytes(), b"identity")
        self.assertEqual((self.root / "recovered/uefi-vars.fd").read_bytes(), b"uefi-vars.fd")
        self.assertEqual((self.directory / "uefi-vars.fd").read_bytes(), b"changed")
        self.assertEqual(self.checkpoints.list("windows")[0]["name"], "clean")
        self.assertEqual(Path(saved["path"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.converts[0][1:6], ["convert", "-f", "qcow2", "-O", "qcow2"])
        for filename in ("disk.qcow2", "uefi-code.fd", "uefi-vars.fd", "tpm/tpm2-00.permall"):
            self.assertNotEqual((Path(saved["path"]) / filename).stat().st_ino, (self.root / "recovered" / filename).stat().st_ino)

    def test_save_never_overwrites_a_checkpoint(self):
        self.checkpoints.save("windows", "clean")
        with self.assertRaises(FileExistsError):
            self.checkpoints.save("windows", "clean")

    def test_restore_never_overwrites_a_vm(self):
        self.checkpoints.save("windows", "clean")
        with self.assertRaises(FileExistsError):
            self.checkpoints.restore("windows", "clean", "windows")
        self.assertEqual((self.directory / "disk.qcow2").read_bytes(), b"disk.qcow2")

    def test_running_guest_is_rejected(self):
        with patch.object(self.manager, "status", return_value={**self.record, "state": "running"}):
            with self.assertRaisesRegex(RuntimeError, "Shut down"):
                self.checkpoints.save("windows", "clean")

    def test_unknown_active_qmp_is_rejected(self):
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.manager.qmp_path("windows")))
            with patch.object(self.manager, "_qmp", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "QMP is active"):
                    self.checkpoints.save("windows", "clean")

    def test_unknown_qmp_failure_is_rejected(self):
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.manager.qmp_path("windows")))
            with patch.object(self.manager, "_qmp", side_effect=RuntimeError("timeout")):
                with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
                    self.checkpoints.save("windows", "clean")

    def test_invalid_names_and_symlinks_are_rejected(self):
        for name in ("../escape", "/tmp/escape", "", ".hidden"):
            with self.assertRaises(ValueError):
                self.checkpoints.save("windows", name)
        (self.directory / "checkpoints").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.checkpoints.save("windows", "clean")

    def test_symlink_state_is_rejected(self):
        (self.directory / "disk.qcow2").unlink()
        (self.directory / "disk.qcow2").symlink_to(self.directory / "uefi-code.fd")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.checkpoints.save("windows", "clean")
        self.assertEqual(self.checkpoints.list("windows"), [])

    def test_modified_checkpoint_is_rejected_before_creating_vm(self):
        self.checkpoints.save("windows", "clean")
        (self.directory / "checkpoints/clean/uefi-vars.fd").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.checkpoints.restore("windows", "clean", "recovered")
        self.assertFalse((self.root / "recovered").exists())

    def test_manifest_traversal_is_rejected(self):
        self.checkpoints.save("windows", "clean")
        path = self.directory / "checkpoints/clean/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["sha256"]["../config.json"] = "bad"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "unexpected state path"):
            self.checkpoints.restore("windows", "clean", "recovered")

    def test_failed_save_cleans_staging(self):
        with patch.object(self.checkpoints, "_copy_disk", side_effect=RuntimeError("full disk")):
            with self.assertRaisesRegex(RuntimeError, "full disk"):
                self.checkpoints.save("windows", "clean")
        self.assertEqual(list((self.directory / "checkpoints").iterdir()), [])

    def test_failed_restore_removes_only_new_vm(self):
        self.checkpoints.save("windows", "clean")
        with patch.object(self.manager, "_copy_firmware", side_effect=OSError("full disk")):
            with self.assertRaisesRegex(OSError, "full disk"):
                self.checkpoints.restore("windows", "clean", "recovered")
        self.assertFalse((self.root / "recovered").exists())
        self.assertTrue((self.directory / "checkpoints/clean/manifest.json").is_file())

    def test_busy_tpm_state_is_rejected_across_processes(self):
        self.record["tpm"] = True
        self.manager._write(self.directory, self.record)
        tpm = self.directory / "tpm"
        tpm.mkdir()
        script = "import fcntl,sys,time; stream=open(sys.argv[1],'a+b'); fcntl.lockf(stream,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)"
        with subprocess.Popen([sys.executable, "-c", script, str(tpm / ".lock")], stdout=subprocess.PIPE, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(RuntimeError, "TPM state is busy"):
                    self.checkpoints.save("windows", "clean")
            finally:
                process.terminate()
                process.wait(timeout=5)
        self.assertEqual(self.checkpoints.list("windows"), [])
        self.checkpoints.save("windows", "clean")

    def test_existing_empty_target_is_not_removed(self):
        self.checkpoints.save("windows", "clean")
        (self.root / "recovered").mkdir()
        with self.assertRaises(FileExistsError):
            self.checkpoints.restore("windows", "clean", "recovered")
        self.assertTrue((self.root / "recovered").is_dir())

    def test_dangling_target_symlink_is_not_removed(self):
        self.checkpoints.save("windows", "clean")
        target = self.root / "recovered"
        target.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.checkpoints.restore("windows", "clean", "recovered")
        self.assertTrue(target.is_symlink())

    def test_invalid_saved_config_is_rejected_without_partial_vm(self):
        self.checkpoints.save("windows", "clean")
        directory = self.directory / "checkpoints/clean"
        config = directory / "config.json"
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for record in ([], {**self.record, "uuid": 42}, {"name": "windows"}):
            config.write_text(json.dumps(record))
            manifest["sha256"]["config.json"] = self.checkpoints._digest(config)
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                self.checkpoints.restore("windows", "clean", "recovered")
            self.assertFalse((self.root / "recovered").exists())

    def test_restored_config_drops_runtime_and_media_fields(self):
        self.checkpoints.save("windows", "clean")
        directory = self.directory / "checkpoints/clean"
        config = directory / "config.json"
        record = json.loads(config.read_text())
        record.update(pid=1234, state="running", launch_command=["unexpected"], iso="/missing.iso")
        config.write_text(json.dumps(record))
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sha256"]["config.json"] = self.checkpoints._digest(config)
        manifest_path.write_text(json.dumps(manifest))
        restored = self.checkpoints.restore("windows", "clean", "recovered")
        self.assertIsNone(restored["iso"])
        self.assertIsNone(restored["pid"])
        self.assertEqual(restored["state"], "stopped")
        self.assertNotIn("launch_command", restored)

    def test_cli_routes_checkpoint_commands(self):
        with patch("windows_terminal.checkpoints.Checkpoints") as checkpoints:
            for operation, expected in ((["save", "clean"], "save"), (["list"], "list"), (["restore", "clean", "--as", "recovered"], "restore")):
                args = parser().parse_args(["--root", str(self.root), "checkpoint", *operation])
                run(args)
                self.assertTrue(getattr(checkpoints.return_value, expected).called)


if __name__ == "__main__":
    unittest.main()
