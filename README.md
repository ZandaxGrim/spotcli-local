# spotcli-local

A Windows-first Spotify terminal controller built around local Windows media controls, with Spotify API features added only where needed.

## Features

- Current track, artist, album, album art, progress, play/pause, next, and previous
- Transparent and solid terminal themes
- 12 color palettes
- Spotify search for tracks, albums, and playlists
- Browse your playlists and cached playlist contents
- Persistent playlist cache across restarts
- Play tracks or entire playlists
- Add tracks to the Spotify queue
- View the current queue
- Toggle shuffle
- Control Spotify's Windows app volume
- Automatic Spotify authentication when required
- Automatic retry of playback actions after Spotify API rate limits
- Configurable keybinds
- Optional launch at Windows sign-in
- `spotcli` command available globally after installation

spotcli keeps normal playback controls local through Windows wherever possible to reduce Spotify API usage.

## Requirements

- Windows 10 or Windows 11
- Python 3.11+
- Spotify Desktop
- `spotify_player` for Spotify authentication and API-backed features
- Spotify Premium may be required for some playback-changing API actions

## Install

Download or clone the project, then open PowerShell in the project folder.

Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The installer:

- installs spotcli under `%LOCALAPPDATA%\spotcli-local`
- creates and manages its own Python virtual environment
- installs the required Python dependencies
- adds spotcli to your user `PATH`
- adds spotcli to Windows Startup by default

After installation, close and reopen your terminal. Then launch spotcli from anywhere:

```powershell
spotcli
```

Verify the command with:

```powershell
where.exe spotcli
```

### Install without Windows Startup

```powershell
.\install.ps1 -NoStartup
```

You can change startup behavior later:

```powershell
spotcli --install-startup
spotcli --remove-startup
```

## Spotify authentication

If authentication is missing, spotcli automatically runs:

```powershell
spotify_player authenticate
```

Complete the Spotify login in your browser, then return to the terminal.

You can also start authentication manually:

```powershell
spotcli --authenticate
```

## Controls

| Key | Action |
| --- | --- |
| `k` | Play / pause |
| `n` | Next track |
| `p` | Previous track |
| `/` | Search Spotify |
| `↑` / `↓` | Move selection |
| `Enter` | Play track or open album/playlist |
| `a` | Add selected track to queue |
| `x` | Play selected/current playlist |
| `l` | Open your playlists |
| `r` | Refresh the current cached view |
| `u` | View playback queue |
| `s` | Toggle shuffle |
| `[` / `]` | Spotify app volume down/up |
| `Esc` / `Backspace` | Go back |
| `t` | Transparent / solid mode |
| `c` | Cycle color palette |
| `q` | Quit |

## Themes

Transparency and color are separate settings.

Available palettes:

`classic`, `spotify`, `midnight`, `mono`, `purple`, `ocean`, `sunset`, `cyberpunk`, `rose`, `amber`, `forest`, `ice`

The transparent theme leaves terminal background cells unset so Windows Terminal can provide the actual opacity or acrylic effect.

## Cache

Playlist data is stored at:

```text
%APPDATA%\spotcli\cache.json
```

Previously loaded playlists remain available after restarting spotcli and can still be browsed when the Spotify API is rate-limited. Press `r` to refresh the current playlist, album, or playlist list from Spotify.

Configuration is stored at:

```text
%APPDATA%\spotcli\config.toml
```

OAuth tokens are not stored in spotcli's cache.

## Updating

Extract the newer release and run:

```powershell
.\update.ps1
```

Your existing configuration and playlist cache are preserved.

## API usage

spotcli uses Windows locally for now-playing information, album art, progress, playback controls, Spotify app volume, and cached playlist browsing.

Spotify's API is used for search, fetching or refreshing playlists, queue operations, shuffle control, and playing selected Spotify tracks or playlists.

When Spotify returns a rate limit, spotcli waits for the API cooldown and retries deferred playback instead of opening the Spotify client as a fallback.
