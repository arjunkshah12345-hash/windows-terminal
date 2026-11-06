import base64
import io
import os

from PIL import Image


def detect_protocol():
    if os.environ.get("TMUX"):
        return "blocks"
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm"
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in os.environ.get("TERM", "") or os.environ.get("TERM_PROGRAM") in {"ghostty", "WezTerm"}:
        return "kitty"
    return "blocks"


def image_geometry(image, columns, rows, cell_width=8, cell_height=16):
    scale = min(columns * cell_width / image.width, rows * cell_height / image.height)
    width = max(1, min(columns, int(image.width * scale / cell_width)))
    height = max(1, min(rows, int(image.height * scale / cell_height)))
    return width, height


def render(image: Image.Image, columns: int, rows: int, protocol: str):
    columns, rows = max(1, columns), max(1, rows)
    if protocol not in {"kitty", "iterm", "blocks"}:
        raise ValueError(f"Unknown graphics protocol: {protocol}")
    if protocol == "blocks":
        pixels = image.convert("RGB").resize((columns, rows * 2), Image.Resampling.BILINEAR)
        lines = []
        for row in range(rows):
            line = []
            for column in range(columns):
                top = pixels.getpixel((column, row * 2))
                bottom = pixels.getpixel((column, row * 2 + 1))
                line.append(f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀")
            lines.append("".join(line) + "\x1b[0m")
        return "\r\n".join(lines)
    output = io.BytesIO()
    image.save(output, "PNG")
    payload = base64.b64encode(output.getvalue()).decode("ascii")
    if protocol == "iterm":
        return f"\x1b]1337;File=inline=1;width={columns};height={rows};preserveAspectRatio=0:{payload}\x07"
    chunks = [payload[index:index + 4096] for index in range(0, len(payload), 4096)]
    result = "\x1b_Ga=d,d=i,i=31,q=2\x1b\\"
    for index, chunk in enumerate(chunks):
        header = f"a=T,f=100,i=31,q=2,C=1,c={columns},r={rows}," if index == 0 else ""
        result += f"\x1b_G{header}m={int(index < len(chunks) - 1)};{chunk}\x1b\\"
    return result
