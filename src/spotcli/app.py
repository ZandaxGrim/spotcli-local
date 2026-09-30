from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .backends.spotify_player import SearchRateLimited, SearchResult, SpotifyPlayerBackend
from .config import PALETTES, THEMES, SpotCLIConfig, load_config, save_config
from .media.windows import TrackInfo, WindowsMediaSession
from .rendering import progress_line, render_album_art


@dataclass(frozen=True, slots=True)
class Palette:
    fg: str
    muted: str
    accent: str
    border: str
    solid_bg: str
    solid_panel: str


PALETTE_STYLES: dict[str, Palette] = {
    # The color treatment used by the original v0.2 transparent mode.
    "classic": Palette(
        fg="#f5f5f5",
        muted="#b3b3b3",
        accent="#1db954",
        border="#535353",
        solid_bg="#121212",
        solid_panel="#181818",
    ),
    "spotify": Palette(
        fg="#f5f5f5",
        muted="#b3b3b3",
        accent="#1ed760",
        border="#535353",
        solid_bg="#121212",
        solid_panel="#181818",
    ),
    "midnight": Palette(
        fg="#d8deea",
        muted="#a9b1d6",
        accent="#7aa2f7",
        border="#565f89",
        solid_bg="#090b10",
        solid_panel="#111722",
    ),
    "mono": Palette(
        fg="#eeeeee",
        muted="#bdbdbd",
        accent="#eeeeee",
        border="#eeeeee",
        solid_bg="#000000",
        solid_panel="#000000",
    ),
}


