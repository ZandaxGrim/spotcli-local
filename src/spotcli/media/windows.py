from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSession,
    GlobalSystemMediaTransportControlsSessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus,
)
from winrt.windows.storage.streams import DataReader


@dataclass(slots=True)
class TrackInfo:
    title: str = "Nothing playing"
    artist: str = ""
    album: str = ""
    source: str = ""
    playing: bool = False
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    thumbnail: bytes | None = None


class WindowsMediaSession:

    ##Grab Spotify metadata through Windows GSMTC and use media keys for controls.
    ##GSMTC is great for reading stuff but Windows/Spotify can lie and say a control
    ##worked when it actually did fuck all, so play/pause/next/previous use the same
    ##media-key path a physical keyboard does instead. way more reliable.

    ##Win32 media key codes. ugly but they work.
    _VK_MEDIA_NEXT_TRACK = 0xB0
    _VK_MEDIA_PREV_TRACK = 0xB1
    _VK_MEDIA_PLAY_PAUSE = 0xB3
    _KEYEVENTF_KEYUP = 0x0002

    def __init__(self) -> None:
        self._manager: Optional[GlobalSystemMediaTransportControlsSessionManager] = None
        self._art_key: tuple[str, str, str] | None = None
        self._art_bytes: bytes | None = None

    async def start(self) -> None:
        self._manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()

    @staticmethod
    def _spotify_window_title() -> str | None:

        ##Spotify likes to put the current song in its window title as `Artist - Track`.
        ##Only look at windows owned by Spotify.exe so Chrome/YouTube can't sneak in
        ##and pretend to be the current song again.

        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
        except Exception:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        titles: list[str] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def process_name_for_hwnd(hwnd: int) -> str:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if not pid.value:
                return ""

            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid.value,
            )

            if not handle:
                return ""

            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)

                if not kernel32.QueryFullProcessImageNameW(
                        handle,
                        0,
                        buf,
                        ctypes.byref(size),
                ):
                    return ""

                return os.path.basename(buf.value).lower()

            finally:
                kernel32.CloseHandle(handle)

        @EnumWindowsProc
        def enum_proc(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True

                length = user32.GetWindowTextLengthW(hwnd)

                if length <= 0:
                    return True

                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)

                title = buf.value.strip()

                if not title:
                    return True

                process_name = process_name_for_hwnd(hwnd)

                if process_name != "spotify.exe":
                    return True

                ##Only trust the Spotify title when it actually looks like `Artist - Track`.
                ##This keeps random Spotify shell/window names from getting treated as music.

                if re.match(r"^.+\s-\s.+$", title):
                    titles.append(title)

            except Exception:
                pass

            return True

        try:
            user32.EnumWindows(enum_proc, 0)
        except Exception:
            return None

        return titles[0] if titles else None

    def _sessions(self) -> list[GlobalSystemMediaTransportControlsSession]:
        if self._manager is None:
            return []

        ##Do NOT use Windows' generic "current session" here.
        ##That is how Chrome/YouTube ended up making SpotCLI grab some BS before.

        try:
            return list(self._manager.get_sessions())
        except Exception:
            return []

    async def now_playing(self) -> TrackInfo:


        ##Use Spotify's own `Artist - Track` window title as the sanity check.
        ##We still want the real GSMTC session for album art/progress/play state,
        ##we just use the window title to make sure we're not grabbing random YouTube shit.

        spotify_window = self._spotify_window_title()

        if not spotify_window:
            return TrackInfo()

        try:
            window_artist, window_title = spotify_window.split(" - ", 1)
        except ValueError:
            return TrackInfo()

        window_artist_compare = window_artist.strip().casefold()
        window_title_compare = window_title.strip().casefold()

        spotify_session = None
        spotify_props = None

        for session in self._sessions():
            try:
                props = await session.try_get_media_properties_async()

                source = (session.source_app_user_model_id or "").casefold()
                title = (props.title or "").strip().casefold()
                artist = (props.artist or "").strip().casefold()

                ##Best case Windows actually calls it Spotify and we can stop caring.

                if "spotify" in source:
                    spotify_session = session
                    spotify_props = props
                    break

                ##Fallback for when Windows gives Spotify some weird-ass session ID.
                ##Match the song exactly and be a little loose on artist text because
                ##Spotify/Windows love randomly adding extra artist names and commas.

                title_matches = title == window_title_compare

                artist_matches = (
                        artist == window_artist_compare
                        or (
                                artist
                                and window_artist_compare
                                and artist in window_artist_compare
                        )
                        or (
                                artist
                                and window_artist_compare
                                and window_artist_compare in artist
                        )
                )

                if title_matches and artist_matches:
                    spotify_session = session
                    spotify_props = props
                    break

            except Exception:
                continue

        if spotify_session is None or spotify_props is None:

            ##Spotify clearly has a song open but GSMTC hasn't caught up yet.
            ##Show the Spotify title for now instead of stealing another app's metadata.
            ##Art/progress should come back once Windows gets its shit together.

            return TrackInfo(
                title=window_title.strip(),
                artist=window_artist.strip(),
                source="Spotify.exe",
            )

        session = spotify_session
        props = spotify_props

        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()

        ##Compare the WinRT enum directly. Turning it into a string can randomly
        ##give just a number like "4", which made old builds think Spotify was paused
        ##and made the timer update like ass every ~5 seconds.

        is_playing = (
                playback.playback_status
                == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
        )

        key = (props.title or "", props.artist or "", props.album_title or "")

        if key != self._art_key:
            self._art_key = key
            self._art_bytes = await self._thumbnail_bytes(props.thumbnail)

        return TrackInfo(
            title=(props.title or "Nothing playing"),
            artist=(props.artist or ""),
            album=(props.album_title or ""),
            source=(session.source_app_user_model_id or ""),
            playing=is_playing,
            position_seconds=_seconds(timeline.position),
            duration_seconds=max(
                0.0,
                _seconds(timeline.end_time) - _seconds(timeline.start_time),
                ),
            thumbnail=self._art_bytes,
        )

    async def _thumbnail_bytes(self, thumbnail) -> bytes | None:
        if thumbnail is None:
            return None

        try:
            stream = await thumbnail.open_read_async()
            size = int(stream.size)

            if size <= 0 or size > 20 * 1024 * 1024:
                stream.close()
                return None

            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)

            data = bytearray(size)
            reader.read_bytes(data)

            reader.close()
            stream.close()

            return bytes(data)

        except Exception:
            return None

    @classmethod
    def _send_media_key(cls, vk: int) -> bool:
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, cls._KEYEVENTF_KEYUP, 0)
            return True

        except Exception:
            return False

    async def play_pause(self) -> bool:
        return self._send_media_key(self._VK_MEDIA_PLAY_PAUSE)

    async def next(self) -> bool:
        return self._send_media_key(self._VK_MEDIA_NEXT_TRACK)

    async def previous(self) -> bool:
        return self._send_media_key(self._VK_MEDIA_PREV_TRACK)

    @staticmethod
    def _spotify_audio_sessions():

        ##Grab Spotify's Windows audio sessions through pycaw.
        ##Volume does not need the Spotify API at all, which is perfect because
        ##their rate limits are annoying as hell. just use Windows, fuck it.

        try:
            from pycaw.pycaw import AudioUtilities
        except Exception:
            return []

        matches = []

        try:
            for session in AudioUtilities.GetAllSessions():
                process = getattr(session, "Process", None)

                if process is None:
                    continue

                try:
                    name = process.name().lower()
                except Exception:
                    continue

                if name == "spotify.exe" or "spotify" in name:
                    matches.append(session)

        except Exception:
            return []

        return matches

    def spotify_volume(self) -> int | None:
        sessions = self._spotify_audio_sessions()
        values: list[float] = []

        for session in sessions:
            volume = getattr(session, "SimpleAudioVolume", None)

            if volume is None:
                continue

            try:
                values.append(float(volume.GetMasterVolume()))
            except Exception:
                continue

        if not values:
            return None

        ##Spotify can have multiple audio sessions open because reasons.
        ##They should all be basically the same level, so average them and move on.

        return max(0, min(100, round(sum(values) / len(values) * 100)))

    def set_spotify_volume(self, percent: int) -> int | None:
        sessions = self._spotify_audio_sessions()

        if not sessions:
            return None

        percent = max(0, min(100, int(percent)))
        level = percent / 100.0
        changed = False

        for session in sessions:
            volume = getattr(session, "SimpleAudioVolume", None)

            if volume is None:
                continue

            try:
                volume.SetMasterVolume(level, None)
                changed = True
            except Exception:
                continue

        return percent if changed else None


def _seconds(value: timedelta | object) -> float:
    if hasattr(value, "total_seconds"):
        try:
            return float(value.total_seconds())
        except Exception:
            pass

    duration = getattr(value, "duration", None)

    if duration is not None:
        try:
            return float(duration) / 10_000_000.0
        except (TypeError, ValueError):
            pass

    return 0.0
