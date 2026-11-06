"""Record a real, unedited windows-terminal session for the demo video.

Runs an interactive zsh inside a pseudo-terminal, types the commands a person
would type, clicks with real SGR mouse sequences, and drives the same VM from
an MCP client. Every byte the terminal receives is saved as an asciicast v2
file; markers (captions, MCP calls, speed-up ranges) go to a sidecar JSON file.
`demo/render.py` turns both into an MP4/GIF.

    python demo/record.py --iso ReactOS-Live.iso --out demo/out

The guest is ReactOS (open source, Windows-compatible) because it is freely
redistributable; with your own Windows ISO the flow is identical.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from windows_terminal.control import Computer  # noqa: E402
from windows_terminal.render import image_geometry  # noqa: E402
from windows_terminal.vm import VMManager  # noqa: E402

COLUMNS, ROWS = 120, 38


class Session:
    def __init__(self, environment, out):
        self.start = time.monotonic()
        self.events = []
        self.markers = []
        self.lock = threading.Lock()
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(environment["DEMO_CWD"])
            os.execvpe("zsh", ["zsh", "-f", "-i"], environment)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, COLUMNS * 11, ROWS * 22))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.out = out

    def now(self):
        return time.monotonic() - self.start

    def _read(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.events.append((self.now(), data))

    def mark(self, kind, **data):
        self.markers.append({"t": self.now(), "kind": kind, **data})

    def send(self, data: bytes):
        os.write(self.fd, data)

    def type(self, text, enter=True):
        for character in text:
            self.send(character.encode())
            time.sleep(random.uniform(0.035, 0.085) if character != " " else 0.09)
        if enter:
            time.sleep(0.35)
            self.send(b"\r")

    def save(self):
        header = {"version": 2, "width": COLUMNS, "height": ROWS, "timestamp": int(time.time()),
                  "env": {"TERM": "xterm-256color", "SHELL": "zsh"}, "title": "windows-terminal demo"}
        with (self.out / "session.cast").open("w") as stream:
            stream.write(json.dumps(header) + "\n")
            for moment, data in self.events:
                stream.write(json.dumps([round(moment, 4), "o", data.decode("utf-8", "replace")]) + "\n")
        (self.out / "markers.json").write_text(json.dumps(self.markers, indent=1))


def wait_until(predicate, timeout, interval=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError("guest did not reach the expected screen")


def near(pixel, target, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], target))


class MCPClient:
    def __init__(self, environment, session):
        self.process = subprocess.Popen(["windows-terminal", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, env=environment)
        self.session = session
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18"})

    def request(self, method, params):
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, name, label, **arguments):
        self.session.mark("mcp", tool=name, label=label)
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        if reply["result"].get("isError"):
            raise RuntimeError(reply["result"]["content"][0]["text"])
        return reply["result"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)


def main():
    options = argparse.ArgumentParser()
    options.add_argument("--iso", type=Path, required=True)
    options.add_argument("--out", type=Path, default=ROOT / "demo/out")
    args = options.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    iso = args.iso.resolve()
    workspace = Path(tempfile.mkdtemp(prefix="wt-rec-", dir="/tmp"))
    (workspace / "ReactOS-Live.iso").symlink_to(iso)
    vm_root = workspace / "vms"
    environment = {
        "PATH": f"{ROOT / '.venv/bin'}:/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(workspace), "TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app", "LANG": "en_US.UTF-8",
        "PS1": "%F{#7aa2f7}~/demo%f %F{#9ece6a}❯%f ", "WINDOWS_TERMINAL_ROOT": str(vm_root),
        "WINDOWS_TERMINAL_VM": "work", "DEMO_CWD": str(workspace),
    }
    session = Session(environment, out)
    manager = VMManager(vm_root)
    computer = None
    try:
        time.sleep(1.2)
        session.mark("step", index=0)
        session.type("windows-terminal init work --iso ReactOS-Live.iso --arch x86_64 --boot bios \\")
        session.type("    --memory 1024 --cpus 1 | jq -c '{name, arch, boot, network, state}'")
        time.sleep(2.5)

        session.mark("step", index=1)
        session.type("windows-terminal up --view --fps 10")
        wait_until(lambda: manager.status("work")["state"] == "running", 10, 0.2)
        computer = Computer(manager.qmp_path("work"))
        time.sleep(4)
        session.mark("speed", factor=12, label="booting")
        wait_until(lambda: computer.screenshot().size == (800, 600) and near(computer.screenshot().getpixel((400, 142)), (255, 255, 255), 12), 600, 2)
        time.sleep(1.5)
        session.mark("speed", factor=1)
        time.sleep(1.5)

        session.mark("step", index=2)
        session.mark("input", label="Enter → guest")
        session.send(b"\r")
        time.sleep(3.5)
        columns, rows = image_geometry(computer.screenshot(), COLUMNS - 1, ROWS - 3)
        column, row = 1 + 400 * columns // 800, 2 + 347 * rows // 600
        for index in range(8):  # glide the guest pointer towards the button, like a hand would
            x = 1 + (380 + (400 - 380) * index // 7) * columns // 800
            session.send(f"\x1b[<35;{x};{row + 5 - index * 5 // 7}M".encode())
            time.sleep(0.06)
        session.mark("input", label="click → guest")
        session.send(f"\x1b[<0;{column};{row}M".encode())
        time.sleep(0.12)
        session.send(f"\x1b[<0;{column};{row}m".encode())
        time.sleep(2)
        session.mark("speed", factor=8, label="loading desktop")
        wait_until(lambda: near(computer.screenshot().getpixel((400, 142)), (16, 62, 132), 30)
                   and near(computer.screenshot().getpixel((36, 18)), (255, 255, 255), 120), 300, 1.5)
        time.sleep(6)
        session.mark("speed", factor=1)
        time.sleep(1)

        session.mark("step", index=3)
        agent = MCPClient(environment, session)
        time.sleep(1.0)
        agent.call("screenshot", "screenshot()  →  800×600 PNG")
        time.sleep(1.4)
        agent.call("key", 'key("win+r")', chord="win+r")
        time.sleep(2.2)
        agent.call("type", 'type("notepad")', text="notepad")
        time.sleep(0.8)
        agent.call("key", 'key("enter")', chord="enter")
        time.sleep(5)
        message = "Hi! I'm an agent. This is my own PC. Your Mac's mouse and keyboard stay yours."
        agent.call("type", 'type("Hi! I\'m an agent…")', text=message)
        time.sleep(3)
        agent.call("key", 'key("ctrl+a")', chord="ctrl+a")
        time.sleep(2.5)
        agent.close()

        session.mark("step", index=4)
        session.mark("input", label="Ctrl+] → detach")
        session.send(b"\x1d")
        time.sleep(1.2)
        session.type("windows-terminal status | jq -c '{name, state, pid}'")
        time.sleep(2.0)
        session.type("windows-terminal down --force | jq -c '{name, state}'")
        time.sleep(2.5)
        session.mark("end")
        time.sleep(0.5)
    finally:
        try:
            if manager.status("work")["state"] != "stopped":
                manager.stop("work", force=True)
        except Exception as error:
            print("cleanup:", error, file=sys.stderr)
        session.save()
        os.kill(session.pid, 9)
        shutil.rmtree(workspace, ignore_errors=True)
    print(f"Recorded {session.now():.1f}s → {out / 'session.cast'}")


if __name__ == "__main__":
    main()
