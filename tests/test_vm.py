import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from windows_terminal.vm import VMConfig, VMManager


class VMTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.manager = VMManager(self.base / "vms")
        self.iso = self.base / "windows.iso"
        self.iso.touch()
        self.code = self.base / "OVMF_CODE.fd"
        self.variables = self.base / "OVMF_VARS.fd"
        self.code.write_bytes(b"firmware")
        self.variables.write_bytes(b"variables")
        self.which = patch("windows_terminal.vm.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        self.which.start()
        self.addCleanup(self.which.stop)

    def create(self, **kwargs):
        def run(arguments, **options):
            Path(arguments[-2]).touch()
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("windows_terminal.vm.subprocess.run", side_effect=run):
            return self.manager.create(VMConfig(iso=str(self.iso), firmware=str(self.code), **kwargs))

    def test_create_persists_private_firmware_and_qcow2(self):
        record = self.create(arch="x86_64")
        self.assertEqual(record["state"], "stopped")
        directory = self.manager.root / "windows"
        self.assertEqual((directory / "uefi-vars.fd").read_bytes(), b"variables")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.load("windows"), record)
        self.variables.write_bytes(b"changed")
        self.assertEqual((directory / "uefi-vars.fd").read_bytes(), b"variables")

    def test_sparse_firmware_copy_preserves_data_and_trailing_zeros(self):
        contents = bytes(65536) + b"firmware" + bytes(131072)
        self.code.write_bytes(contents)
        destination = self.base / "copied.fd"
        self.manager._copy_firmware(self.code, destination)
        self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(destination.stat().st_size, len(contents))

    def test_create_invokes_qemu_img_with_argument_list(self):
        with patch("windows_terminal.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.manager.create(VMConfig(iso=str(self.iso), firmware=str(self.code)))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/qemu-img", "create", "-f", "qcow2", str(self.manager.root / "windows/disk.qcow2"), "64G"])

    def test_existing_vm_is_not_overwritten(self):
        original = self.create()
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.manager.load("windows"), original)

    def test_invalid_names_and_resources(self):
        for name in ("../outside", "/tmp/evil", ".", "a/b", "a\\b", "a b", "-bad", "a" * 49, ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manager.load(name)
        for arguments in ({"arch": "arm"}, {"memory_mb": 511}, {"memory_mb": True},
                          {"cpus": 0}, {"cpus": 257}, {"disk_gb": 0},
                          {"disk_gb": 16385}, {"disk_gb": 64.0}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.manager.create(VMConfig(iso=str(self.iso), **arguments))

    def test_iso_required_and_must_exist(self):
        for iso in (None, str(self.base / "missing.iso"), str(self.base)):
            with self.subTest(iso=iso), self.assertRaises(ValueError):
                self.manager.create(VMConfig(iso=iso))

    def test_failed_create_cleans_only_new_directory(self):
        with patch("windows_terminal.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                self.manager.create(VMConfig(iso=str(self.iso), firmware=str(self.code)))
        self.assertFalse((self.manager.root / "windows").exists())

    def test_symlink_vm_directory_rejected(self):
        self.manager.root.mkdir()
        (self.manager.root / "windows").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.load("windows")

    def test_xdg_root(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.base / "data")}):
            self.assertEqual(VMManager().root, self.base / "data/windows-terminal")

    def test_malformed_configs_raise_actionable_errors(self):
        original = self.create()
        path = self.manager.root / "windows/config.json"
        for record in (None, [], {"name": "windows"}, {**original, "uuid": None}, {**original, "uuid": "bad"}, {**original, "arch": []}, {**original, "iso": []}, {**original, "firmware": 7}):
            with self.subTest(record=record):
                path.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "Invalid VM config .*config.json"):
                    self.manager.load("windows")
        path.write_text("{invalid json")
        with self.assertRaisesRegex(ValueError, "Invalid VM config .*config.json"):
            self.manager.load("windows")

    def test_older_configs_without_optional_network_and_tpm_load(self):
        record = self.create()
        del record["network"]
        del record["tpm"]
        path = self.manager.root / "windows/config.json"
        path.write_text(json.dumps(record))
        self.assertEqual(self.manager.load("windows"), record)
        self.assertIn("user,id=net0,restrict=on", self.manager.command("windows"))

    def test_cli_reports_corrupt_config_without_traceback(self):
        original = self.create()
        path = self.manager.root / "windows/config.json"
        for record in ({"name": "windows"}, {**original, "uuid": None}):
            path.write_text(json.dumps(record))
            result = subprocess.run([sys.executable, "-m", "windows_terminal", "--root", str(self.manager.root), "status"], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("Invalid VM config", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_explicit_firmware_requires_matching_vars(self):
        self.assertEqual(self.manager._firmware("x86_64", str(self.code)), (self.code, self.variables))
        self.variables.unlink()
        with self.assertRaisesRegex(ValueError, "VARS"):
            self.manager._firmware("x86_64", str(self.code))

    def test_auto_detect_brew_and_system_firmware(self):
        for arch, code, variables in (
            ("aarch64", "/opt/homebrew/share/qemu/edk2-aarch64-code.fd", "/opt/homebrew/share/qemu/edk2-arm-vars.fd"),
            ("x86_64", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
        ):
            with self.subTest(arch=arch), patch.object(Path, "is_file", lambda path: str(path) in {code, variables}), patch.object(Path, "resolve", lambda path: path):
                self.assertEqual(self.manager._firmware(arch, None), (Path(code), Path(variables)))

    def test_missing_firmware_is_actionable(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Install QEMU firmware"):
                self.manager._firmware("aarch64", None)

    def test_missing_qemu_is_actionable(self):
        self.create()
        with patch("windows_terminal.vm.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Install QEMU"):
                self.manager.command("windows")

    def test_x86_command_is_headless_sata_and_isolated(self):
        self.create(arch="x86_64")
        command = self.manager.command("windows")
        self.assertIn("ide-hd,drive=osdisk,bus=sata.0", command)
        self.assertIn("e1000,netdev=net0", command)
        self.assertEqual(command[command.index("-vga") + 1], "std")
        self.assertEqual(command[command.index("-display") + 1], "none")
        self.assertIn("user,id=net0,restrict=on", command)
        self.assertNotIn("hostfwd", " ".join(command))
        self.assertIn("usb-tablet", command)
        self.assertIn("usb-kbd", command)
        self.assertIn(f"unix:{self.manager.qmp_path('windows')},server=on,wait=off", command)

    def test_arm_command_uses_nvme_ramfb(self):
        self.create(arch="aarch64")
        command = self.manager.command("windows")
        self.assertIn("nvme,drive=osdisk,serial=WT0001", command)
        self.assertIn("ramfb", command)
        self.assertIn("e1000,netdev=net0", command)
        self.assertIn("usb-storage,drive=install", command)

    def test_acceleration_requires_matching_arch_and_access(self):
        for system, machine, arch, kvm, expected in (
            ("Darwin", "arm64", "aarch64", False, "hvf"),
            ("Darwin", "arm64", "x86_64", False, "tcg"),
            ("Linux", "x86_64", "x86_64", True, "kvm"),
            ("Linux", "x86_64", "x86_64", False, "tcg"),
            ("Linux", "x86_64", "aarch64", True, "tcg"),
        ):
            with self.subTest(expected=expected), patch("platform.system", return_value=system), patch("platform.machine", return_value=machine), patch("os.access", return_value=kvm):
                self.assertEqual(self.manager._acceleration(arch), expected)

    def test_nat_network_is_explicit_and_does_not_forward_ports(self):
        self.create(network="nat")
        command = self.manager.command("windows")
        self.assertIn("user,id=net0", command)
        self.assertNotIn("restrict=on", " ".join(command))
        self.assertNotIn("hostfwd", " ".join(command))

    def test_invalid_network_and_tpm_are_rejected(self):
        for config in [VMConfig(network="bridged"), VMConfig(tpm="yes")]:
            with self.assertRaises(ValueError):
                self.manager._validate(config)

    def test_missing_tpm_dependency_is_actionable(self):
        with patch("windows_terminal.vm.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "TPM 2.0 requires swtpm"):
                self.manager._swtpm_executable()

    def test_tpm_commands_match_architecture(self):
        for arch, device in [("aarch64", "tpm-tis-device,tpmdev=tpm0"), ("x86_64", "tpm-tis,tpmdev=tpm0")]:
            name = arch.replace("_", "-")
            with patch.object(self.manager, "_swtpm_executable", return_value="/usr/bin/swtpm"):
                self.create(name=name, arch=arch, tpm=True)
            with patch.object(self.manager, "_acceleration", return_value="tcg"):
                command = self.manager.command(name)
            self.assertIn("emulator,id=tpm0,chardev=chrtpm", command)
            self.assertIn(device, command)

    def test_arm_hvf_disables_tpm_physical_presence_region(self):
        self.create(arch="aarch64", tpm=True)
        with patch.object(self.manager, "_acceleration", return_value="hvf"):
            command = self.manager.command("windows")
        self.assertIn("tpm-tis-device,tpmdev=tpm0,ppi=off", command)

    def test_boot_prefers_disk_over_installation_media(self):
        self.create()
        command = self.manager.command("windows")
        self.assertEqual(command[command.index("-boot") + 1], "order=cd,menu=on")

    def test_installation_media_is_always_raw_not_format_probed(self):
        self.create()
        command = self.manager.command("windows")
        media = next(argument for argument in command if "id=install," in argument)
        self.assertIn("format=raw", media)

    def test_commas_in_root_do_not_break_monitor_socket(self):
        manager = VMManager(self.base / "directory,with,commas")
        self.assertNotIn(",", str(manager.qmp_path("windows")))

    def test_long_data_root_uses_short_private_socket(self):
        self.manager = VMManager(self.base / ("long" * 30))
        with patch("windows_terminal.vm.tempfile.gettempdir", return_value=str(self.base)):
            path = self.manager.qmp_path("windows")
            self.assertLessEqual(len(os.fsencode(path)), 100)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path, self.manager.qmp_path("windows"))
            self.assertNotEqual(path, self.manager.qmp_path("another"))

    def test_runtime_symlink_rejected(self):
        self.manager = VMManager(self.base / ("long" * 30))
        (self.base / f"wt-{os.getuid()}").symlink_to(self.base)
        with patch("windows_terminal.vm.tempfile.gettempdir", return_value=str(self.base)):
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                self.manager.qmp_path("windows")

    def test_stale_pid_is_never_signalled(self):
        record = self.create()
        record.update(pid=1234, state="running", launch_command=["qemu"])
        self.manager._write(self.manager._directory("windows"), record)
        with patch("windows_terminal.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "unrelated-process", "")), patch.object(self.manager, "_qmp") as qmp:
            self.assertEqual(self.manager.stop("windows", force=True)["state"], "stopped")
            qmp.assert_not_called()

    def test_cross_manager_process_ownership_exact_command(self):
        record = self.create()
        command = self.manager.command("windows")
        record.update(pid=1234, launch_command=command)
        with patch("windows_terminal.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0, " ".join(command), "")):
            self.assertTrue(self.manager._owned(record))
        with patch("windows_terminal.vm.subprocess.run", return_value=subprocess.CompletedProcess([], 0, " ".join(command) + " extra", "")):
            self.assertFalse(self.manager._owned(record))

    def test_start_tracks_process_and_is_idempotent(self):
        self.create()
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch("windows_terminal.vm.subprocess.Popen", return_value=process) as popen, patch.object(self.manager, "_qmp", return_value={"status": "running"}):
            record = self.manager.start("windows")
            self.assertEqual(record["pid"], 1234)
            self.assertEqual(self.manager.start("windows")["state"], "running")
            popen.assert_called_once()
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue((self.manager.root / "windows/qemu.log").is_file())

    def test_start_detects_immediate_exit(self):
        self.create()
        process = Mock(pid=1234, returncode=1)
        process.poll.return_value = 1
        with patch("windows_terminal.vm.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "exited with code 1"):
                self.manager.start("windows")
        self.assertEqual(self.manager.load("windows")["state"], "stopped")
        process.terminate.assert_not_called()

    def test_start_timeout_terminates_only_spawned_process(self):
        self.create()
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch("windows_terminal.vm.subprocess.Popen", return_value=process), patch("windows_terminal.vm.time.monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(RuntimeError, "startup timed out"):
                self.manager.start("windows")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertIsNone(self.manager.load("windows")["pid"])

    def test_start_refuses_active_unowned_socket(self):
        self.create()
        path = self.manager.qmp_path("windows")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            with patch.object(self.manager, "_qmp", return_value={}), patch("windows_terminal.vm.subprocess.Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
                    self.manager.start("windows")
                popen.assert_not_called()

    def test_start_preserves_non_socket_file(self):
        self.create()
        path = self.manager.qmp_path("windows")
        path.write_text("keep me")
        with patch("windows_terminal.vm.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "non-socket"):
                self.manager.start("windows")
            popen.assert_not_called()
        self.assertEqual(path.read_text(), "keep me")

    def test_stopping_state_is_preserved_and_start_is_idempotent(self):
        record = self.create()
        record.update(state="stopping", pid=1234)
        self.manager._write(self.manager._directory("windows"), record)
        with patch.object(self.manager, "_owned", return_value=True), patch("windows_terminal.vm.subprocess.Popen") as popen:
            self.assertEqual(self.manager.status("windows")["state"], "stopping")
            self.assertEqual(self.manager.start("windows")["state"], "stopping")
            popen.assert_not_called()

    def test_stop_uses_qmp_not_signals(self):
        record = self.create()
        record.update(pid=1234, state="running")
        for force, expected in ((False, "system_powerdown"), (True, "quit")):
            with self.subTest(force=force), patch.object(self.manager, "status", return_value=record.copy()), patch.object(self.manager, "_owned", return_value=False), patch.object(self.manager, "_qmp") as qmp:
                self.assertEqual(self.manager.stop("windows", force=force)["state"], "stopped")
                qmp.assert_called_once_with(self.manager.qmp_path("windows"), expected)

    def test_list_empty_and_populated(self):
        self.assertEqual(self.manager.list(), [])
        self.create()
        self.create(name="another")
        self.assertEqual([record["name"] for record in self.manager.list()], ["another", "windows"])

    def test_qmp_handshake_and_events_over_real_unix_socket(self):
        path = self.base / "qmp.sock"
        requests = []
        errors = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            server.settimeout(3)

            def serve():
                try:
                    connection, _ = server.accept()
                    with connection, connection.makefile("rwb") as stream:
                        stream.write(b'{"QMP": {}}\n')
                        stream.flush()
                        for _ in range(2):
                            request = json.loads(stream.readline())
                            requests.append(request["execute"])
                            stream.write(b'{"event": "RESUME"}\n')
                            stream.write(json.dumps({"return": {}, "id": request["id"]}).encode() + b"\n")
                            stream.flush()
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            self.assertEqual(self.manager._qmp(path, "quit"), {})
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertEqual(requests, ["qmp_capabilities", "quit"])


if __name__ == "__main__":
    unittest.main()
