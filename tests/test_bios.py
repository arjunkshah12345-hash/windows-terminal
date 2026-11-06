import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch

from windows_terminal.checkpoints import Checkpoints
from windows_terminal.cli import parser
from windows_terminal.vm import VMConfig, VMManager


class BiosBootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.manager = VMManager(self.base / "vms")
        self.iso = self.base / "legacy.iso"
        self.iso.touch()
        which = patch("windows_terminal.vm.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        which.start()
        self.addCleanup(which.stop)

    def create(self, **kwargs):
        def run(arguments, **options):
            Path(arguments[-2]).touch()
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("windows_terminal.vm.subprocess.run", side_effect=run):
            return self.manager.create(VMConfig(iso=str(self.iso), arch="x86_64", boot="bios", **kwargs))

    def test_bios_vm_has_no_uefi_files_and_uses_legacy_machine(self):
        record = self.create()
        directory = self.manager.root / "windows"
        self.assertEqual(record["boot"], "bios")
        self.assertIsNone(record["firmware"])
        self.assertFalse((directory / "uefi-code.fd").exists())
        command = self.manager.command("windows")
        self.assertEqual(command[command.index("-machine") + 1], "pc")
        self.assertNotIn("if=pflash", " ".join(command))
        self.assertIn("ide-cd,drive=install,bus=ide.1", command)
        self.assertIn("ide-hd,drive=osdisk,bus=ide.0", command)
        self.assertNotIn("usb-kbd", command)
        self.assertIn("restrict=on", command[command.index("-netdev") + 1])

    def test_bios_requires_x86_64_and_no_uefi_firmware(self):
        with self.assertRaisesRegex(ValueError, "only available for x86_64"):
            VMManager._validate(VMConfig(arch="aarch64", boot="bios"))
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            VMManager._validate(VMConfig(arch="x86_64", boot="bios", firmware="/x/OVMF_CODE.fd"))
        with self.assertRaisesRegex(ValueError, "uefi or bios"):
            VMManager._validate(VMConfig(boot="coreboot"))

    def test_existing_configs_without_boot_default_to_uefi(self):
        record = {**vars(VMConfig(iso=None)), "uuid": "0e7a48e8-5106-47ae-bb27-2c2bb7a44ab2"}
        record.pop("boot")
        VMManager._validate_record(record, "windows")

    def test_cli_accepts_boot_flag(self):
        args = parser().parse_args(["init", "--iso", "x.iso", "--arch", "x86_64", "--boot", "bios"])
        self.assertEqual(args.boot, "bios")

    def test_bios_checkpoint_round_trip_without_firmware(self):
        self.create()
        (self.manager.root / "windows/disk.qcow2").write_bytes(b"disk")
        checkpoints = Checkpoints(self.manager)

        def convert(arguments, **options):
            Path(arguments[-1]).write_bytes(Path(arguments[-2]).read_bytes())
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("windows_terminal.checkpoints.subprocess.run", side_effect=convert), \
                patch.object(self.manager, "_executable", return_value="qemu-img"):
            saved = checkpoints.save("windows", "clean")
            manifest = json.loads((Path(saved["path"]) / "manifest.json").read_text())
            self.assertEqual(set(manifest["sha256"]), {"disk.qcow2", "config.json"})
            restored = checkpoints.restore("windows", "clean", "copy")
        self.assertEqual(restored["boot"], "bios")
        self.assertEqual((self.manager.root / "copy/disk.qcow2").read_bytes(), b"disk")
        self.assertIn("-machine", self.manager.command("copy"))


class DetachMessageTests(unittest.TestCase):
    def test_up_view_prints_short_reattach_hint_not_the_record(self):
        from io import StringIO
        from windows_terminal import cli
        args = parser().parse_args(["up", "--vm", "work", "--view", "--fps", "10", "--protocol", "blocks"])
        manager = unittest.mock.Mock()
        manager.start.return_value = {"launch_command": ["qemu"] * 40}
        manager.status.return_value = {"state": "running"}
        manager.qmp_path.return_value = "/tmp/none.sock"
        with patch.object(cli, "VMManager", return_value=manager), \
                patch("windows_terminal.viewer.view") as view, patch("sys.stderr", new=StringIO()) as stderr:
            self.assertIsNone(cli.run(args))
        self.assertEqual(view.call_args.args[2:], ("blocks", 10))
        self.assertIn("still running", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
