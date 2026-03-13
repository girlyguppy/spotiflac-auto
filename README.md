# SpotiFLAC Auto

Automated Spotify-to-FLAC download pipeline. Monitors your Spotify listening history, scores albums and tracks by how much you actually listen to them, and automatically downloads qualifying music as lossless FLAC files.

Built on top of [SpotiFLAC](https://github.com/afkarxyz/SpotiFLAC) by afkarxyz — this fork adds a headless CLI downloader and a Python automation layer that handles the full pipeline from listening data to organized FLAC library.

## What's in this repo

- **`cmd/cli/`** — Headless CLI wrapper around SpotiFLAC's Go backend. Downloads tracks, albums, and playlists from a Spotify URL without the desktop GUI.
- **`backend/`** — SpotiFLAC's download engine (Go). Handles Tidal, Qobuz, Deezer, and Amazon with automatic fallback.
- **`flacjacket/`** — Python automation package. Polls Spotify, scores music, decides what to download, calls `spotiflac-cli`, and organizes the output.
- **`systemd/`** — Service/timer units for running unattended on a NAS or server.

## What it does

- **Polls Spotify** for your recently played tracks (or imports your Spotify data export)
- **Scores albums and tracks** based on listening behavior — albums by track coverage, tracks by play count
- **Downloads FLACs** automatically via `spotiflac-cli` when songs cross configurable thresholds
- **Deduplicates** intelligently — same song under different Spotify IDs won't download twice
- **Organizes files** into a flat structure (`tracks/` and `lyrics/`) with album and playlist metadata as JSON files
- **Syncs playlist membership** via the Spotify API to build playlist indexes
- **Filters autoplay** — optional scoring filter ignores tracks from unknown artists that Spotify autoplayed
- **Runs unattended** as a systemd service/timer on a NAS or server

## How it works

```
Spotify listening data
        |
        v
   Play history DB (SQLite)
        |
        v
   Scoring engine
   (album coverage, track plays, playlist membership)
        |
        v
   Download decisions
   (configurable thresholds)
        |
        v
   spotiflac-cli (FLAC downloads from Tidal/Qobuz/Deezer/Amazon)
        |
        v
   Flat file output: tracks/, lyrics/, .albums/, .playlists/
```

## Prerequisites

- **Go 1.21+** (to build `spotiflac-cli`)
- **Python 3.10+** (stdlib only, no pip dependencies)
- **Spotify Developer App** — create one at [developer.spotify.com](https://developer.spotify.com/dashboard) with redirect URI `http://127.0.0.1:8888/callback`

## Setup

1. **Build spotiflac-cli:**
   ```bash
   go build -o spotiflac-cli ./cmd/cli/
   sudo mv spotiflac-cli /usr/local/bin/
   ```

2. **Create config:**
   ```bash
   mkdir -p ~/.config/flacjacket
   cp config.example.json ~/.config/flacjacket/config.json
   ```
   Edit `config.json` and fill in your Spotify `client_id` and `client_secret`.

3. **Authenticate:**
   ```bash
   python3 -m flacjacket auth
   ```

4. **Import listening history** (optional — if you have a Spotify data export):
   ```bash
   python3 -m flacjacket import ~/path/to/spotify-export/
   ```

5. **Run it:**
   ```bash
   # One-shot: poll, score, download
   python3 -m flacjacket poll

   # Dry run (see what would be downloaded without downloading)
   python3 -m flacjacket poll --dry-run

   # Continuous polling loop
   python3 -m flacjacket poll --loop
   ```

## Using spotiflac-cli standalone

The CLI works independently for one-off downloads:

```bash
# Download a track
spotiflac-cli https://open.spotify.com/track/...

# Download an album to a specific directory
spotiflac-cli -o /mnt/music https://open.spotify.com/album/...

# Use a specific service
spotiflac-cli -s qobuz https://open.spotify.com/track/...

# JSON output (for scripting)
spotiflac-cli -json https://open.spotify.com/track/...
```

Options: `-output`/`-o` (directory), `-service`/`-s` (tidal/qobuz/deezer/amazon/auto), `-lyrics` (save .lrc, default true), `-embed-lyrics`, `-json`, `-config` (path to config.json).

## Commands

| Command | Description |
|---------|-------------|
| `auth` | Authenticate with Spotify (opens browser) |
| `poll` | Poll for new plays, score, and download. `--loop` for continuous. `--dry-run` to preview. |
| `score` | Show current scoring results without downloading |
| `import <path>` | Import Spotify data export (extended or Account Data format) |
| `download <url>` | Manually download a Spotify album/track/playlist URL |
| `status` | Show database statistics |
| `history` | Show recent play history |
| `downloads` | List all downloaded items |
| `blacklist` | Show blacklisted items |
| `skip <url>` | Blacklist an album/track to prevent downloading |
| `unskip <url>` | Remove an item from the blacklist |
| `manage` | Interactive download management (remove/re-download) |
| `sync-playlists` | Sync playlist membership from Spotify API |
| `organize` | Regenerate album and playlist metadata JSONs |
| `migrate` | One-time migration from old directory structure to flat layout |
| `reset` | Clear download records (re-queues everything for download) |

## Configuration

See `config.example.json` for all options. Key settings:

```json
{
  "output_dir": "~/Music",
  "spotiflac_cli_path": "spotiflac-cli",
  "poll_interval_minutes": 30,
  "thresholds": {
    "album_coverage": 0.5,
    "album_min_unique_tracks": 2,
    "track_plays": 3,
    "playlist_min_tracks": 5,
    "min_play_seconds": 30,
    "ignore_autoplay_unknown": true
  }
}
```

- **album_coverage**: Fraction of album tracks you've listened to before it qualifies (0.5 = 50%)
- **album_min_unique_tracks**: Minimum unique tracks played from an album
- **track_plays**: Minimum plays for an individual track to qualify
- **playlist_min_tracks**: Minimum unique tracks from a playlist before it qualifies
- **min_play_seconds**: Ignore plays shorter than this (skip detection)
- **ignore_autoplay_unknown**: Filter out autoplay from artists you've never intentionally listened to

## Output structure

```
{output_dir}/
  tracks/              # All FLAC files, flat, deduplicated
    Artist - Track.flac
  lyrics/              # All LRC files
    Artist - Track.lrc
  .albums/             # Album metadata (JSON)
    Artist - Album.json
  .playlists/          # Playlist metadata (JSON)
    Playlist Name.json
```

Album and playlist JSONs map to the flat track files, so downstream tools can reconstruct album/playlist organization without relying on folder structure.

## Running as a service

Systemd unit files are included in `systemd/`:

```bash
sudo cp systemd/flacjacket@.service /etc/systemd/system/
sudo systemctl enable --now flacjacket@$USER.service
```

This runs the automation in continuous polling mode under your user account.

## License

Based on [SpotiFLAC](https://github.com/afkarxyz/SpotiFLAC) by afkarxyz. See [LICENSE](LICENSE).
