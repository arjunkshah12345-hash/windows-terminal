"""Render a recorded asciicast (plus markers) into the demo MP4.

Terminal text is emulated with pyte; iTerm2 inline images written by the
viewer are decoded and drawn at the cell where the terminal would place them.
Nothing in the terminal pane is mocked: it is a replay of the recorded bytes.
Boot waits are time-lapsed and labelled as such.

    python demo/render.py demo/out --output demo/out/demo.mp4
"""

import argparse
import base64
import bisect
import io
import json
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import pyte

WIDTH, HEIGHT, FPS = 1920, 1080, 30
CELL_W, CELL_H = 11, 22
TERM_X, TERM_Y, PAD, BAR = 56, 70, 14, 40
PANEL_X = 1452
MONO = "/System/Library/Fonts/Menlo.ttc"
SANS = "/System/Library/Fonts/SFNS.ttf"

BG = (13, 16, 23)
TERM_BG = (17, 20, 28)
TEXT = (205, 214, 230)
ACCENT = (122, 162, 247)
GREEN = (158, 206, 106)
MUTED = (110, 120, 140)
NAMED = {
    "black": (21, 22, 30), "red": (247, 118, 142), "green": (158, 206, 106), "brown": (224, 175, 104),
    "yellow": (224, 175, 104), "blue": (122, 162, 247), "magenta": (187, 154, 247), "cyan": (125, 207, 255),
    "white": (192, 202, 245), "brightblack": (65, 72, 104), "brightred": (247, 118, 142),
    "brightgreen": (158, 206, 106), "brightyellow": (224, 175, 104), "brightblue": (122, 162, 247),
    "brightmagenta": (187, 154, 247), "brightcyan": (125, 207, 255), "brightwhite": (220, 225, 245),
}
STEPS = [
    ("Create a VM", "from your own ISO. Private disk, no network."),
    ("Boot it in the terminal", "real QEMU, rendered as inline graphics"),
    ("You drive", "keyboard + mouse go to the guest only"),
    ("An agent drives", "same VM, nine computer-use tools over MCP"),
    ("Detach", "Ctrl+] — the VM keeps running"),
]
TOKEN = re.compile(rb"\x1b\[\?1049([hl])|\x1b\]1337;File=([^:\x07]*):([A-Za-z0-9+/=]*)\x07|\x1b_G[^\x1b]*\x1b\\|(\x1b\[2J)")


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def color(value, default):
    if value == "default":
        return default
    if value in NAMED:
        return NAMED[value]
    if re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    return default


def load(directory):
    lines = (directory / "session.cast").read_text().splitlines()
    header = json.loads(lines[0])
    times, chunks = [], []
    for line in lines[1:]:
        moment, _, data = json.loads(line)
        times.append(moment)
        chunks.append(data.encode("utf-8"))
    blob = b"".join(chunks)
    starts, offset = [], 0
    for chunk in chunks:
        starts.append(offset)
        offset += len(chunk)

    def time_of(position):
        return times[max(0, bisect.bisect_right(starts, position) - 1)]

    def text(begin, end):
        # Keep the recorded timing: split plain output at the original chunk boundaries.
        index = max(0, bisect.bisect_right(starts, begin) - 1)
        while begin < end:
            boundary = min(end, starts[index + 1] if index + 1 < len(starts) else end)
            if boundary > begin:
                operations.append((times[index], "text", blob[begin:boundary]))
            begin, index = boundary, index + 1

    operations, cursor = [], 0
    for match in TOKEN.finditer(blob):
        if match.start() > cursor:
            text(cursor, match.start())
        moment = time_of(match.end() - 1)
        if match.group(1):
            operations.append((moment, "alt", match.group(1) == b"h"))
        elif match.group(2) is not None:
            parameters = dict(item.split("=", 1) for item in match.group(2).decode().split(";") if "=" in item)
            operations.append((moment, "image", (int(parameters["width"]), int(parameters["height"]), match.group(3))))
        elif match.group(4):
            operations.append((moment, "clear", None))
            operations.append((moment, "text", match.group(4)))
        cursor = match.end()
    if cursor < len(blob):
        text(cursor, len(blob))
    markers = json.loads((directory / "markers.json").read_text())
    return header, operations, markers


