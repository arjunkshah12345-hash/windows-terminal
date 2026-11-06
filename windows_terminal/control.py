import io
import math
import tempfile
import time
from pathlib import Path

from PIL import Image

from .qmp import QMPClient, QMPError


ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "win": "meta_l", "super": "meta_l", "windows": "meta_l", "meta": "meta_l",
    "enter": "ret", "return": "ret", "escape": "esc", "esc": "esc",
    "backspace": "backspace", "delete": "delete", "tab": "tab", "space": "spc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pgup", "pagedown": "pgdn",
    "insert": "insert", "capslock": "caps_lock", "printscreen": "print",
    **{f"f{number}": f"f{number}" for number in range(1, 13)},
    **{character: character for character in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
PLAIN = dict(zip("`-=[]\\;',./", ["grave_accent", "minus", "equal", "bracket_left", "bracket_right", "backslash", "semicolon", "apostrophe", "comma", "dot", "slash"]))
SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', ["grave_accent", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "minus", "equal", "bracket_left", "bracket_right", "backslash", "semicolon", "apostrophe", "comma", "dot", "slash"]))


def key_event(code, down):
    return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": code}}}


def text_chords(text: str):
    chords = []
    for character in text.replace("\r\n", "\n"):
        if character in "\n\r\t \b":
            chords.append([{"\n": "ret", "\r": "ret", "\t": "tab", " ": "spc", "\b": "backspace"}[character]])
        elif character in PLAIN:
            chords.append([PLAIN[character]])
        elif character in SHIFTED:
            chords.append(["shift", SHIFTED[character]])
        elif character.isascii() and character.isalnum():
            chords.append((["shift"] if character.isupper() else []) + [character.lower()])
        else:
            raise ValueError("Typing supports US-layout ASCII text only; unsupported character: " + repr(character))
    return chords


class Computer:
    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)

    def screenshot(self) -> Image.Image:
        with tempfile.TemporaryDirectory(prefix="wt-screen-") as folder:
            target = Path(folder) / "screen.ppm"
            with QMPClient(self.socket_path) as client:
                client.execute("screendump", {"filename": str(target)})
            with Image.open(target) as image:
                return image.convert("RGB")

    def png(self) -> bytes:
        output = io.BytesIO()
        self.screenshot().save(output, format="PNG")
        return output.getvalue()

    def _chords(self, chords, interval=0):
        batch_size = max(1, min(32, int(0.25 / max(interval, 0.001))))
        for offset in range(0, len(chords), batch_size):
            uncertain_codes = None
            try:
                with QMPClient(self.socket_path) as client:
                    for codes in chords[offset:offset + batch_size]:
                        uncertain_codes = codes
                        client.execute("input-send-event", {"events": [key_event(code, True) for code in codes]})
                        client.execute("input-send-event", {"events": [key_event(code, False) for code in reversed(codes)]})
                        uncertain_codes = None
                        if interval:
                            time.sleep(interval)
            except (QMPError, OSError, KeyboardInterrupt):
                if uncertain_codes:
                    try:
                        with QMPClient(self.socket_path, timeout=1) as recovery:
                            recovery.execute("input-send-event", {"events": [key_event(code, False) for code in reversed(uncertain_codes)]})
                    except (QMPError, OSError):
                        pass
                raise
            if offset + batch_size < len(chords):
                time.sleep(0.01)

    def type(self, text: str, interval: float = 0.01):
        if len(text) > 20000:
            raise ValueError("Text is limited to 20,000 characters per call.")
        if not math.isfinite(interval) or not 0 <= interval <= 1:
            raise ValueError("Typing interval must be between 0 and 1 second.")
        self._chords(text_chords(text), interval)

    def key(self, chord: str):
        parts = chord.lower().split("+")
        if not parts or any(part not in ALIASES for part in parts):
            raise ValueError(f"Unknown key chord: {chord}. Example: ctrl+alt+delete, win+r, enter.")
        self._chords([[ALIASES[part] for part in parts]])

    def move(self, x: int, y: int, width: int | None = None, height: int | None = None):
        if width is None or height is None:
            width, height = self.screenshot().size
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"Coordinates must be inside the {width}×{height} guest display.")
        events = [{"type": "abs", "data": {"axis": axis, "value": round(position * 32767 / max(1, extent - 1))}} for axis, position, extent in [("x", x, width), ("y", y, height)]]
        with QMPClient(self.socket_path) as client:
            client.execute("input-send-event", {"events": events})

    def button(self, button: str, down: bool):
        if button not in {"left", "middle", "right", "wheel-up", "wheel-down"}:
            raise ValueError("Button must be left, middle, right, wheel-up, or wheel-down.")
        with QMPClient(self.socket_path) as client:
            client.execute("input-send-event", {"events": [{"type": "btn", "data": {"down": down, "button": button}}]})

    def click(self, x: int, y: int, button: str = "left", count: int = 1):
        if count not in {1, 2}:
            raise ValueError("Click count must be 1 or 2.")
        if button not in {"left", "middle", "right"}:
            raise ValueError("Click button must be left, middle, or right.")
        self.move(x, y)
        for _ in range(count):
            try:
                self.button(button, True)
            finally:
                self.button(button, False)
            if count == 2:
                time.sleep(0.08)

    def scroll(self, direction: str, steps: int = 3):
        if direction not in {"up", "down"} or not 1 <= steps <= 100:
            raise ValueError("Scroll direction is up/down; steps must be 1–100.")
        for _ in range(steps):
            self.button("wheel-" + direction, True)
            self.button("wheel-" + direction, False)