class SpotCLI:
    """Rich/ANSI front-end for spotcli.

    v0.4 intentionally does not use Textual for the main renderer. Textual's
    compositor paints true-color background cells, which prevents Windows
    Terminal's own acrylic / opacity from showing through consistently. Rich
    can leave the terminal background at its *default* value, so transparent
    mode is genuinely terminal-native rather than simulated with RGB alpha.
    """

    def __init__(self) -> None:
        self.console = Console(style="default on default", highlight=False)
        self.media = WindowsMediaSession()
        self.search_backend = SpotifyPlayerBackend()
        self.config: SpotCLIConfig = load_config()
        self.info = TrackInfo()
        self.search_query = ""
        self.search_mode = False
        self.search_results: list[SearchResult] = []
        self.selected_result_index: int | None = None
        self.status_message = ""
        self.running = True
        self._progress_anchor_position = 0.0
        self._progress_anchor_time = time.monotonic()
        self._progress_track_key: tuple[str, str, float] | None = None
        self._progress_playing = False
        self._last_raw_position: float | None = None
        self._media_poll_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        if os.name != "nt":
            raise RuntimeError("spotcli-local currently requires Windows")

        await self.media.start()
        await self.refresh_now_playing()
        self._media_poll_task = asyncio.create_task(self._poll_media(), name="spotcli-media-poll")

        # Rendering and GSMTC polling deliberately run independently. Some
        # Windows media-property calls can take noticeable time; if we await
        # them inside the paint loop the progress display freezes with them.
        try:
            with Live(
                self.render(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                transient=False,
            ) as live:
                while self.running:
                    await self._read_keys()
                    live.update(self.render(), refresh=True)
                    # 10 FPS is plenty for fractional-cell progress animation
                    # and keeps terminal CPU usage modest.
                    await asyncio.sleep(0.10)
        finally:
            if self._media_poll_task is not None:
                self._media_poll_task.cancel()
                try:
                    await self._media_poll_task
                except asyncio.CancelledError:
                    pass

    async def _poll_media(self) -> None:
        while self.running:
            await self.refresh_now_playing()
            await asyncio.sleep(0.75)

    async def refresh_now_playing(self) -> None:
        try:
            incoming = await self.media.now_playing()
            now = time.monotonic()
            track_key = (incoming.title, incoming.artist, round(incoming.duration_seconds, 2))

            raw_changed = (
                self._last_raw_position is None
                or abs(incoming.position_seconds - self._last_raw_position) >= 0.20
            )

            if self._progress_track_key != track_key:
                # A new song is authoritative: reset immediately.
                self._progress_track_key = track_key
                self._progress_anchor_position = incoming.position_seconds
                self._progress_anchor_time = now
            elif incoming.playing != self._progress_playing:
                # Pause/resume transitions should snap to Windows' current value.
                self._progress_anchor_position = incoming.position_seconds
                self._progress_anchor_time = now
            elif raw_changed:
                # GSMTC commonly leaves the exact same position untouched for
                # several seconds and then jumps forward. Do not repeatedly
                # snap our smooth local clock back to that stale sample. Only
                # resync when Windows itself produced a *new* sample and it is
                # far enough away to look like a real seek or meaningful drift.
                predicted = self._display_position()
                if abs(incoming.position_seconds - predicted) >= 3.0:
                    self._progress_anchor_position = incoming.position_seconds
                    self._progress_anchor_time = now

            self._last_raw_position = incoming.position_seconds
            self._progress_playing = incoming.playing
            self.info = incoming
        except Exception as exc:
            self.status_message = f"media error: {exc}"

    def _display_position(self) -> float:
        position = self._progress_anchor_position
        if self._progress_playing:
            position += max(0.0, time.monotonic() - self._progress_anchor_time)
        if self.info.duration_seconds > 0:
            position = min(position, self.info.duration_seconds)
        return max(0.0, position)

    def _optimistic_transport_update(self, action: str) -> None:
        now = time.monotonic()
        if action == "toggle":
            self._progress_anchor_position = self._display_position()
            self._progress_anchor_time = now
            self._progress_playing = not self._progress_playing
            self.info.playing = self._progress_playing
        elif action in {"next", "previous"}:
            self._progress_anchor_position = 0.0
            self._progress_anchor_time = now

    async def _read_keys(self) -> None:
        # msvcrt is deliberately imported only on Windows.
        import msvcrt

        while msvcrt.kbhit():
            char = msvcrt.getwch()

            # Windows extended-key prefix. The following scan code is part of
            # the same keypress, so consume it unconditionally. In v0.6 we
            # only consumed it when kbhit() was already true; a delayed scan
            # code for Down Arrow is literally "P" and Left Arrow is "K",
            # which accidentally triggered Previous / Play-Pause. Windows
            # Terminal may also translate mouse-wheel input into arrow keys in
            # the alternate screen, producing the same symptom.
            if char in ("\x00", "\xe0"):
                scan = msvcrt.getwch()
                await self._handle_special_key(scan)
                continue

            if self.search_mode:
                await self._handle_search_key(char)
            else:
                await self._handle_command_key(char)

    async def _handle_special_key(self, scan: str) -> None:
        # Common msvcrt scan codes: H=up, P=down, K=left, M=right.
        # Arrow keys never control playback. When search results are present,
        # Up/Down are used only to move the result selection.
        if not self.search_results:
            return
        if self.selected_result_index is None:
            self.selected_result_index = 0
        if scan == "H":
            self.selected_result_index = (self.selected_result_index - 1) % len(self.search_results)
        elif scan == "P":
            self.selected_result_index = (self.selected_result_index + 1) % len(self.search_results)

    async def _activate_selected_result(self) -> None:
        if not self.search_results:
            return
        if self.selected_result_index is None:
            self.selected_result_index = 0
        index = max(0, min(self.selected_result_index, len(self.search_results) - 1))
        item = self.search_results[index]
        if not item.uri:
            self.status_message = "selected result has no Spotify URI"
            return
        opened = await asyncio.to_thread(self.search_backend.open_uri, item.uri)
        self.status_message = (
            f"opened {item.kind}: {item.name}"
            if opened
            else f"could not open {item.kind} in Spotify"
        )

    async def _handle_command_key(self, char: str) -> None:
        key = char.lower()
        if key == "q":
            self.running = False
        elif char in ("\r", "\n") and self.search_results:
            await self._activate_selected_result()
        elif char == "\x1b" and self.search_results:
            self.search_results = []
            self.selected_result_index = None
            self.status_message = ""
        elif key == "k":
            ok = await self.media.play_pause()
            self.status_message = "" if ok else "Windows media-key play/pause failed"
            if ok:
                self._optimistic_transport_update("toggle")
        elif key == "n":
            ok = await self.media.next()
            self.status_message = "" if ok else "Windows media-key next failed"
            if ok:
                self._optimistic_transport_update("next")
        elif key == "p":
            ok = await self.media.previous()
            self.status_message = "" if ok else "Windows media-key previous failed"
            if ok:
                self._optimistic_transport_update("previous")
        elif char == "/":
            self.search_mode = True
            self.search_query = ""
            self.status_message = "type a search and press Enter; Esc cancels"
        elif key == "t":
            idx = THEMES.index(self.config.theme)
            self.config.theme = THEMES[(idx + 1) % len(THEMES)]
            save_config(self.config)
        elif key == "c":
            idx = PALETTES.index(self.config.palette)
            self.config.palette = PALETTES[(idx + 1) % len(PALETTES)]
            save_config(self.config)

    async def _handle_search_key(self, char: str) -> None:
        if char == "\x1b":  # Escape
            self.search_mode = False
            self.search_query = ""
            self.status_message = ""
            return
        if char in ("\r", "\n"):
            query = self.search_query.strip()
            self.search_mode = False
            if not query:
                self.status_message = ""
                return
            self.status_message = f'searching for "{query}"…'
            try:
                self.search_results = await asyncio.to_thread(self.search_backend.search, query)
                self.selected_result_index = 0 if self.search_results else None
                self.status_message = f"{len(self.search_results)} result(s) · ↑/↓ select · Enter open"
            except SearchRateLimited as exc:
                self.search_results = []
                self.selected_result_index = None
                opened = self.search_backend.open_desktop_search(query)
                if exc.quota_exceeded:
                    reason = "shared Spotify API quota exhausted"
                elif exc.retry_after:
                    reason = f"Spotify API cooldown ~{exc.retry_after}s"
                else:
                    reason = "Spotify API rate-limited"
                suffix = "; opened the query in Spotify" if opened else ""
                self.status_message = reason + suffix
            except Exception as exc:
                self.search_results = []
                self.selected_result_index = None
                self.status_message = str(exc)
            return
        if char in ("\b", "\x7f"):
            self.search_query = self.search_query[:-1]
            return
        if char.isprintable():
            self.search_query += char

    def render(self) -> RenderableType:
        width, height = shutil.get_terminal_size((120, 40))
        palette = PALETTE_STYLES[self.config.palette]
        transparent = self.config.theme == "transparent"

        # In transparent mode every layout surface uses terminal-default
        # background. In solid mode we explicitly paint the surfaces.
        base_style = Style(color=palette.fg, bgcolor=None if transparent else palette.solid_bg)
        panel_style = Style(color=palette.fg, bgcolor=None if transparent else palette.solid_panel)
        muted = Style(color=palette.muted, bgcolor=None if transparent else palette.solid_panel)

        top = self._render_topbar(width, palette, transparent)
        player = self._render_player(width, palette, panel_style, muted)
        search = self._render_search(palette, panel_style)

        parts: list[RenderableType] = [top, player, search]
        if height >= 22:
            parts.append(self._render_results(palette, panel_style, height))
        parts.append(self._render_help(palette, transparent))

        # Group itself has no background. The outer style is only used for
        # solid mode; transparent mode is explicitly `default on default`.
        group = Group(*parts)
        if transparent:
            return Align.left(group, style="default on default")
        return Align.left(group, style=base_style)

    def _render_topbar(self, width: int, palette: Palette, transparent: bool) -> Text:
        left = "spotcli · local-first terminal Spotify controller"
        right = f"theme: {self.config.theme}  •  palette: {self.config.palette}"
        if width < 82:
            right = f"{self.config.theme}/{self.config.palette}"
        gap = max(1, width - len(left) - len(right) - 2)
        text = Text(style=Style(color=palette.muted, bgcolor=None if transparent else palette.solid_bg))
        if width >= 56:
            text.append(left)
            text.append(" " * gap)
            text.append(right)
        else:
            text.append("spotcli  ")
            text.append(right)
        return text

    def _render_player(
        self,
        width: int,
        palette: Palette,
        panel_style: Style,
        muted: Style,
    ) -> Panel:
        show_cover = width >= 52 and self.info.thumbnail is not None
        if width >= 100:
            art_width, art_rows = 28, 10
            progress_width = 38
        elif width >= 72:
            art_width, art_rows = 20, 7
            progress_width = 24
        elif width >= 52:
            art_width, art_rows = 14, 5
            progress_width = 14
        else:
            art_width, art_rows = 0, 0
            progress_width = 12

        title = Text(self.info.title or "Nothing playing", style=Style(color=palette.fg, bold=True))
        artist = Text(self.info.artist or "", style=muted)
        album = Text(self.info.album or "", style=muted)
        progress = Text(progress_line(self._display_position(), self.info.duration_seconds, progress_width), style=palette.fg)
        state = "▶ playing" if self.info.playing else "⏸ paused"
        source_text = f"{state}  •  {self.info.source or 'no active media session'}"
        source = Text(source_text, style=muted)

        meta_rows: list[RenderableType] = [title, artist]
        if width >= 72:
            meta_rows.append(album)
        meta_rows.append(progress)
        if width >= 88:
            meta_rows.append(source)
        if self.status_message:
            meta_rows.append(Text(self.status_message, style=Style(color=palette.accent)))
        meta = Group(*meta_rows)

        if show_cover:
            table = Table.grid(expand=True, padding=(0, 2))
            table.add_column(width=art_width)
            table.add_column(ratio=1)
            table.add_row(render_album_art(self.info.thumbnail, width=art_width, rows=art_rows), meta)
            body: RenderableType = table
        else:
            body = meta

        return Panel(
            body,
            border_style=palette.accent,
            style=panel_style,
            padding=(1, 2),
            expand=True,
        )

    def _render_search(self, palette: Palette, panel_style: Style) -> Panel:
        if self.search_mode:
            text = Text("Search Spotify: ", style=Style(color=palette.muted))
            text.append(self.search_query)
            text.append("▌", style=Style(color=palette.accent))
        else:
            text = Text("Search Spotify…  (press /)", style=Style(color=palette.muted))
        return Panel(text, border_style=palette.border, style=panel_style, padding=(0, 1), expand=True)

    def _render_results(self, palette: Palette, panel_style: Style, height: int) -> Panel:
        if not self.search_results:
            body: RenderableType = Text("", style=Style(color=palette.muted))
        else:
            max_rows = max(3, min(18, height - 20))
            lines = Text()
            for i, item in enumerate(self.search_results[:max_rows], start=1):
                selected = (self.selected_result_index == i - 1)
                lines.append("▶ " if selected else "  ", style=Style(color=palette.accent, bold=selected))
                lines.append(f"{i:>2}. ", style=Style(color=palette.accent, bold=selected))
                lines.append(item.name, style=Style(color=palette.fg, bold=selected))
                if item.artist:
                    lines.append(f"  •  {item.artist}", style=Style(color=palette.muted))
                if i < min(max_rows, len(self.search_results)):
                    lines.append("\n")
            body = lines
        return Panel(body, title="Results", border_style=palette.accent, style=panel_style, expand=True)

    def _render_help(self, palette: Palette, transparent: bool) -> Text:
        style = Style(color=palette.muted, bgcolor=None if transparent else palette.solid_bg)
        text = Text(style=style)
        controls: Sequence[tuple[str, str]] = (
            ("k", "Play/Pause"),
            ("n", "Next"),
            ("p", "Previous"),
            ("/", "Search"),
            ("↑↓", "Select"),
            ("Enter", "Open"),
            ("t", "Theme"),
            ("c", "Palette"),
            ("q", "Quit"),
        )
        for index, (key, label) in enumerate(controls):
            if index:
                text.append("   ")
            text.append(key, style=Style(color=palette.accent, bold=True, bgcolor=style.bgcolor))
            text.append(f" {label}")
        return text


def main() -> None:
    try:
        asyncio.run(SpotCLI().run())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
