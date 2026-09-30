from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import tomllib

# v0.4 keeps appearance as two independent concepts:
#   theme   -> whether the terminal default background or an app background is used
#   palette -> foreground / border / accent colors
THEMES = ("transparent", "solid")

# All of the original v0.2 looks are represented again.  The old
# `transparent` look did not have a separate palette name, so it is preserved
# as `classic` while transparency itself remains a theme.
PALETTES = ("classic", "spotify", "midnight", "mono")


@dataclass(slots=True)
class SpotCLIConfig:
    theme: str = "transparent"
    palette: str = "classic"


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "spotcli" / "config.toml"


def load_config() -> SpotCLIConfig:
    path = config_path()
    if not path.exists():
        return SpotCLIConfig()

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return SpotCLIConfig()

    raw_theme = str(data.get("theme", "transparent")).lower()
    raw_palette = str(data.get("palette", "")).lower()

    # v0.2 stored transparent/midnight/spotify/mono in one `theme` value.
    if raw_theme == "transparent" and not raw_palette:
        return SpotCLIConfig(theme="transparent", palette="classic")
    if raw_theme in ("midnight", "spotify", "mono"):
        return SpotCLIConfig(theme="solid", palette=raw_theme)

    theme = raw_theme if raw_theme in THEMES else "transparent"
    palette = raw_palette if raw_palette in PALETTES else "classic"
    return SpotCLIConfig(theme=theme, palette=palette)


def save_config(config: SpotCLIConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'theme = "{config.theme}"\n'
        f'palette = "{config.palette}"\n',
        encoding="utf-8",
    )
