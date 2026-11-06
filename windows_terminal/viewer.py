import os
import re
import select
import shutil
import sys
import termios
import time
import tty

from .control import Computer
from .qmp import QMPError
from .render import detect_protocol, image_geometry, render


SEQUENCES = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[1~": "home", b"\x1b[4~": "end",
    b"\x1b[2~": "insert", b"\x1b[3~": "delete", b"\x1b[5~": "pageup", b"\x1b[6~": "pagedown",
    b"\x1bOP": "f1", b"\x1bOQ": "f2", b"\x1bOR": "f3", b"\x1bOS": "f4",
    b"\x1b[15~": "f5", b"\x1b[17~": "f6", b"\x1b[18~": "f7", b"\x1b[19~": "f8",
    b"\x1b[20~": "f9", b"\x1b[21~": "f10", b"\x1b[23~": "f11", b"\x1b[24~": "f12",
    b"\x1b[Z": "shift+tab",
}
MOUSE = re.compile(rb"^\x1b\[<(\d+);(\d+);(\d+)([mM])")
MODIFIED_KEY = re.compile(rb"^\x1b\[(\d+);([2-8])([A-DFH~])")
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class InputDecoder:
    def __init__(self):
        self.pending = b""
        self.paste = None
        self.paste_invalid = False

    def feed(self, data: bytes, flush=False):
        self.pending += data
        actions = []
        while self.pending:
            if self.paste is not None:
                end = self.pending.find(PASTE_END)
                count = end if end >= 0 else max(0, len(self.pending) - len(PASTE_END) + 1)
                chunk = self.pending[:count]
                self.pending = self.pending[count:]
                if not self.paste_invalid:
                    if len(self.paste) + len(chunk) > 20000 or any(value not in (9, 10, 13) and not 32 <= value < 127 for value in chunk):
                        self.paste_invalid = True
                        self.paste.clear()
                    else:
                        self.paste.extend(chunk)
                if end < 0:
                    break
                self.pending = self.pending[len(PASTE_END):]
                if self.paste_invalid:
                    actions.append(("paste_rejected", None))
                elif self.paste:
                    actions.append(("text", self.paste.decode("ascii")))
                self.paste = None
                self.paste_invalid = False
                continue
            if self.pending.startswith(b"\x1b"):
                if self.pending.startswith(PASTE_START):
                    self.pending = self.pending[len(PASTE_START):]
                    self.paste = bytearray()
                    continue
                modified = MODIFIED_KEY.match(self.pending)
                if modified:
                    number, modifier, ending = modified.groups()
                    sequence = b"\x1b[" + (number if ending == b"~" else b"") + ending
                    key = SEQUENCES.get(sequence) if ending == b"~" or number == b"1" else None
                    if key:
                        mask = int(modifier) - 1
                        modifiers = [name for bit, name in ((1, "shift"), (2, "alt"), (4, "ctrl")) if mask & bit]
                        actions.append(("key", "+".join([*modifiers, key])))
                    self.pending = self.pending[modified.end():]
                    continue
                mouse = MOUSE.match(self.pending)
                if mouse:
                    code, column, row = (int(value) for value in mouse.groups()[:3])
                    actions.append(("mouse", (code, column, row, mouse[4] == b"M")))
                    self.pending = self.pending[mouse.end():]
                    continue
                consumed = False
                for sequence, key in SEQUENCES.items():
                    if self.pending.startswith(sequence):
                        actions.append(("key", key))
                        self.pending = self.pending[len(sequence):]
                        consumed = True
                        break
                if consumed:
                    continue
                markers = (PASTE_START, PASTE_END)
                marker = next((value for value in markers if self.pending.startswith(value)), None)
                if marker:
                    self.pending = self.pending[len(marker):]
                    continue
                if not flush and (self.pending == b"\x1b" or self.pending.startswith(b"\x1b[<") or re.fullmatch(rb"\x1b\[[0-9;?]*", self.pending) or any(value.startswith(self.pending) for value in (*SEQUENCES, *markers))):
                    break
                if self.pending.startswith((b"\x1b[", b"\x1bO")):
                    sequence = re.match(rb"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|O.)", self.pending)
                    if sequence:
                        self.pending = self.pending[sequence.end():]
                        continue
                    self.pending = b""
                    break
                actions.append(("key", "esc"))
                self.pending = self.pending[1:]
                continue
            value = self.pending[0]
            self.pending = self.pending[1:]
            if value == 29:
                actions.append(("detach", None))
            elif value in {8, 127}:
                actions.append(("key", "backspace"))
            elif value in {10, 13}:
                actions.append(("key", "enter"))
            elif value == 9:
                actions.append(("key", "tab"))
            elif 1 <= value <= 26:
                actions.append(("key", "ctrl+" + chr(value + 96)))
            elif 32 <= value < 127:
                if actions and actions[-1][0] == "text":
                    actions[-1] = ("text", actions[-1][1] + chr(value))
                else:
                    actions.append(("text", chr(value)))
            elif value >= 128:
                actions.append(("unsupported", None))
        return actions


def view(computer: Computer, name: str, protocol="auto", fps=5):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("The viewer needs an interactive terminal. Use screenshot or MCP when running headless.")
    if not 1 <= fps <= 15:
        raise ValueError("FPS must be between 1 and 15.")
    protocol = detect_protocol() if protocol == "auto" else protocol
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    decoder = InputDecoder()
    next_frame = 0
    last_input = 0
    width, height = 1, 1
    columns, rows = 1, 1
    held = set()
    geometry = None
    message = "Keyboard + mouse → guest  ·  Ctrl+] detach (VM keeps running)"
    try:
        tty.setraw(descriptor)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?2004h\x1b[2J")
        sys.stdout.flush()
        while True:
            now = time.monotonic()
            if now >= next_frame:
                image = computer.screenshot()
                width, height = image.size
                size = shutil.get_terminal_size((100, 32))
                columns, rows = image_geometry(image, max(1, size.columns - 1), max(1, size.lines - 3))
                current_geometry = (size.columns, size.lines, columns, rows)
                if geometry != current_geometry:
                    sys.stdout.write("\x1b[2J")
                    geometry = current_geometry
                label = f"  ▦ windows-terminal   /   {name}   ·   {width}×{height}   ·   {protocol}"
                sys.stdout.write("\x1b[H\x1b[2K\x1b[48;2;24;28;34m\x1b[38;2;222;228;237m" + label[:size.columns - 1] + "\x1b[0m")
                sys.stdout.write("\x1b[2;1H" + render(image, columns, rows, protocol))
                sys.stdout.write(f"\x1b[{size.lines};1H\x1b[2K\x1b[38;2;145;158;173m" + message[:size.columns - 1] + "\x1b[0m")
                sys.stdout.flush()
                next_frame = time.monotonic() + 1 / fps
            readable, _, _ = select.select([descriptor], [], [], max(0, min(0.04, next_frame - time.monotonic())))
            if readable:
                data = os.read(descriptor, 4096)
                if not data:
                    return
                actions = decoder.feed(data)
                last_input = time.monotonic()
            elif decoder.pending and time.monotonic() - last_input >= 0.04:
                actions = decoder.feed(b"", flush=True)
            else:
                actions = []
            for kind, value in actions:
                if kind == "detach":
                    return
                try:
                    if kind == "key":
                        computer.key(value)
                    elif kind == "text":
                        computer.type(value, interval=0)
                    elif kind == "unsupported":
                        message = "US-layout ASCII input only; non-ASCII input was not sent."
                    elif kind == "paste_rejected":
                        message = "Paste rejected: ASCII text only, no control codes, maximum 20,000 characters."
                    elif kind == "mouse":
                        code, column, row, down = value
                        if not (1 <= column <= columns and 2 <= row < rows + 2):
                            if not down:
                                for button in tuple(held):
                                    computer.button(button, False)
                                    held.discard(button)
                            continue
                        computer.move(min(width - 1, int((column - 1) * width / columns)), min(height - 1, int((row - 2) * height / rows)), width, height)
                        if code & 64:
                            computer.scroll("down" if code & 1 else "up", 1)
                        elif not code & 32:
                            button = {0: "left", 1: "middle", 2: "right"}.get(code & 3)
                            if button:
                                computer.button(button, down)
                                if down:
                                    held.add(button)
                                else:
                                    held.discard(button)
                except (ValueError, QMPError) as error:
                    message = str(error)
    finally:
        for button in held:
            try:
                computer.button(button, False)
            except (OSError, QMPError):
                pass
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        sys.stdout.write("\x1b_Ga=d,d=i,i=31,q=2\x1b\\\x1b[0m\x1b[?2004l\x1b[?1006l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