class Terminal:
    def __init__(self, columns, rows):
        self.main, self.alt = pyte.Screen(columns, rows), pyte.Screen(columns, rows)
        self.streams = {id(self.main): pyte.ByteStream(self.main), id(self.alt): pyte.ByteStream(self.alt)}
        self.screen = self.main
        self.images = {}
        self.version = 0

    def apply(self, kind, payload):
        self.version += 1
        if kind == "text":
            self.streams[id(self.screen)].feed(payload)
        elif kind == "alt":
            self.images.clear()
            if payload:
                self.alt.reset()
                self.screen = self.alt
            else:
                self.screen = self.main
        elif kind == "clear":
            self.images.clear()
        elif kind == "image":
            columns, rows, data = payload
            position = (self.screen.cursor.y, self.screen.cursor.x)
            self.images[position] = (columns, rows, data)


def decode(data, cache={}):
    key = hash(data)
    if key not in cache:
        cache.clear()
        cache[key] = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
    return cache[key]


def draw_terminal(terminal, blink):
    screen = terminal.screen
    canvas = Image.new("RGB", (screen.columns * CELL_W, screen.lines * CELL_H), TERM_BG)
    draw = ImageDraw.Draw(canvas)
    regular, bold = font(MONO, 18), font(MONO, 18, 1)
    for row in range(screen.lines):
        line = screen.buffer[row]
        for column in range(screen.columns):
            character = line[column]
            foreground, background = color(character.fg, TEXT), color(character.bg, TERM_BG)
            if character.reverse:
                foreground, background = background, foreground
            x, y = column * CELL_W, row * CELL_H
            if background != TERM_BG:
                draw.rectangle((x, y, x + CELL_W - 1, y + CELL_H - 1), fill=background)
            if character.data not in (" ", ""):
                draw.text((x, y + 2), character.data, font=bold if character.bold else regular, fill=foreground)
    for (row, column), (columns, rows, data) in terminal.images.items():
        image = decode(data).resize((columns * CELL_W, rows * CELL_H), Image.Resampling.LANCZOS)
        canvas.paste(image, (column * CELL_W, row * CELL_H))
    if not screen.cursor.hidden and terminal.screen is terminal.main and blink:
        x, y = screen.cursor.x * CELL_W, screen.cursor.y * CELL_H
        draw.rectangle((x, y + 2, x + CELL_W - 1, y + CELL_H - 2), fill=(192, 202, 245))
    return canvas


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def base_layer(columns, rows):
    base = Image.new("RGB", (WIDTH, HEIGHT), BG)
    glow = Image.new("RGB", (WIDTH, HEIGHT), BG)
    ImageDraw.Draw(glow).ellipse((300, -300, 1500, 700), fill=(28, 40, 72))
    ImageDraw.Draw(glow).ellipse((1200, 600, 2300, 1400), fill=(30, 26, 54))
    base = Image.blend(base, glow.filter(ImageFilter.GaussianBlur(220)), 0.85)
    window = (columns * CELL_W + PAD * 2, rows * CELL_H + BAR + PAD * 2)
    shadow = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((TERM_X + 6, TERM_Y + 22, TERM_X + window[0] + 6, TERM_Y + window[1] + 22), 14, fill=170)
    base.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(28)))
    frame = Image.new("RGB", window, TERM_BG)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, window[0], BAR), fill=(28, 31, 42))
    draw.line((0, BAR, window[0], BAR), fill=(40, 44, 58))
    for index, dot in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
        draw.ellipse((18 + index * 22, 14, 30 + index * 22, 26), fill=dot)
    title = "zsh — windows-terminal — 120×38"
    title_font = font(SANS, 15)
    width = draw.textlength(title, font=title_font)
    draw.text(((window[0] - width) / 2, 11), title, font=title_font, fill=(150, 158, 178))
    base.paste(frame, (TERM_X, TERM_Y), rounded_mask(window, 14))
    ImageDraw.Draw(base).rounded_rectangle((TERM_X, TERM_Y, TERM_X + window[0] - 1, TERM_Y + window[1] - 1), 14, outline=(48, 53, 70))
    draw = ImageDraw.Draw(base)
    draw.text((PANEL_X, TERM_Y - 4), "windows-terminal", font=font(SANS, 40), fill=(236, 240, 250))
    draw.text((PANEL_X, TERM_Y + 48), "A separate PC for your agent.\nRight inside your terminal.", font=font(SANS, 21), fill=(150, 160, 182), spacing=6)
    footer = font(SANS, 17)
    draw.text((TERM_X, HEIGHT - 52), "github.com/arjunkshah12345-hash/windows-terminal", font=footer, fill=(150, 160, 182))
    note = "Guest shown: ReactOS 0.4.15 (open-source, Windows-compatible). Bring your own Windows ISO for Windows 11."
    draw.text((WIDTH - 56 - draw.textlength(note, font=footer), HEIGHT - 52), note, font=footer, fill=(96, 106, 128))
    return base


