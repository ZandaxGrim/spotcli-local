from __future__ import annotations

import json
import os
import subprocess
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


@dataclass(slots=True)
class SearchResult:
    kind: str
    name: str
    artist: str = ""
    uri: str = ""
    context_uri: str = ""
    position: int | None = None


@dataclass(slots=True)
class PlaybackState:
    shuffle: bool | None = None
    volume_percent: int | None = None
    device_name: str = ""
    supports_volume: bool = True


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


class SpotifyAPIError(RuntimeError):
    pass


class SpotifyPlayerBackend:

    ##Spotify Web API helper using spotify_player's cached OAuth token.
    ##The Windows spotify_player scripting socket has historically been flaky, so
    ##spotcli reuses spotify_player's cached Web API token directly. This avoids
    ##requiring spotcli to keep its own client secret or developer-dashboard app.
    ##Search/browse calls work with the scopes spotify_player normally requests.
    ##Playback-changing calls additionally require user-modify-playback-state and
    ##Spotify Premium. Private playlist browsing requires playlist-read-private.

    API_ROOT = "https://api.spotify.com/v1"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._playlists_cache: tuple[float, list[SearchResult]] | None = None
        self._collection_cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._cooldown_until = 0.0
        self._quota_exceeded = False
        self._persistent_cache = self._load_persistent_cache()

        ##GET-heavy data is cached aggressively. spotcli is intentionally
        ##event-driven: no Web API request is made on a timer.

        self.search_ttl = 3600.0
        self.playlists_ttl = 1800.0
        self.collection_ttl = 1800.0

    # ---------- public catalog / library ----------

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        cache_key = query.strip().casefold()
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.search_ttl:
            return list(cached[1])

        self._raise_if_cooling_down(search=True)

        try:
            data = self._request_json(
                "GET",
                "/search",
                params={
                    "q": query,
                    "type": "track,album,playlist",
                    "limit": max(1, min(int(limit), 10)),
                },
                operation="search",
            )
        except SpotifyAPIError as exc:

            ##Keep v0.7's friendlier search-specific rate-limit behavior.

            message = str(exc)
            if "rate-limited" in message or "quota" in message:
                retry_after = self._remaining_cooldown()
                raise SearchRateLimited(
                    retry_after=retry_after,
                    quota_exceeded=self._quota_exceeded or "quota" in message.lower(),
                ) from exc
            raise

        results = self._parse_search_results(data)
        self._cache[cache_key] = (time.monotonic(), list(results))
        return results

    def get_playlists(self, limit: int = 50, *, force_refresh: bool = False) -> list[SearchResult]:
        if not force_refresh:
            cached = self._playlists_cache
            if cached:
                return list(cached[1])
            persisted = self._persistent_items("playlists")
            if persisted is not None:
                self._playlists_cache = (time.monotonic(), list(persisted))
                return list(persisted)
        data = self._request_json(
            "GET",
            "/me/playlists",
            params={"limit": max(1, min(int(limit), 50)), "offset": 0},
            operation="load playlists",
        )
        results: list[SearchResult] = []
        for playlist in data.get("items") or []:
            if not isinstance(playlist, dict):
                continue
            owner = playlist.get("owner") or {}
            results.append(
                SearchResult(
                    kind="playlist",
                    name=str(playlist.get("name") or "Untitled playlist"),
                    artist=str(owner.get("display_name") or owner.get("id") or ""),
                    uri=str(playlist.get("uri") or ""),
                )
            )
        self._playlists_cache = (time.monotonic(), list(results))
        self._persistent_cache["playlists"] = {
            "cached_at": time.time(),
            "items": [self._serialize_result(item) for item in results],
        }
        self._save_persistent_cache()
        return results

    def get_collection_items(self, item: SearchResult, limit: int = 50, *, force_refresh: bool = False) -> list[SearchResult]:
        spotify_id = _spotify_id(item.uri)
        if not spotify_id:
            raise SpotifyAPIError("selected item has no usable Spotify ID")

        cache_key = f"{item.kind}:{spotify_id}"
        if not force_refresh:
            cached = self._collection_cache.get(cache_key)
            if cached:
                return list(cached[1])
            persisted = self._persistent_collection(cache_key)
            if persisted is not None:
                self._collection_cache[cache_key] = (time.monotonic(), list(persisted))
                return list(persisted)

        if item.kind == "album":
            data = self._request_json(
                "GET",
                f"/albums/{quote(spotify_id, safe='')}/tracks",
                params={"limit": max(1, min(int(limit), 50)), "offset": 0},
                operation="load album tracks",
            )
            raw_items = data.get("items") or []
        elif item.kind == "playlist":
            data = self._request_json(
                "GET",
                f"/playlists/{quote(spotify_id, safe='')}/items",
                params={"limit": max(1, min(int(limit), 50)), "offset": 0},
                operation="load playlist items",
            )
            raw_items = data.get("items") or []
        else:
            raise SpotifyAPIError(f"cannot browse {item.kind}")

        results: list[SearchResult] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue

            ##Album responses are track objects directly. Playlist responses in
            ##current Spotify APIs wrap the media item under `item`; older
            ##responses used `track`, so accept both.

            track = raw
            if item.kind == "playlist":
                track = raw.get("item") or raw.get("track") or {}
            if not isinstance(track, dict) or track.get("type") not in (None, "track"):
                continue
            uri = str(track.get("uri") or "")
            if not uri.startswith("spotify:track:"):
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
                    uri=uri,
                    context_uri=item.uri,
                    position=index,
                )
            )
        self._collection_cache[cache_key] = (time.monotonic(), list(results))
        collections = self._persistent_cache.setdefault("collections", {})
        if isinstance(collections, dict):
            collections[cache_key] = {
                "cached_at": time.time(),
                "items": [self._serialize_result(result) for result in results],
            }
        self._save_persistent_cache()
        return results

    def get_queue(self) -> list[SearchResult]:
        data = self._request_json("GET", "/me/player/queue", operation="load queue")
        results: list[SearchResult] = []
        for entry in data.get("queue") or []:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "track")
            uri = str(entry.get("uri") or "")
            if kind not in {"track", "episode"}:
                continue
            artists = entry.get("artists") or []
            artist = ", ".join(
                str(a.get("name", "")) for a in artists if isinstance(a, dict) and a.get("name")
            )
            if kind == "episode" and not artist:
                show = entry.get("show") or {}
                artist = str(show.get("name") or "") if isinstance(show, dict) else ""
            results.append(
                SearchResult(
                    kind=kind,
                    name=str(entry.get("name") or "Unknown item"),
                    artist=artist,
                    uri=uri,
                )
            )
        return results

    # ---------- playback ----------

    def play(self, item: SearchResult) -> None:
        if not item.uri:
            raise SpotifyAPIError("selected item has no Spotify URI")
        if item.kind not in {"track", "episode"}:
            raise SpotifyAPIError("select a track to play")

        if item.context_uri and item.kind == "track":
            body: dict[str, Any] = {"context_uri": item.context_uri}

            ##URI offsets are more stable than numeric offsets when a playlist
            ##contains unavailable/local entries.

            body["offset"] = {"uri": item.uri}
        else:
            body = {"uris": [item.uri]}
        self._request_no_content("PUT", "/me/player/play", body=body, operation="play selected track")

    def play_context(self, item: SearchResult) -> None:
        if item.kind not in {"playlist", "album"} or not item.uri:
            raise SpotifyAPIError("select a playlist or album to play")
        self._request_no_content(
            "PUT",
            "/me/player/play",
            body={"context_uri": item.uri},
            operation=f"play selected {item.kind}",
        )

    def add_to_queue(self, item: SearchResult) -> None:
        if item.kind not in {"track", "episode"} or not item.uri:
            raise SpotifyAPIError("only tracks or episodes can be queued")
        self._request_no_content(
            "POST",
            "/me/player/queue",
            params={"uri": item.uri},
            operation="add to queue",
        )

    def get_playback_state(self) -> PlaybackState:
        data = self._request_json("GET", "/me/player", operation="read playback state", allow_empty=True)
        if not data:
            return PlaybackState()
        device = data.get("device") or {}
        volume = device.get("volume_percent") if isinstance(device, dict) else None
        return PlaybackState(
            shuffle=data.get("shuffle_state") if isinstance(data.get("shuffle_state"), bool) else None,
            volume_percent=int(volume) if isinstance(volume, (int, float)) else None,
            device_name=str(device.get("name") or "") if isinstance(device, dict) else "",
            supports_volume=bool(device.get("supports_volume", True)) if isinstance(device, dict) else True,
        )

    def set_shuffle(self, enabled: bool) -> None:
        self._request_no_content(
            "PUT",
            "/me/player/shuffle",
            params={"state": "true" if enabled else "false"},
            operation="change shuffle",
        )


    # ---------- persistent cache / auth ----------

    @staticmethod
    def persistent_cache_path() -> Path:
        base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
        return base / "spotcli" / "cache.json"

    def has_cached_token(self) -> bool:
        return bool(self._cached_access_tokens())

    def authenticate(self) -> None:
        executable = shutil.which("spotify_player") or shutil.which("spotify_player.exe")
        if not executable:
            raise SpotifyAPIError(
                "spotify_player was not found in PATH, so automatic authentication cannot start"
            )
        try:
            result = subprocess.run([executable, "authenticate"], check=False)
        except OSError as exc:
            raise SpotifyAPIError(f"could not start spotify_player authenticate: {exc}") from exc
        if result.returncode != 0:
            raise SpotifyAPIError(f"spotify_player authenticate exited with code {result.returncode}")
        if not self.has_cached_token():
            raise SpotifyAPIError("authentication finished, but no cached Spotify token was found")

    def _load_persistent_cache(self) -> dict[str, Any]:
        path = self.persistent_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "collections": {}}
        if not isinstance(data, dict):
            return {"version": 1, "collections": {}}
        data.setdefault("version", 1)
        data.setdefault("collections", {})
        return data

    def _save_persistent_cache(self) -> None:
        path = self.persistent_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(self._persistent_cache, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _serialize_result(item: SearchResult) -> dict[str, Any]:
        return {
            "kind": item.kind,
            "name": item.name,
            "artist": item.artist,
            "uri": item.uri,
            "context_uri": item.context_uri,
            "position": item.position,
        }

    @staticmethod
    def _deserialize_results(raw_items: Any) -> list[SearchResult]:
        if not isinstance(raw_items, list):
            return []
        results: list[SearchResult] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            results.append(
                SearchResult(
                    kind=str(raw.get("kind") or ""),
                    name=str(raw.get("name") or ""),
                    artist=str(raw.get("artist") or ""),
                    uri=str(raw.get("uri") or ""),
                    context_uri=str(raw.get("context_uri") or ""),
                    position=_int_or_none(raw.get("position")),
                )
            )
        return results

    def _persistent_items(self, key: str) -> list[SearchResult] | None:
        entry = self._persistent_cache.get(key)
        if not isinstance(entry, dict) or "items" not in entry:
            return None
        return self._deserialize_results(entry.get("items"))

    def _persistent_collection(self, key: str) -> list[SearchResult] | None:
        collections = self._persistent_cache.get("collections")
        if not isinstance(collections, dict):
            return None
        entry = collections.get(key)
        if not isinstance(entry, dict) or "items" not in entry:
            return None
        return self._deserialize_results(entry.get("items"))

    def cache_age_seconds(self, kind: str, item: SearchResult | None = None) -> int | None:
        if kind == "playlists":
            entry = self._persistent_cache.get("playlists")
        elif kind in {"playlist", "album"} and item is not None:
            sid = _spotify_id(item.uri)
            collections = self._persistent_cache.get("collections")
            entry = collections.get(f"{kind}:{sid}") if isinstance(collections, dict) else None
        else:
            return None
        if not isinstance(entry, dict):
            return None
        try:
            return max(0, int(time.time() - float(entry.get("cached_at", 0))))
        except (TypeError, ValueError):
            return None

    # ---------- desktop fallbacks ----------

    @staticmethod
    def open_uri(uri: str) -> bool:
        if os.name != "nt" or not uri:
            return False
        try:
            import ctypes

            result = ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, 1)
            return int(result) > 32
        except Exception:
            return False

    @staticmethod
    def open_desktop_search(query: str) -> bool:
        if os.name != "nt":
            return False
        try:
            uri = "spotify:search:" + quote(query, safe="")
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
            return True
        except OSError:
            return False

    # ---------- HTTP/token helpers ----------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        operation: str,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        payload = self._request(method, path, params=params, body=body, operation=operation)
        if not payload:
            return {} if allow_empty else {}
        try:
            value = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SpotifyAPIError(f"Spotify returned invalid JSON while trying to {operation}") from exc
        if not isinstance(value, dict):
            raise SpotifyAPIError(f"Spotify returned an unexpected response while trying to {operation}")
        return value

    def _request_no_content(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        operation: str,
    ) -> None:
        self._request(method, path, params=params, body=body, operation=operation)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        operation: str,
    ) -> bytes:
        self._raise_if_cooling_down(search=False)
        tokens = self._cached_access_tokens()
        if not tokens:
            raise SpotifyAPIError(
                "Spotify API is not authenticated. Run `spotify_player authenticate` once, then restart spotcli."
            )

        query = ""
        if params:
            query = "?" + urlencode(params)
        url = self.API_ROOT + path + query
        encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
        last_401 = False

        for token in tokens:
            request = Request(
                url,
                data=encoded_body,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "spotcli-local/0.11",
                },
            )
            try:
                with urlopen(request, timeout=10) as response:
                    return response.read()
            except HTTPError as exc:
                if exc.code == 401:
                    last_401 = True
                    continue
                detail, reason = self._http_error_detail(exc)
                if exc.code == 429:
                    retry_after = _int_or_none(exc.headers.get("Retry-After"))
                    cooldown = retry_after or 60
                    self._cooldown_until = max(self._cooldown_until, time.monotonic() + cooldown)
                    self._quota_exceeded = reason == "QUOTA_EXCEEDED"
                    retry_text = f" retry in about {retry_after}s" if retry_after else " cooldown active"
                    quota = " shared quota exhausted;" if self._quota_exceeded else ""
                    raise SpotifyAPIError(f"Spotify API rate-limited while trying to {operation};{quota}{retry_text}".replace(";;", ";")) from exc
                if exc.code == 403:
                    raise SpotifyAPIError(
                        f"Spotify refused to {operation} (403). This usually means the cached token lacks the required scope, "
                        "the active device is restricted, or the action requires Spotify Premium. "
                        "Run `spotify_player authenticate` again and retry."
                    ) from exc
                if exc.code == 404:
                    raise SpotifyAPIError(f"Spotify could not {operation}: no compatible active device or item was found") from exc
                suffix = f": {detail[:160]}" if detail else ""
                raise SpotifyAPIError(f"Spotify failed to {operation} (HTTP {exc.code}){suffix}") from exc
            except URLError as exc:
                raise SpotifyAPIError(f"Spotify network error while trying to {operation}: {exc.reason}") from exc
            except OSError as exc:
                raise SpotifyAPIError(f"Spotify network error while trying to {operation}: {exc}") from exc

        if last_401:
            raise SpotifyAPIError(
                "cached Spotify token expired. Run `spotify_player authenticate` again, then restart spotcli."
            )
        raise SpotifyAPIError(f"Spotify could not {operation}")

    @staticmethod
    def _http_error_detail(exc: HTTPError) -> tuple[str, str]:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            body = json.loads(raw)
        except Exception:
            return "", ""
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or ""), str(error.get("reason") or "").upper()
        return str(error or ""), ""

    def _raise_if_cooling_down(self, *, search: bool) -> None:
        remaining = self._remaining_cooldown()
        if remaining is None:
            return
        if search:
            raise SearchRateLimited(retry_after=remaining, quota_exceeded=self._quota_exceeded)
        suffix = " (shared quota exhausted)" if self._quota_exceeded else ""
        raise SpotifyAPIError(f"Spotify API cooldown active for about {remaining}s{suffix}")

    def cooldown_seconds(self) -> int | None:

        ##Return remaining API cooldown seconds, if a 429 cooldown is active.

        return self._remaining_cooldown()

    async def wait_for_cooldown(self) -> None:

        ##Sleep until the current Spotify API Retry-After window has elapsed.
        ##Import locally so the synchronous backend remains cheap for normal calls.

        import asyncio
        while True:
            remaining = self._remaining_cooldown()
            if remaining is None:
                return
            await asyncio.sleep(min(float(remaining) + 0.15, 5.0))

    def _remaining_cooldown(self) -> int | None:
        remaining = int(self._cooldown_until - time.monotonic())
        return max(1, remaining) if remaining > 0 else None

    @staticmethod
    def _parse_search_results(data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []

        for track in ((data.get("tracks") or {}).get("items") or []):
            if not isinstance(track, dict):
                continue
            results.append(_track_result(track))

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
                paths.extend(sorted(root.glob("*_token.json")))
                paths.extend(sorted(root.glob("*token*.json")))
            except OSError:
                pass
        return list(dict.fromkeys(paths))


def _track_result(track: dict[str, Any]) -> SearchResult:
    artists = track.get("artists") or []
    artist = ", ".join(
        str(a.get("name", "")) for a in artists if isinstance(a, dict) and a.get("name")
    )
    return SearchResult(
        kind="track",
        name=str(track.get("name") or "Unknown track"),
        artist=artist,
        uri=str(track.get("uri") or ""),
    )


def _spotify_id(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("spotify:"):
        parts = uri.split(":")
        return parts[-1] if len(parts) >= 3 else ""
    return uri.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
