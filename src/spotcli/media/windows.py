from __future__ import annotations

import ctypes
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
    """Read Spotify metadata through Windows GSMTC and send transport keys.

    GSMTC is excellent for metadata, but some Spotify/Windows combinations expose
    a session that reports transport commands as accepted without actually
    changing playback.  v0.5 therefore uses Windows' media-key path for
    play/pause/next/previous.  This is the same system-wide path a keyboard's
    physical media keys use and is substantially more reliable with Spotify.
    """

    # Win32 virtual key codes.
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

    def _session(self) -> Optional[GlobalSystemMediaTransportControlsSession]:
        if self._manager is None:
            return None

        # Prefer Spotify explicitly rather than whichever application Windows
        # happens to consider the current media session.
        try:
            sessions = list(self._manager.get_sessions())
            for session in sessions:
                source = (session.source_app_user_model_id or "").lower()
                if "spotify" in source:
                    return session
        except Exception:
            pass
        return self._manager.get_current_session()

    async def now_playing(self) -> TrackInfo:
        session = self._session()
        if session is None:
            return TrackInfo()

        props = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()
        # Compare the WinRT enum directly. Stringifying WinRT enums is not
        # stable across Python/winrt versions and can yield only the numeric
        # value (for example "4"), which made v0.6 think Spotify was paused
        # even while it was playing. That forced the displayed clock to wait
        # for Spotify's coarse ~5-second timeline updates.
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
            duration_seconds=max(0.0, _seconds(timeline.end_time) - _seconds(timeline.start_time)),
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
        """Return active Windows audio sessions owned by Spotify.exe.

        This uses the local Windows Core Audio mixer through pycaw, so changing
        Spotify's app volume costs zero Spotify Web API requests.
        """
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
        # Spotify can own multiple processes/sessions. They normally share the
        # same level; averaging avoids a stale duplicate dominating the display.
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