def overlay(frame, state):
    draw = ImageDraw.Draw(frame, "RGBA")
    title, body, number = font(SANS, 22), font(SANS, 16), font(SANS, 16)
    y = TERM_Y + 140
    for index, (heading, detail) in enumerate(STEPS):
        active, done = index == state["step"], index < state["step"]
        if active:
            draw.rounded_rectangle((PANEL_X - 14, y - 12, WIDTH - 44, y + 62), 12, fill=(122, 162, 247, 26), outline=(122, 162, 247, 90))
        circle = ACCENT if active else (GREEN if done else (60, 66, 84))
        draw.ellipse((PANEL_X, y + 2, PANEL_X + 26, y + 28), fill=circle)
        label = "✓" if done else str(index + 1)
        draw.text((PANEL_X + 13, y + 15), label, font=number, fill=(13, 16, 23), anchor="mm")
        draw.text((PANEL_X + 40, y), heading, font=title, fill=(236, 240, 250) if active or done else (120, 128, 148))
        draw.text((PANEL_X + 40, y + 30), detail, font=body, fill=(150, 160, 182) if active else (90, 98, 118))
        y += 92
    log_top = y + 6
    if state["mcp"]:
        mono = font(MONO, 15)
        draw.rounded_rectangle((PANEL_X - 14, log_top, WIDTH - 44, log_top + 44 + 30 * 7), 12, fill=(10, 12, 18, 235), outline=(48, 53, 70))
        draw.text((PANEL_X + 2, log_top + 12), "AGENT  →  windows-terminal mcp", font=font(SANS, 15), fill=(150, 160, 182))
        for index, (label, age) in enumerate(state["mcp"][-6:]):
            fresh = age < 1.2
            line_y = log_top + 46 + index * 30
            draw.text((PANEL_X + 2, line_y), "→", font=mono, fill=GREEN if fresh else MUTED)
            draw.text((PANEL_X + 24, line_y), label, font=mono, fill=(236, 240, 250) if fresh else (150, 160, 182))
    if state["speed"]:
        text = f"⏩  {state['speed']}  ·  time-lapse"
        pill = font(SANS, 16)
        width = draw.textlength(text, font=pill)
        right = TERM_X + 120 * CELL_W + PAD * 2 - 16
        draw.rounded_rectangle((right - width - 28, TERM_Y + 7, right, TERM_Y + 33), 13, fill=(224, 175, 104, 40), outline=(224, 175, 104, 160))
        draw.text((right - width - 14, TERM_Y + 10), text, font=pill, fill=(240, 200, 130))
    if state["input"]:
        label, age = state["input"]
        alpha = int(255 * max(0.0, min(1.0, (1.8 - age) / 0.4)))
        chip = font(SANS, 20)
        width = draw.textlength(label, font=chip)
        x = TERM_X + (120 * CELL_W + PAD * 2 - width) / 2
        y = TERM_Y + BAR + 38 * CELL_H - 30
        draw.rounded_rectangle((x - 20, y - 8, x + width + 20, y + 30), 19, fill=(122, 162, 247, int(alpha * 0.9)))
        draw.text((x, y - 2), label, font=chip, fill=(13, 16, 23, alpha))
    if state["end"] is not None:
        alpha = min(1.0, state["end"] / 0.6)
        draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(8, 10, 15, int(220 * alpha)))
        big, small = font(SANS, 84), font(SANS, 28)
        fill = (236, 240, 250, int(255 * alpha))
        draw.text((WIDTH / 2, HEIGHT / 2 - 70), "windows-terminal", font=big, fill=fill, anchor="mm")
        draw.text((WIDTH / 2, HEIGHT / 2 + 10), "A separate PC for your agent. Right inside your terminal.", font=small, fill=(150, 160, 182, int(255 * alpha)), anchor="mm")
        draw.text((WIDTH / 2, HEIGHT / 2 + 70), "github.com/arjunkshah12345-hash/windows-terminal", font=font(MONO, 24), fill=(122, 162, 247, int(255 * alpha)), anchor="mm")


