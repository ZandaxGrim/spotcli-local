# spotcli-local v0.7

A Windows-first, local terminal Spotify controller with real terminal transparency, album-art rendering, Windows media controls, and Spotify catalog search without requiring you to create your own Spotify developer app.

## What's new in v0.7

- **Reliable playback keys on Windows:** `k`, `n`, and `p` now emit the same Windows system media-key events as physical keyboard media buttons instead of relying on Spotify's GSMTC transport methods.
- **Smooth song progress:** the UI interpolates the position locally between Windows timeline updates, so the progress bar/time moves smoothly instead of jumping every few seconds.
- **Fixed Windows search path:** spotcli no longer calls `spotify_player search` through its CLI socket on Windows. That CLI path currently has a known Windows `os error 10054` bug. Instead, spotcli reuses spotify_player's cached Web API bearer token and talks to Spotify's search endpoint directly.
- Search returns **tracks, albums, and playlists**.
- The transparent Rich/ANSI renderer and album-art block renderer remain intact.

## Requirements

- Windows 10/11
- Python 3.11+
- Spotify Desktop
- `spotify_player` only for its one-time OAuth authentication/cache used by search

## Install

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e .
spotcli
```

For Spotify search, authenticate spotify_player once:

```powershell
spotify_player authenticate
```

After the browser authentication finishes, restart `spotcli`. spotcli reads the cached Web API token directly; it does **not** use spotify_player's Windows CLI socket for searches.

## Controls

| Key | Action |
| --- | --- |
| `k` | Play / pause |
| `n` | Next track |
| `p` | Previous track |
| `/` | Search Spotify |
| `t` | Toggle transparent / solid theme |
| `c` | Cycle palette |
| `q` | Quit |

While searching, `Enter` submits, `Backspace` edits, and `Esc` cancels.

## Appearance

Theme and palette are independent. Themes are `transparent` and `solid`; palettes are `classic`, `spotify`, `midnight`, and `mono`.

The transparent theme leaves terminal backgrounds untouched, so Windows Terminal's own acrylic/opacity/background image remains visible.


## v0.7 notes

- Progress rendering is now independent from Windows media polling, so a slow GSMTC call cannot freeze the UI.
- The progress bar uses 1/8-cell Unicode fill levels, making it visibly advance roughly every half-second instead of one whole character every ~5 seconds.
- Stale GSMTC timeline samples no longer drag the local progress clock backwards.
- Search caches successful queries for 15 minutes and respects Spotify's `Retry-After` header.
- If the shared spotify_player/ncspot API client is rate-limited, spotcli stops retrying and opens the query in Spotify Desktop as a temporary fallback.

The search 429 is upstream: spotify_player's default client ID is shared among many users, so its quota can be exhausted even when spotcli itself has only made one request. A dedicated Spotify client ID remains the stable route for fully in-terminal catalog search.


## v0.7 input and timeline fixes

- Fixed arrow keys and Windows Terminal mouse-wheel events accidentally triggering playback commands.
- Restored the original whole-cell progress bar.
- Fixed the elapsed-time clock by comparing the Windows playback-status enum directly, so the local clock can advance every second between Spotify timeline samples.
- Search results are selectable: use **Up/Down** and press **Enter** to open the selected track, album, or playlist in Spotify Desktop.
- Press **Esc** outside search-entry mode to clear the current result list.
