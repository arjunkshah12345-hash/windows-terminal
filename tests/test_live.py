import base64
import io
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from PIL import Image

from windows_terminal.control import Computer
from windows_terminal.checkpoints import Checkpoints
from windows_terminal.mcp import call_tool
from windows_terminal.qmp import QMPClient
from windows_terminal.vm import VMConfig, VMManager


@unittest.skipUnless(os.environ.get("WINDOWS_TERMINAL_LIVE_TESTS") == "1", "Set WINDOWS_TERMINAL_LIVE_TESTS=1 to launch disposable real QEMU guests")
class LiveTests(unittest.TestCase):
    def check_architecture(self, arch, tpm=False):
        if not shutil.which(f"qemu-system-{arch}") or not shutil.which("qemu-img"):
            self.skipTest(f"QEMU {arch} is not installed")
        with tempfile.TemporaryDirectory(prefix="wt-live-", dir="/tmp") as folder:
            root = Path(folder)
            placeholder = root / "placeholder.iso"
            placeholder.write_bytes(b"\x00" * 2048)
            manager = VMManager(root / "vms")
            manager.create(VMConfig(arch=arch, iso=str(placeholder), memory_mb=512, cpus=1, disk_gb=16, tpm=tpm))
            process = None
            tpm_process = None
            try:
                try:
                    result = manager.start("windows")
                except RuntimeError as error:
                    log = root / "vms/windows/qemu.log"
                    self.fail(str(error) + "\n" + log.read_text(errors="replace"))
                process = manager._processes["windows"]
                tpm_process = manager._tpm_processes.get("windows")
                self.assertEqual(result["state"], "running")
                time.sleep(0.5)
                other = VMManager(root / "vms")
                self.assertEqual(other.status("windows")["state"], "running")
                self.assertEqual(other.start("windows")["pid"], result["pid"])
                computer = Computer(manager.qmp_path("windows"))
                if tpm:
                    with QMPClient(manager.qmp_path("windows")) as client:
                        self.assertEqual(client.execute("query-tpm")[0]["id"], "tpm0")
                image = computer.screenshot()
                self.assertGreater(image.width, 0)
                self.assertGreater(image.height, 0)
                computer.key("ctrl+alt+delete")
                computer.key("enter")
                computer.type("Test! a-z 123", interval=0)
                computer.move(0, 0)
                computer.click(10, 10)
                computer.scroll("down", 1)
                result = call_tool(manager, "windows", "screenshot", {})
                with Image.open(io.BytesIO(base64.b64decode(result["content"][0]["data"]))) as screenshot:
                    self.assertEqual(screenshot.format, "PNG")
                if arch == "aarch64" and not tpm:
                    self.check_viewer(root / "vms")
                    self.assertEqual(other.status("windows")["state"], "running")
                self.assertEqual(other.stop("windows", force=True)["state"], "stopped")
                process.wait(timeout=5)
                if tpm_process is not None:
                    tpm_process.wait(timeout=5)
                    qemu_io = shutil.which("qemu-io")
                    if qemu_io:
                        subprocess.run([qemu_io, "-f", "qcow2", "-c", "write -P 0x5a 1048576 4096", str(root / "vms/windows/disk.qcow2")], capture_output=True, check=True, timeout=10)
                    checkpoints = Checkpoints(other)
                    checkpoints.save("windows", "clean")
                    if qemu_io:
                        subprocess.run([qemu_io, "-f", "qcow2", "-c", "write -P 0xa5 1048576 4096", str(root / "vms/windows/disk.qcow2")], capture_output=True, check=True, timeout=10)
                    checkpoints.restore("windows", "clean", "recovered")
                    subprocess.run([shutil.which("qemu-img"), "compare", "-f", "qcow2", "-F", "qcow2", str(root / "vms/windows/checkpoints/clean/disk.qcow2"), str(root / "vms/recovered/disk.qcow2")], capture_output=True, check=True, timeout=10)
                    if qemu_io:
                        subprocess.run([qemu_io, "-f", "qcow2", "-c", "read -P 0x5a 1048576 4096", str(root / "vms/recovered/disk.qcow2")], capture_output=True, check=True, timeout=10)
                    for filename in ("uefi-code.fd", "uefi-vars.fd"):
                        self.assertEqual(checkpoints._digest(root / "vms/windows" / filename), checkpoints._digest(root / "vms/recovered" / filename))
                    original_tpm = root / "vms/windows/tpm"
                    for state_file in original_tpm.iterdir():
                        if state_file.name != ".lock":
                            self.assertEqual(state_file.read_bytes(), (root / "vms/recovered/tpm" / state_file.name).read_bytes())
                    recovered = manager.start("recovered")
                    process = manager._processes["recovered"]
                    tpm_process = manager._tpm_processes["recovered"]
                    self.assertEqual(recovered["state"], "running")
                    with QMPClient(manager.qmp_path("recovered")) as client:
                        self.assertTrue(client.execute("query-tpm"))
                    manager.stop("recovered", force=True)
                    process.wait(timeout=5)
                    tpm_process.wait(timeout=5)
                    checkpoints._verified_files(root / "vms/windows/checkpoints/clean")
                    self.assertTrue(any((root / "vms/windows/tpm").iterdir()))
                    restarted = manager.start("windows")
                    process = manager._processes["windows"]
                    tpm_process = manager._tpm_processes["windows"]
                    self.assertEqual(restarted["state"], "running")
                    manager.stop("windows", force=True)
                    process.wait(timeout=5)
                    tpm_process.wait(timeout=5)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if tpm_process is not None and tpm_process.poll() is None:
                    tpm_process.terminate()
                    tpm_process.wait(timeout=5)

    def check_viewer(self, root):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, "-m", "windows_terminal", "--root", str(root), "view", "--protocol", "blocks", "--fps", "1"], stdin=slave, stdout=slave, stderr=slave)
        try:
            output = bytearray()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and b"38;2;" not in output:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    output.extend(os.read(master, 65536))
                if process.poll() is not None:
                    self.fail(f"Viewer exited prematurely: {output.decode(errors='replace')}")
            self.assertIn(b"38;2;", output)
            os.write(master, b"\x1b[200~do not detach\x1d\x1b[201~")
            while time.monotonic() < deadline and b"Paste rejected:" not in output:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    output.extend(os.read(master, 65536))
                if process.poll() is not None:
                    self.fail("Pasted detach control exited the viewer")
            self.assertIn(b"Paste rejected:", output)
            self.assertIsNone(process.poll())
            os.write(master, b"\x1d")
            while process.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
            self.assertEqual(process.wait(timeout=2), 0)
            self.assertIn(b"\x1b[?1049l", output)
            restored = termios.tcgetattr(slave)
            restored[3] &= ~getattr(termios, "PENDIN", 0)
            original[3] &= ~getattr(termios, "PENDIN", 0)
            self.assertEqual(restored, original)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)

    def test_arm_real_guest_framebuffer_and_input(self):
        self.check_architecture("aarch64")

    def test_x86_real_guest_framebuffer_and_input(self):
        self.check_architecture("x86_64")

    @unittest.skipUnless(shutil.which("swtpm"), "swtpm is not installed")
    def test_arm_tpm_start_stop_and_persistence(self):
        self.check_architecture("aarch64", tpm=True)

    @unittest.skipUnless(shutil.which("swtpm"), "swtpm is not installed")
    def test_x86_tpm_start_stop_and_persistence(self):
        self.check_architecture("x86_64", tpm=True)


if __name__ == "__main__":
    unittest.main()
