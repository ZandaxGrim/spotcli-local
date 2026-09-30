from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import subprocess
import time


@dataclass(slots=True)
class SearchResult:
    kind: str
    name: str
    artist: str = ""
    uri: str = ""


class SearchRateLimited(RuntimeError):
    def __init__(self, retry_after: int | None = None, quota_exceeded: bool = False) -> None:
        self.retry_after = retry_after
        self.quota_exceeded = quota_exceeded
        if quota_exceeded:
            message = "Spotify's shared API quota is exhausted"
        elif retry_after:
            message = f"Spotify search is rate-limited; retry in about {retry_after}s"
        else:
            message = "Spotify search is rate-limited right now"
        super().__init__(message)


class SpotifyPlayerBackend:
    """Use spotify_player's cached OAuth token without its Windows CLI socket.

    spotify_player's scripting CLI currently has a known Windows bug where
    subcommands can fail with WSAECONNRESET / OS error 10054.  Its cached Web
    API token is still perfectly usable, so spotcli reads that token and talks
    to Spotify's Web API directly for catalog search.

    This keeps the no-developer-dashboard workflow: authenticate once with
    `spotify_player authenticate`, then spotcli can reuse the cached token.
    """

    API = "https://api.spotify.com/v1/search"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._cooldown_until = 0.0

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        cache_key = query.strip().casefold()
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 900:
            return list(cached[1])

        if time.monotonic() < self._cooldown_until:
            remaining = max(1, int(self._cooldown_until - time.monotonic()))
            raise SearchRateLimited(retry_after=remaining)
        tokens = self._cached_access_tokens()
        if not tokens:
            raise RuntimeError(
                "Search is not authenticated yet. Run `spotify_player authenticate` once, "
                "then restart spotcli."
            )

        last_error: str | None = None
        for token in tokens:
            try:
                data = self._request_search(token, query, limit)
                results = self._parse_results(data)
                self._cache[cache_key] = (time.monotonic(), list(results))
                return results
            except HTTPError as exc:
                if exc.code == 401:
                    last_error = "cached Spotify token expired"
                    continue
                if exc.code == 429:
                    retry_after = None
                    try:
                        retry_after = int(exc.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        retry_after = None
                    try:
                        body = json.loads(exc.read().decode("utf-8", errors="replace"))
                    except Exception:
                        body = {}
                    reason = str(((body.get("error") or {}).get("reason") or "")).upper()
                    quota_exceeded = reason == "QUOTA_EXCEEDED"
                    if retry_after:
                        self._cooldown_until = time.monotonic() + retry_after
                    raise SearchRateLimited(retry_after, quota_exceeded) from exc
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                raise RuntimeError(f"Spotify search failed (HTTP {exc.code}) {detail[:140]}") from exc
            except URLError as exc:
                raise RuntimeError(f"Spotify search network error: {exc.reason}") from exc
            except OSError as exc:
                raise RuntimeError(f"Spotify search network error: {exc}") from exc

        raise RuntimeError(
            (last_error or "Spotify authentication failed")
            + ". Run `spotify_player authenticate` to refresh it, then restart spotcli."
        )


    @staticmethod
    def open_uri(uri: str) -> bool:
        """Open a Spotify URI with the registered Windows Spotify handler."""
        if os.name != "nt" or not uri:
            return False
        try:
            # ShellExecuteW handles spotify:track:, spotify:album:, and
            # spotify:playlist: through the user's registered Spotify client.
            import ctypes
            result = ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, 1)
            return int(result) > 32
        except Exception:
            return False

    @staticmethod
    def open_desktop_search(query: str) -> bool:
        """Open the same query in the installed Spotify client.

        This is only a fallback for cases where the shared spotify_player/ncspot
        Web API client is globally rate-limited. It does not replace CLI results,
        but it keeps search usable without hammering a 429 endpoint.
        """
        if os.name != "nt":
            return False
        try:
            uri = "spotify:search:" + quote(query, safe="")
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
            return True
        except OSError:
            return False

    def _request_search(self, token: str, query: str, limit: int) -> dict[str, Any]:
        params = urlencode(
            {
                "q": query,
                "type": "track,album,playlist",
                # Spotify's current Search endpoint caps this at 10.
                "limit": max(1, min(int(limit), 10)),
            }
        )
        request = Request(
            f"{self.API}?{params}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "spotcli-local/0.7",
            },
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse_results(data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []

        for track in ((data.get("tracks") or {}).get("items") or []):
            if not isinstance(track, dict):
                continue
            artists = track.get("artists") or []
            artist = ", ".join(
                str(a.get("name", "")) for a in artists if isinstance(a, dict) and a.get("name")
            )
            results.append(
                SearchResult(
                    kind="track",
                    name=str(track.get("name") or "Unknown track"),
                    artist=artist,
                    uri=str(track.get("uri") or ""),
                )
            )

        for album in ((data.get("albums") or {}).get("items") or []):
            if not isinstance(album, dict):
                continue
            artists = album.get("artists") or []
            artist = ", ".join(
                str(a.get("name", "")) for a in artists if isinstance(a, dict) and a.get("name")
            )
            results.append(
                SearchResult(
                    kind="album",
                    name=str(album.get("name") or "Unknown album"),
                    artist=artist,
                    uri=str(album.get("uri") or ""),
                )
            )

        for playlist in ((data.get("playlists") or {}).get("items") or []):
            if not isinstance(playlist, dict):
                continue
            owner = playlist.get("owner") or {}
            owner_name = owner.get("display_name") or owner.get("id") or ""
            results.append(
                SearchResult(
                    kind="playlist",
                    name=str(playlist.get("name") or "Unknown playlist"),
                    artist=str(owner_name),
                    uri=str(playlist.get("uri") or ""),
                )
            )

        return results

    def _cached_access_tokens(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for path in self._token_paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            token = self._find_access_token(data)
            if token and token not in seen:
                seen.add(token)
                found.append(token)
        return found

    @staticmethod
    def _find_access_token(value: Any) -> str | None:
        if isinstance(value, dict):
            token = value.get("access_token")
            if isinstance(token, str) and token:
                return token
            for nested in value.values():
                hit = SpotifyPlayerBackend._find_access_token(nested)
                if hit:
                    return hit
        elif isinstance(value, list):
            for nested in value:
                hit = SpotifyPlayerBackend._find_access_token(nested)
                if hit:
                    return hit
        return None

    @staticmethod
    def _token_paths() -> list[Path]:
        home = Path.home()
        roots = [
            home / ".cache" / "spotify-player",
            Path(os.environ.get("LOCALAPPDATA", "")) / "spotify-player",
            Path(os.environ.get("APPDATA", "")) / "spotify-player",
        ]
        paths: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not str(root) or not root.exists() or root in seen:
                continue
            seen.add(root)
            try:
                # Prefer Web API token files. credentials.json is a librespot
                # session cache and normally won't contain an API bearer token.
                paths.extend(sorted(root.glob("*_token.json")))
                paths.extend(sorted(root.glob("*token*.json")))
            except OSError:
                pass
        # Stable de-duplication.
        return list(dict.fromkeys(paths))
