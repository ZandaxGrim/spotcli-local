from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import tomllib

THEMES = ("transparent", "solid")
PALETTES = (
    "classic", "spotify", "midnight", "mono",
    "purple", "ocean", "sunset", "cyberpunk",
    "rose", "amber", "forest", "ice",
)


@dataclass(slots=True)
class Keybinds:
    play_pause: str = "k"
    next: str = "n"
    previous: str = "p"
    search: str = "/"
    queue_selected: str = "a"
    play_playlist: str = "x"
    playlists: str = "l"
    queue_view: str = "u"
    shuffle: str = "s"
    refresh_cache: str = "r"
    volume_down: str = "["
    volume_up: str = "]"
    theme: str = "t"
    palette: str = "c"
    quit: str = "q"


@dataclass(slots=True)
class SpotCLIConfig:
    theme: str = "transparent"
    palette: str = "classic"
    volume_step: int = 5
    keys: Keybinds = field(default_factory=Keybinds)


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

    # v0.2 stored transparent/midnight/spotify/mono in one theme value.
    if raw_theme == "transparent" and not raw_palette:
        theme, palette = "transparent", "classic"
    elif raw_theme in ("midnight", "spotify", "mono"):
        theme, palette = "solid", raw_theme
    else:
        theme = raw_theme if raw_theme in THEMES else "transparent"
        palette = raw_palette if raw_palette in PALETTES else "classic"

    try:
        volume_step = max(1, min(25, int(data.get("volume_step", 5))))
    except (TypeError, ValueError):
        volume_step = 5

    defaults = Keybinds()
    raw_keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    keys = Keybinds(
        **{
            name: _key_value(raw_keys.get(name), getattr(defaults, name))
            for name in defaults.__dataclass_fields__
        }
    )
    return SpotCLIConfig(theme=theme, palette=palette, volume_step=volume_step, keys=keys)


def save_config(config: SpotCLIConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'theme = "{config.theme}"',
        f'palette = "{config.palette}"',
        f"volume_step = {config.volume_step}",
        "",
        "[keys]",
    ]
    for name in config.keys.__dataclass_fields__:
        value = getattr(config.keys, name).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{name} = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _key_value(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    # Command bindings are intentionally one printable character. Navigation
    # still uses Enter/Esc/Backspace/arrow keys and is not remapped here.
    return text[0].lower() if text and text[0].isprintable() else fallback
