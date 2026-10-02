from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .backends.spotify_player import (
    PlaybackState,
    SearchRateLimited,
    SearchResult,
    SpotifyAPIError,
    SpotifyPlayerBackend,
)
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


@dataclass(slots=True)
class ViewState:
    title: str
    items: list[SearchResult]
    selected: int | None
    kind: str
    context_item: SearchResult | None = None


PALETTE_STYLES: dict[str, Palette] = {
    "classic": Palette("#f5f5f5", "#b3b3b3", "#1db954", "#535353", "#121212", "#181818"),
    "spotify": Palette("#f5f5f5", "#b3b3b3", "#1ed760", "#535353", "#121212", "#181818"),
    "midnight": Palette("#d8deea", "#a9b1d6", "#7aa2f7", "#565f89", "#090b10", "#111722"),
    "mono": Palette("#eeeeee", "#bdbdbd", "#eeeeee", "#eeeeee", "#000000", "#000000"),
    "purple": Palette("#f5efff", "#b9a8d4", "#b388ff", "#6d4c8f", "#120d1a", "#1b1326"),
    "ocean": Palette("#e8fbff", "#93c7d3", "#38bdf8", "#256b7c", "#07151b", "#0b2029"),
    "sunset": Palette("#fff2e8", "#d7a18c", "#ff7a59", "#8a4f45", "#1c0f0b", "#29150f"),
    "cyberpunk": Palette("#f5f7ff", "#a6a6d0", "#ff4fd8", "#6f5ae8", "#0b0714", "#151023"),
    "rose": Palette("#fff2f6", "#d3a4b3", "#ff6b9e", "#8c4f67", "#190b11", "#241018"),
    "amber": Palette("#fff8e8", "#d0b98a", "#fbbf24", "#81651e", "#181205", "#221a08"),
    "forest": Palette("#effff4", "#a6ccb2", "#4ade80", "#3d7650", "#08140c", "#0e1f13"),
    "ice": Palette("#f3fbff", "#aac7d8", "#7dd3fc", "#4d7187", "#07131b", "#0d1e29"),
}


