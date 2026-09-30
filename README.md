# spotcli-local v0.11.0

A Windows-first, local Spotify terminal controller with album art, transparent-terminal support, search/browse, queue controls, playlists, shuffle, local Spotify app volume, and persistent playlist caching.

## What's new in v0.11

### Installer now adds `spotcli` to PATH

`install.ps1` now adds `%LOCALAPPDATA%\spotcli-local` to your **user PATH** automatically. After installing, open a new terminal and launch the app from anywhere with:

```powershell
spotcli
```

Verify the command with:

```powershell
where.exe spotcli
```

The expected result is `%LOCALAPPDATA%\spotcli-local\spotcli.cmd`. The installer is idempotent and will not add duplicate PATH entries.

### Play an entire playlist

Press `x` while a playlist is selected in search or **Your playlists** to start that playlist directly. You can also press `x` while browsing inside a playlist to start the current playlist context.

### Deferred playback during API cooldowns

Selecting a track no longer opens Spotify Desktop as a fallback when the Web API is rate-limited. If Spotify returns a `429`/`Retry-After`, spotcli remembers the requested track, waits for that cooldown in the background, and retries the play action once the timer expires. Selecting another track replaces the pending deferred play request.

### More palettes

The `c` key now cycles through: `classic`, `spotify`, `midnight`, `mono`, `purple`, `ocean`, `sunset`, `cyberpunk`, `rose`, `amber`, `forest`, and `ice`. Palette and transparency remain independent, so every palette can be used with either the transparent or solid theme.


### Persistent playlist memory

Playlist browsing is now cached to disk at:

```text
%APPDATA%\spotcli\cache.json
```

This cache contains only browse metadata such as playlist/track names and Spotify URIs. OAuth tokens are **not** copied into spotcli's cache.

Once your playlists and their tracks have been loaded successfully, they remain available after closing spotcli, rebooting Windows, or hitting Spotify API rate limits later.

- `l` opens your cached playlist list immediately when available.
- Opening a previously cached playlist uses its saved track list without making another API request.
- `r` force-refreshes the cache for the current playlist view.
- In **Your playlists**, `r` refreshes the playlist list.
- Inside a playlist or album, `r` refreshes that collection's saved tracks.
- Cache survives spotcli upgrades because it lives under `%APPDATA%`, outside the installation directory.

This deliberately makes browsing cache-first instead of expiring data automatically. You decide when Spotify gets another request.

### Automatic authentication

If spotcli starts and cannot find a cached `spotify_player` OAuth token, it now automatically runs:

```powershell
spotify_player authenticate
```

before entering the TUI. Complete the browser/login flow and spotcli continues automatically afterward.

You can also trigger authentication manually without launching the player:

```powershell
spotcli --authenticate
```

If `spotify_player` is not installed or not in `PATH`, local Windows controls still work, but API-backed search/library features will show an authentication error.

### Install once + Windows startup

You no longer need to create a venv and run `pip install -e .` every time you launch the program.

From the extracted release folder, run once:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The installer creates a permanent environment under:

```text
%LOCALAPPDATA%\spotcli-local\.venv
```

and registers spotcli in your Windows Startup folder. After that, it starts automatically when you sign into Windows.

The installer also adds the install directory to your user `PATH`, so after opening a new terminal you can simply run:

```powershell
spotcli
```

The manual launcher is:

```text
%LOCALAPPDATA%\spotcli-local\spotcli.cmd
```

To install without automatic startup:

```powershell
.\install.ps1 -NoStartup
```

You can also manage startup directly:

```powershell
spotcli --install-startup
spotcli --remove-startup
```

When a future release is extracted, update the permanent install with:

```powershell
.\update.ps1
```

Your `%APPDATA%\spotcli` config and cache remain untouched.

## Requirements

- Windows 10/11
- Python 3.11+
- Spotify Desktop
- `spotify_player` available for OAuth-backed Spotify Web API features
- Spotify Premium may still be required for Web API playback-changing actions

## Default controls

| Key | Action |
| --- | --- |
| `k` | Play / pause via Windows media key |
| `n` | Next track |
| `p` | Previous track |
| `/` | Search Spotify |
| `↑` / `↓` | Move selection |
| `Enter` | Play a track / open an album or playlist |
| `a` | Add selected track to queue |
| `x` | Play selected/current playlist |
| `l` | View cached/user playlists |
| `r` | Refresh the current playlist/collection cache |
| `u` | View playback queue |
| `s` | Toggle shuffle |
| `[` / `]` | Change Spotify.exe Windows app volume |
| `Esc` / `Backspace` | Go back |
| `t` | Toggle transparent / solid theme |
| `c` | Cycle palette |
| `q` | Quit |

## API-minimizing behavior

These stay local and consume zero Spotify Web API requests:

- now-playing metadata
- album art
- progress
- play / pause
- next / previous
- Spotify.exe app volume
- cached playlist browsing
- cached playlist track browsing

Spotify's Web API is used only for things Windows cannot provide locally:

- catalog search when a cached search is unavailable
- first playlist/library fetch or an explicit `r` refresh
- first collection fetch or an explicit `r` refresh
- queue read/write
- shuffle read/write
- direct playback of a selected Spotify URI

One playback-state request is still made at startup to initialize the shuffle indicator.

## Configurable keybinds

`%APPDATA%\spotcli\config.toml`:

```toml
theme = "transparent"
palette = "classic"
volume_step = 5

[keys]
play_pause = "k"
next = "n"
previous = "p"
search = "/"
queue_selected = "a"
play_playlist = "x"
playlists = "l"
refresh_cache = "r"
queue_view = "u"
shuffle = "s"
volume_down = "["
volume_up = "]"
theme = "t"
palette = "c"
quit = "q"
```

## Transparency

Transparent theme leaves terminal background cells unset. Actual opacity/acrylic is controlled by Windows Terminal. Solid theme paints the application background itself.