def timeline(markers, duration):
    """Map output time → source time, compressing marked speed-up ranges."""
    segments, start, factor, label = [], 0.0, 1.0, None
    for marker in markers:
        if marker["kind"] == "speed":
            segments.append((start, marker["t"], factor, label))
            start, factor, label = marker["t"], float(marker["factor"]), marker.get("label")
    segments.append((start, duration, factor, label))
    output, mapped = 0.0, []
    for begin, end, speed, name in segments:
        length = (end - begin) / speed
        mapped.append((output, output + length, begin, speed, f"{name} {int(speed)}×" if speed > 1 else None))
        output += length
    return mapped, output


def main():
    options = argparse.ArgumentParser()
    options.add_argument("directory", type=Path)
    options.add_argument("--output", type=Path)
    options.add_argument("--poster", type=Path)
    args = options.parse_args()
    header, operations, markers = load(args.directory)
    end_marker = next((m["t"] for m in markers if m["kind"] == "end"), operations[-1][0])
    mapped, total = timeline(markers, end_marker)
    hold = 4.0
    terminal = Terminal(header["width"], header["height"])
    base = base_layer(header["width"], header["height"])
    output = args.output or args.directory / "demo.mp4"
    encoder = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
                                "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                str(output)], stdin=subprocess.PIPE)
    index, cached_key, cached, poster_done = 0, None, None, False
    frames = int((total + hold) * FPS)
    for frame_number in range(frames):
        moment = frame_number / FPS
        segment = next((s for s in mapped if s[0] <= moment < s[1]), mapped[-1])
        source = min(end_marker, segment[2] + (moment - segment[0]) * segment[3])
        while index < len(operations) and operations[index][0] <= source:
            terminal.apply(operations[index][1], operations[index][2])
            index += 1
        past = [m for m in markers if m["t"] <= source]
        steps = [m["index"] for m in past if m["kind"] == "step"]
        inputs = [m for m in past if m["kind"] == "input"]
        latest_input = inputs[-1] if inputs and source - inputs[-1]["t"] < 1.8 else None
        state = {
            "step": steps[-1] if steps else 0,
            "mcp": [(m["label"], source - m["t"]) for m in past if m["kind"] == "mcp"],
            "speed": segment[4],
            "input": (latest_input["label"], source - latest_input["t"]) if latest_input else None,
            "end": moment - total if moment >= total + 0.4 else None,
        }
        blink = int(moment * 2) % 2 == 0
        key = (terminal.version, blink, json.dumps(state, default=str) if state["end"] is not None or state["input"] or any(age < 1.2 for _, age in state["mcp"]) else
               (state["step"], len(state["mcp"]), state["speed"]))
        if key != cached_key:
            frame = base.copy()
            frame.paste(draw_terminal(terminal, blink), (TERM_X + PAD, TERM_Y + BAR + PAD))
            overlay(frame, state)
            cached, cached_key = frame.tobytes(), key
            if args.poster and not poster_done and state["step"] == 3 and len(state["mcp"]) >= 5 and not any(age < 1.2 for _, age in state["mcp"]):
                frame.save(args.poster)
                poster_done = True
        encoder.stdin.write(cached)
    encoder.stdin.close()
    encoder.wait()
    print(f"{output}  {total + hold:.1f}s  {frames} frames")


if __name__ == "__main__":
    main()
