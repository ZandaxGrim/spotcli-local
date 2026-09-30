from __future__ import annotations

from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageEnhance, ImageOps
from rich.style import Style
from rich.text import Text


def render_album_art(data: bytes | None, width: int = 28, rows: int = 10) -> Text:
    """Render album art with true-color Unicode half blocks.

    Each terminal cell carries two independently colored vertical pixels.  The
    renderer keeps considerably more cells than v0.2/v0.3 and applies a very
    light post-resize sharpness pass, which makes small cover text/edges less
    muddy without inventing image detail.
    """
    width = max(4, int(width))
    rows = max(2, int(rows))
    if not data:
        return _placeholder(width, rows)

    try:
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(
                image,
                (width, rows * 2),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image = ImageEnhance.Sharpness(image).enhance(1.12)
    except Exception:
        return _placeholder(width, rows)

    output = Text(no_wrap=True)
    for y in range(0, rows * 2, 2):
        for x in range(width):
            top = image.getpixel((x, y))
            bottom = image.getpixel((x, y + 1))
            output.append(
                "▀",
                style=Style(
                    color=f"rgb({top[0]},{top[1]},{top[2]})",
                    bgcolor=f"rgb({bottom[0]},{bottom[1]},{bottom[2]})",
                ),
            )
        if y < (rows - 1) * 2:
            output.append("\n")
    return output


def _placeholder(width: int, rows: int) -> Text:
    lines = []
    label = "album art"
    middle = rows // 2
    for row in range(rows):
        if row == middle and width >= len(label):
            left = max(0, (width - len(label)) // 2)
            line = " " * left + label
            line += " " * max(0, width - len(line))
        else:
            line = " " * width
        lines.append(line)
    return Text("\n".join(lines), style="dim")


def progress_line(position_seconds: float, duration_seconds: float, width: int = 34) -> str:
    """Render the original whole-cell progress bar used before v0.6."""
    width = max(4, int(width))
    if duration_seconds <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, position_seconds / duration_seconds))

    filled = round(width * ratio)
    bar = "━" * filled + "─" * (width - filled)
    return f"{bar}  {_clock(position_seconds)} / {_clock(duration_seconds)}"

def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