class SpotCLI:
    def __init__(self) -> None:
        self.console = Console(style="default on default", highlight=False)
        self.media = WindowsMediaSession()
        self.spotify = SpotifyPlayerBackend()
        self.config: SpotCLIConfig = load_config()
        self.info = TrackInfo()

        self.search_query = ""
        self.search_mode = False
        self.items: list[SearchResult] = []
        self.selected_index: int | None = None
        self.view_title = "Results"
        self.view_kind = "home"
        self.view_context: SearchResult | None = None
        self.view_stack: list[ViewState] = []

        self.status_message = ""
        self.running = True
        self.shuffle_state: bool | None = None
        self.volume_percent: int | None = None
        self.supports_volume = True

        self._progress_anchor_position = 0.0
        self._progress_anchor_time = time.monotonic()
        self._progress_track_key: tuple[str, str, float] | None = None
        self._progress_playing = False
        self._last_raw_position: float | None = None
        self._media_poll_task: asyncio.Task[None] | None = None
        self._startup_api_task: asyncio.Task[None] | None = None
        self._deferred_play_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        if os.name != "nt":
            raise RuntimeError("spotcli-local currently requires Windows")

        if not self.spotify.has_cached_token():
            print("spotcli: Spotify authentication is required; starting spotify_player authenticate…")
            try:
                await asyncio.to_thread(self.spotify.authenticate)
                print("spotcli: authentication complete")
            except SpotifyAPIError as exc:
                print(f"spotcli: {exc}", file=sys.stderr)
                print("spotcli: local playback controls will still work; API-backed features may not.", file=sys.stderr)

        await self.media.start()
        await self.refresh_now_playing()
        self._media_poll_task = asyncio.create_task(self._poll_media(), name="spotcli-media-poll")
        # One intentional Web API read is made at startup so the shuffle
        # indicator reflects Spotify immediately. It runs in the background so
        # a slow/rate-limited API never delays the local player UI.
        self._startup_api_task = asyncio.create_task(
            self._refresh_shuffle_state(startup=True), name="spotcli-startup-shuffle"
        )
        # Volume is still read from the local Windows app mixer and costs no
        # Spotify API quota.
        self.volume_percent = await asyncio.to_thread(self.media.spotify_volume)

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
                    await asyncio.sleep(0.10)
        finally:
            if self._media_poll_task is not None:
                self._media_poll_task.cancel()
                try:
                    await self._media_poll_task
                except asyncio.CancelledError:
                    pass
            if self._startup_api_task is not None and not self._startup_api_task.done():
                self._startup_api_task.cancel()
                try:
                    await self._startup_api_task
                except asyncio.CancelledError:
                    pass
            if self._deferred_play_task is not None and not self._deferred_play_task.done():
                self._deferred_play_task.cancel()
                try:
                    await self._deferred_play_task
                except asyncio.CancelledError:
                    pass

    async def _poll_media(self) -> None:
        ##Keep this local whenever possible. GSMTC can give us track/art/progress
        ##without burning Spotify API calls every few seconds like an idiot.
        while self.running:
            await self.refresh_now_playing()
            await asyncio.sleep(0.75)

    async def _refresh_shuffle_state(self, startup: bool = False) -> None:
        ##Grab shuffle once at startup then keep it local after that.
        ##If Spotify decides to be useless or rate limits us, just chill and retry
        ##instead of leaving shuffle stuck as `?` forever.

        retry_count = 0

        while self.running:
            try:
                state: PlaybackState = await asyncio.to_thread(
                    self.spotify.get_playback_state
                )
                self.shuffle_state = state.shuffle

                ##If startup had to retry a few times, clear the old API whining
                ##once shuffle finally works so the UI doesn't look broken for no reason.
                if startup and self.status_message.startswith("startup shuffle check:"):
                    self.status_message = ""

                return

            except SpotifyAPIError as exc:
                if not startup:
                    self.status_message = str(exc)
                    return

                retry_count += 1
                cooldown = self.spotify.cooldown_seconds()

                ##Startup shuffle is useful but it doesn't need to shit all over
                ##the player UI when Spotify rate limits us. wait quietly and retry
                ##until we finally get a real on/off back.
                retry_in = cooldown if cooldown is not None else min(30, 5 * retry_count)
                retry_in = max(1, retry_in)

                await asyncio.sleep(retry_in)

    async def refresh_now_playing(self) -> None:
        try:
            incoming = await self.media.now_playing()
            now = time.monotonic()
            track_key = (incoming.title, incoming.artist, round(incoming.duration_seconds, 2))
            raw_changed = self._last_raw_position is None or abs(incoming.position_seconds - self._last_raw_position) >= 0.20

            if self._progress_track_key != track_key:
                self._progress_track_key = track_key
                self._progress_anchor_position = incoming.position_seconds
                self._progress_anchor_time = now
            elif incoming.playing != self._progress_playing:
                self._progress_anchor_position = incoming.position_seconds
                self._progress_anchor_time = now
            elif raw_changed:
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

    # ---------- input ----------

    async def _read_keys(self) -> None:
        import msvcrt

        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                scan = msvcrt.getwch()
                await self._handle_special_key(scan)
                continue
            if self.search_mode:
                await self._handle_search_key(char)
            else:
                await self._handle_command_key(char)

    async def _handle_special_key(self, scan: str) -> None:
        if not self.items:
            return
        if self.selected_index is None:
            self.selected_index = 0
        if scan == "H":  # up
            self.selected_index = (self.selected_index - 1) % len(self.items)
        elif scan == "P":  # down
            self.selected_index = (self.selected_index + 1) % len(self.items)

    async def _handle_command_key(self, char: str) -> None:
        keys = self.config.keys
        key = char.lower()

        if key == keys.quit:
            self.running = False
        elif char in ("\r", "\n") and self.items:
            await self._activate_selected()
        elif char in ("\x1b", "\b", "\x7f"):
            self._go_back()
        elif key == keys.play_pause:
            ok = await self.media.play_pause()
            self.status_message = "" if ok else "Windows media-key play/pause failed"
            if ok:
                self._optimistic_transport_update("toggle")
        elif key == keys.next:
            ok = await self.media.next()
            self.status_message = "" if ok else "Windows media-key next failed"
            if ok:
                self._optimistic_transport_update("next")
        elif key == keys.previous:
            ok = await self.media.previous()
            self.status_message = "" if ok else "Windows media-key previous failed"
            if ok:
                self._optimistic_transport_update("previous")
        elif key == keys.search:
            self.search_mode = True
            self.search_query = ""
            self.status_message = "type a search and press Enter; Esc cancels"
        elif key == keys.queue_selected:
            await self._queue_selected()
        elif key == keys.play_playlist:
            await self._play_selected_playlist()
        elif key == keys.playlists:
            await self._show_playlists()
        elif key == keys.queue_view:
            await self._show_queue()
        elif key == keys.shuffle:
            await self._toggle_shuffle()
        elif key == keys.refresh_cache:
            await self._refresh_current_cache()
        elif key == keys.volume_down:
            await self._change_volume(-self.config.volume_step)
        elif key == keys.volume_up:
            await self._change_volume(self.config.volume_step)
        elif key == keys.theme:
            idx = THEMES.index(self.config.theme)
            self.config.theme = THEMES[(idx + 1) % len(THEMES)]
            save_config(self.config)
        elif key == keys.palette:
            idx = PALETTES.index(self.config.palette)
            self.config.palette = PALETTES[(idx + 1) % len(PALETTES)]
            save_config(self.config)

    async def _handle_search_key(self, char: str) -> None:
        if char == "\x1b":
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
            await self._do_search(query)
            return
        if char in ("\b", "\x7f"):
            self.search_query = self.search_query[:-1]
            return
        if char.isprintable():
            self.search_query += char

    # ---------- navigation / actions ----------

    def _push_view(self) -> None:
        self.view_stack.append(
            ViewState(
                title=self.view_title,
                items=list(self.items),
                selected=self.selected_index,
                kind=self.view_kind,
                context_item=self.view_context,
            )
        )

    def _go_back(self) -> None:
        if self.view_stack:
            previous = self.view_stack.pop()
            self.view_title = previous.title
            self.items = previous.items
            self.selected_index = previous.selected
            self.view_kind = previous.kind
            self.view_context = previous.context_item
            self.status_message = ""
        elif self.items:
            self.items = []
            self.selected_index = None
            self.view_title = "Results"
            self.view_kind = "home"
            self.view_context = None
            self.status_message = ""

    def _selected_item(self) -> SearchResult | None:
        if not self.items:
            return None
        if self.selected_index is None:
            self.selected_index = 0
        self.selected_index = max(0, min(self.selected_index, len(self.items) - 1))
        return self.items[self.selected_index]

    async def _activate_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if item.kind in {"album", "playlist"}:
            await self._browse_collection(item)
            return
        if item.kind not in {"track", "episode"}:
            self.status_message = f"can't play {item.kind} directly"
            return
        self.status_message = f"playing {item.name}…"
        try:
            await asyncio.to_thread(self.spotify.play, item)
            self.status_message = f"playing: {item.name}"
            self._progress_anchor_position = 0.0
            self._progress_anchor_time = time.monotonic()
            self._progress_playing = True
        except SpotifyAPIError as exc:
            remaining = self.spotify.cooldown_seconds()
            if remaining is not None:
                self._schedule_deferred_play(item, remaining)
            else:
                self.status_message = str(exc)


    def _schedule_deferred_play(self, item: SearchResult, remaining: int) -> None:
        if self._deferred_play_task is not None and not self._deferred_play_task.done():
            self._deferred_play_task.cancel()
        self.status_message = f"Spotify API cooldown · {item.name} will play automatically in ~{remaining}s"
        self._deferred_play_task = asyncio.create_task(
            self._deferred_play(item), name="spotcli-deferred-play"
        )

    async def _deferred_play(self, item: SearchResult) -> None:
        try:
            await self.spotify.wait_for_cooldown()
            self.status_message = f"retrying play: {item.name}…"
            await asyncio.to_thread(self.spotify.play, item)
            self.status_message = f"playing: {item.name}"
            self._progress_anchor_position = 0.0
            self._progress_anchor_time = time.monotonic()
            self._progress_playing = True
        except asyncio.CancelledError:
            raise
        except SpotifyAPIError as exc:
            remaining = self.spotify.cooldown_seconds()
            if remaining is not None:
                self.status_message = f"Spotify API still cooling down (~{remaining}s); press Enter to retry"
            else:
                self.status_message = str(exc)
        finally:
            current = asyncio.current_task()
            if self._deferred_play_task is current:
                self._deferred_play_task = None

    async def _play_selected_playlist(self) -> None:
        item = self._selected_item()
        if item is None and self.view_kind == "playlist":
            item = self.view_context
        if item is None:
            self.status_message = "select a playlist first"
            return
        if item.kind != "playlist":
            # When browsing inside a playlist, x should start that playlist even
            # if the current selection is a track.
            if self.view_kind == "playlist" and self.view_context is not None:
                item = self.view_context
            else:
                self.status_message = "play-playlist only works on playlists"
                return
        self.status_message = f"starting playlist: {item.name}…"
        try:
            await asyncio.to_thread(self.spotify.play_context, item)
            self.status_message = f"playing playlist: {item.name}"
        except SpotifyAPIError as exc:
            remaining = self.spotify.cooldown_seconds()
            if remaining is not None:
                self.status_message = f"Spotify API cooldown (~{remaining}s); try {self.config.keys.play_playlist} again when it expires"
            else:
                self.status_message = str(exc)

    async def _queue_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            self.status_message = "select a search/browse result first"
            return
        if item.kind not in {"track", "episode"}:
            self.status_message = "only tracks or episodes can be queued"
            return
        try:
            await asyncio.to_thread(self.spotify.add_to_queue, item)
            self.status_message = f"queued: {item.name}"
        except SpotifyAPIError as exc:
            self.status_message = str(exc)

    async def _browse_collection(self, item: SearchResult) -> None:
        self.status_message = f"loading {item.kind}: {item.name}…"
        try:
            results = await asyncio.to_thread(self.spotify.get_collection_items, item)
        except SpotifyAPIError as exc:
            self.status_message = str(exc)
            return
        self._push_view()
        self.items = results
        self.selected_index = 0 if results else None
        self.view_title = f"{item.kind.title()} · {item.name}"
        self.view_kind = item.kind
        self.view_context = item
        age = self.spotify.cache_age_seconds(item.kind, item)
        cache_note = f" · cache {_format_age(age)} old" if age is not None else ""
        self.status_message = f"{len(results)} track(s){cache_note} · Enter play · {self.config.keys.queue_selected} queue · {self.config.keys.refresh_cache} refresh · Esc/Backspace back"

    async def _show_playlists(self) -> None:
        self.status_message = "loading your playlists…"
        try:
            results = await asyncio.to_thread(self.spotify.get_playlists)
        except SpotifyAPIError as exc:
            self.status_message = str(exc)
            return
        self._push_view()
        self.items = results
        self.selected_index = 0 if results else None
        self.view_title = "Your playlists"
        self.view_kind = "playlists"
        self.view_context = None
        age = self.spotify.cache_age_seconds("playlists")
        cache_note = f" · cache {_format_age(age)} old" if age is not None else ""
        self.status_message = f"{len(results)} playlist(s){cache_note} · Enter browse · {self.config.keys.play_playlist} play playlist · {self.config.keys.refresh_cache} refresh cache · Esc/Backspace back"

    async def _show_queue(self) -> None:
        self.status_message = "loading queue…"
        try:
            results = await asyncio.to_thread(self.spotify.get_queue)
        except SpotifyAPIError as exc:
            self.status_message = str(exc)
            return
        self._push_view()
        self.items = results
        self.selected_index = 0 if results else None
        self.view_title = "Playback queue"
        self.view_kind = "queue"
        self.view_context = None
        self.status_message = f"{len(results)} queued item(s) · Enter play now · Esc/Backspace back"

    async def _refresh_current_cache(self) -> None:
        """Force-refresh the persistent cache for the current browse view."""
        if self.view_kind == "playlists":
            self.status_message = "refreshing playlist cache…"
            try:
                results = await asyncio.to_thread(self.spotify.get_playlists, 50, force_refresh=True)
            except SpotifyAPIError as exc:
                self.status_message = f"cache refresh failed: {exc}"
                return
            self.items = results
            self.selected_index = 0 if results else None
            self.status_message = f"playlist cache updated · {len(results)} playlist(s)"
            return

        if self.view_kind in {"playlist", "album"} and self.view_context is not None:
            item = self.view_context
            self.status_message = f"refreshing {item.kind} cache…"
            try:
                results = await asyncio.to_thread(
                    self.spotify.get_collection_items, item, 50, force_refresh=True
                )
            except SpotifyAPIError as exc:
                self.status_message = f"cache refresh failed: {exc}"
                return
            self.items = results
            self.selected_index = 0 if results else None
            self.status_message = f"{item.kind} cache updated · {len(results)} track(s)"
            return

        self.status_message = "open Your playlists or a cached playlist/album, then press refresh"

    async def _toggle_shuffle(self) -> None:
        if self.shuffle_state is None:
            await self._refresh_shuffle_state()
            if self.shuffle_state is None:
                return
        target = not self.shuffle_state
        try:
            await asyncio.to_thread(self.spotify.set_shuffle, target)
            self.shuffle_state = target
            self.status_message = f"shuffle {'on' if target else 'off'}"
        except SpotifyAPIError as exc:
            self.status_message = str(exc)

    async def _change_volume(self, delta: int) -> None:
        # v0.9 controls Spotify.exe through the Windows Core Audio mixer. This
        # is completely local and does not consume Spotify API quota.
        current = await asyncio.to_thread(self.media.spotify_volume)
        if current is None:
            self.status_message = "Spotify Windows audio session not found"
            self.volume_percent = None
            return
        target = max(0, min(100, current + delta))
        changed = await asyncio.to_thread(self.media.set_spotify_volume, target)
        if changed is None:
            self.status_message = "could not change Spotify's Windows app volume"
            return
        self.volume_percent = changed
        self.status_message = f"Spotify app volume {changed}% (local)"

    async def _do_search(self, query: str) -> None:
        self.status_message = f'searching for "{query}"…'
        try:
            results = await asyncio.to_thread(self.spotify.search, query)
            self._push_view()
            self.items = results
            self.selected_index = 0 if results else None
            self.view_title = f'Search · "{query}"'
            self.view_kind = "search"
            self.view_context = None
            self.status_message = (
                f"{len(results)} result(s) · ↑/↓ select · Enter play/browse · "
                f"{self.config.keys.queue_selected} queue track"
            )
        except SearchRateLimited as exc:
            opened = self.spotify.open_desktop_search(query)
            reason = "Spotify API rate-limited"
            if exc.quota_exceeded:
                reason = "shared Spotify API quota exhausted"
            elif exc.retry_after:
                reason = f"Spotify API cooldown ~{exc.retry_after}s"
            self.status_message = reason + ("; opened the query in Spotify" if opened else "")
        except Exception as exc:
            self.status_message = str(exc)

    # ---------- rendering ----------

    def render(self) -> RenderableType:
        width, height = shutil.get_terminal_size((120, 40))
        palette = PALETTE_STYLES[self.config.palette]
        transparent = self.config.theme == "transparent"
        base_style = Style(color=palette.fg, bgcolor=None if transparent else palette.solid_bg)
        panel_style = Style(color=palette.fg, bgcolor=None if transparent else palette.solid_panel)
        muted = Style(color=palette.muted, bgcolor=None if transparent else palette.solid_panel)

        parts: list[RenderableType] = [
            self._render_topbar(width, palette, transparent),
            self._render_player(width, palette, panel_style, muted),
            self._render_search(palette, panel_style),
        ]
        if height >= 20:
            parts.append(self._render_results(palette, panel_style, height))
        parts.append(self._render_help(palette, transparent, width))

        group = Group(*parts)
        return Align.left(group, style="default on default" if transparent else base_style)

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

    def _render_player(self, width: int, palette: Palette, panel_style: Style, muted: Style) -> Panel:
        show_cover = width >= 52 and self.info.thumbnail is not None
        if width >= 100:
            art_width, art_rows, progress_width = 28, 10, 38
        elif width >= 72:
            art_width, art_rows, progress_width = 20, 7, 24
        elif width >= 52:
            art_width, art_rows, progress_width = 14, 5, 14
        else:
            art_width, art_rows, progress_width = 0, 0, 12

        title = Text(self.info.title or "Nothing playing", style=Style(color=palette.fg, bold=True))
        artist = Text(self.info.artist or "", style=muted)

        ##Don't print the same shit twice. Self-titled albums make this look dumb,
        ##so if album == artist (or somehow title) just skip the extra line.
        album_name = (self.info.album or "").strip()
        album_compare = album_name.casefold()
        artist_compare = (self.info.artist or "").strip().casefold()
        title_compare = (self.info.title or "").strip().casefold()
        show_album = (
                width >= 72
                and bool(album_name)
                and album_compare not in {artist_compare, title_compare}
        )
        album = Text(album_name, style=muted)

        progress = Text(
            progress_line(
                self._display_position(),
                self.info.duration_seconds,
                progress_width,
            ),
            style=palette.fg,
        )

        state = Text(
            "▶ playing" if self.info.playing else "|| paused",
            style=muted,
        )

        controls = Text(style=muted)
        shuffle = "?" if self.shuffle_state is None else ("on" if self.shuffle_state else "off")
        volume = "?" if self.volume_percent is None else f"{self.volume_percent}%"
        controls.append(f"shuffle: {shuffle}")
        controls.append("  •  ")
        controls.append(f"volume: {volume}")

        meta_rows: list[RenderableType] = [title, artist]

        if show_album:
            meta_rows.append(album)

        meta_rows.extend([progress, controls, state])
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

        return Panel(body, border_style=palette.accent, style=panel_style, padding=(1, 2), expand=True)

    def _render_search(self, palette: Palette, panel_style: Style) -> Panel:
        if self.search_mode:
            text = Text("Search Spotify: ", style=Style(color=palette.muted))
            text.append(self.search_query)
            text.append("▌", style=Style(color=palette.accent))
        else:
            text = Text(f"Search Spotify…  (press {self.config.keys.search})", style=Style(color=palette.muted))
        return Panel(text, border_style=palette.border, style=panel_style, padding=(0, 1), expand=True)

    def _render_results(self, palette: Palette, panel_style: Style, height: int) -> Panel:
        if not self.items:
            body: RenderableType = Text("", style=Style(color=palette.muted))
        else:
            max_rows = max(3, min(18, height - 20))
            selected = self.selected_index or 0
            # Keep selection visible when there are more rows than fit.
            start = max(0, min(selected - max_rows // 2, max(0, len(self.items) - max_rows)))
            visible = self.items[start : start + max_rows]
            lines = Text()
            for offset, item in enumerate(visible):
                index = start + offset
                is_selected = self.selected_index == index
                marker = "▶ " if is_selected else "  "
                kind = {"track": "♪", "album": "▣", "playlist": "≡", "episode": "◉"}.get(item.kind, "·")
                lines.append(marker, style=Style(color=palette.accent, bold=is_selected))
                lines.append(f"{index + 1:>2}. {kind} ", style=Style(color=palette.accent, bold=is_selected))
                lines.append(item.name, style=Style(color=palette.fg, bold=is_selected))
                if item.artist:
                    lines.append(f"  •  {item.artist}", style=Style(color=palette.muted))
                if offset < len(visible) - 1:
                    lines.append("\n")
            body = lines
        return Panel(body, title=self.view_title, border_style=palette.accent, style=panel_style, expand=True)

    def _render_help(self, palette: Palette, transparent: bool, width: int) -> Text:
        style = Style(color=palette.muted, bgcolor=None if transparent else palette.solid_bg)
        text = Text(style=style)
        k = self.config.keys
        controls: Sequence[tuple[str, str]] = (
            (k.play_pause, "Play/Pause"),
            (k.next, "Next"),
            (k.previous, "Previous"),
            (k.search, "Search"),
            ("↑↓", "Select"),
            ("Enter", "Play/Open"),
            (k.queue_selected, "Queue"),
            (k.play_playlist, "Play playlist"),
            (k.playlists, "Playlists"),
            (k.queue_view, "Queue view"),
            (k.shuffle, "Shuffle"),
            (k.refresh_cache, "Refresh cache"),
            (f"{k.volume_down}/{k.volume_up}", "Volume"),
            ("Esc", "Back"),
            (k.theme, "Theme"),
            (k.palette, "Palette"),
            (k.quit, "Quit"),
        )
        # On narrow terminals, keep only the most important controls visible.
        if width < 80:
            controls = controls[:10] + ((k.quit, "Quit"),)
        for index, (key, label) in enumerate(controls):
            if index:
                text.append("   ")
            text.append(key, style=Style(color=palette.accent, bold=True, bgcolor=style.bgcolor))
            text.append(f" {label}")
        return text


def _startup_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "spotcli-local.cmd"


def install_startup() -> Path:
    if os.name != "nt":
        raise RuntimeError("startup installation is only supported on Windows")
    path = _startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    wt = shutil.which("wt.exe")
    if wt:
        command = f'start "" "{wt}" -w 0 new-tab --title spotcli "{python}" -m spotcli.app'
    else:
        command = f'start "spotcli" "{python}" -m spotcli.app'
    path.write_text("@echo off\r\n" + command + "\r\n", encoding="utf-8")
    return path


def remove_startup() -> bool:
    path = _startup_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def main() -> None:
    parser = argparse.ArgumentParser(prog="spotcli", description="Local-first Spotify terminal controller")
    parser.add_argument("--authenticate", action="store_true", help="run spotify_player authenticate and exit")
    parser.add_argument("--install-startup", action="store_true", help="launch spotcli automatically when you sign in")
    parser.add_argument("--remove-startup", action="store_true", help="remove the Windows startup launcher")
    args = parser.parse_args()

    if args.authenticate:
        backend = SpotifyPlayerBackend()
        try:
            backend.authenticate()
        except SpotifyAPIError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1) from exc
        print("Spotify authentication complete.")
        return

    if args.install_startup:
        try:
            path = install_startup()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"Startup launcher installed: {path}")
        return

    if args.remove_startup:
        print("Startup launcher removed." if remove_startup() else "No spotcli startup launcher was installed.")
        return

    try:
        asyncio.run(SpotCLI().run())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
