"""Post-download file organization: flat structure + metadata JSONs."""

import json
import os
import re
import shutil
import logging
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger("flacjacket")


# ---------------------------------------------------------------------------
# Filename utilities
# ---------------------------------------------------------------------------

def sanitize_filename(text: str) -> str:
    """Make text safe for FAT32 filenames."""
    text = unicodedata.normalize("NFC", text)
    # Replace forbidden FAT32 characters
    text = re.sub(r'[\\/:*?"<>|]', '_', text)
    # Collapse runs of underscores/spaces
    text = re.sub(r'[_\s]+', ' ', text)
    text = text.strip(' .')
    if len(text) > 120:
        text = text[:120].rstrip()
    return text


def make_track_filename(artist: str, track_name: str) -> str:
    """Canonical flat filename: 'Track - Artist.flac'."""
    safe_artist = sanitize_filename(artist) or "Unknown Artist"
    safe_track = sanitize_filename(track_name) or "Unknown Track"
    return f"{safe_track} - {safe_artist}.flac"


def make_lyrics_filename(artist: str, track_name: str) -> str:
    """Canonical flat lyrics filename: 'Track - Artist.lrc'."""
    safe_artist = sanitize_filename(artist) or "Unknown Artist"
    safe_track = sanitize_filename(track_name) or "Unknown Track"
    return f"{safe_track} - {safe_artist}.lrc"


# ---------------------------------------------------------------------------
# Post-download flattening (staging -> tracks/ + lyrics/)
# ---------------------------------------------------------------------------

def flatten_download(staging_dir: str, tracks_dir: str, lyrics_dir: str,
                     download_result: dict) -> list[dict]:
    """
    Move files from spotiflac-cli staging output into the flat structure.

    spotiflac-cli creates Artist/Album/Track.flac inside staging_dir.
    This function moves .flac -> tracks/ and .lrc -> lyrics/ with flat
    naming (Artist - Track.flac). Duplicates are detected at move time.

    Returns list of {artist, track_name, album_name, filename, was_new}.
    """
    os.makedirs(tracks_dir, exist_ok=True)
    os.makedirs(lyrics_dir, exist_ok=True)

    processed = []

    # Try to get file paths from JSON output
    json_results = download_result.get("results") or []
    source_files = []
    for item in json_results:
        if isinstance(item, dict):
            path = item.get("file") or item.get("path") or ""
            if path:
                source_files.append(path)

    if source_files:
        for file_path in source_files:
            result = _move_file_to_flat(staging_dir, tracks_dir, lyrics_dir, file_path)
            if result:
                processed.append(result)
    else:
        # Fallback: scan staging_dir for all .flac files
        processed = _scan_and_move_all(staging_dir, tracks_dir, lyrics_dir)

    # Clean up empty directories in staging
    _cleanup_empty_dirs(staging_dir)

    return processed


def _move_file_to_flat(staging_dir: str, tracks_dir: str, lyrics_dir: str,
                       file_path: str) -> dict | None:
    """Move a single .flac (and its .lrc) from staging to flat dirs."""
    full_path = file_path if os.path.isabs(file_path) else os.path.join(staging_dir, file_path)
    if not os.path.isfile(full_path):
        return None

    # Parse artist/album/track from path structure
    try:
        rel = os.path.relpath(full_path, staging_dir)
    except ValueError:
        return None
    parts = Path(rel).parts

    if len(parts) == 3:
        artist, album_name, raw_filename = parts
    elif len(parts) == 2:
        artist, raw_filename = parts
        album_name = ""
    elif len(parts) == 1:
        artist = "Unknown Artist"
        album_name = ""
        raw_filename = parts[0]
    else:
        return None

    track_name = os.path.splitext(raw_filename)[0]
    # Strip leading track numbers like "01. Song" or "02 - Song", but only
    # if they look like real prefixes (1-3 digit number followed by a separator,
    # and the remainder is a real title, not just more digits)
    stripped = re.sub(r'^\d{1,3}[\s.\-_]+', '', track_name)
    if stripped and not stripped.isdigit():
        track_name = stripped

    flat_name = make_track_filename(artist, track_name)
    dest_flac = os.path.join(tracks_dir, flat_name)

    was_new = True
    if os.path.exists(dest_flac):
        # Duplicate — remove source, keep existing
        os.remove(full_path)
        was_new = False
        log.info("Dedup: skipped %s (already exists)", flat_name)
    else:
        shutil.move(full_path, dest_flac)

    # Handle matching .lrc
    lrc_source = os.path.splitext(full_path)[0] + ".lrc"
    lrc_name = make_lyrics_filename(artist, track_name)
    lrc_dest = os.path.join(lyrics_dir, lrc_name)

    if os.path.isfile(lrc_source):
        if os.path.exists(lrc_dest):
            os.remove(lrc_source)
        else:
            shutil.move(lrc_source, lrc_dest)

    return {
        "artist": artist,
        "track_name": track_name,
        "album_name": album_name,
        "filename": flat_name,
        "was_new": was_new,
    }


def _scan_and_move_all(staging_dir: str, tracks_dir: str, lyrics_dir: str) -> list[dict]:
    """Fallback: walk staging_dir for all .flac files and flatten them."""
    processed = []
    if not os.path.isdir(staging_dir):
        return processed

    for root, dirs, files in os.walk(staging_dir):
        for fname in files:
            if fname.endswith(".flac"):
                full_path = os.path.join(root, fname)
                result = _move_file_to_flat(staging_dir, tracks_dir, lyrics_dir, full_path)
                if result:
                    processed.append(result)
    return processed


def _cleanup_empty_dirs(base_dir: str, exclude: set | None = None):
    """Remove empty directories bottom-up under base_dir."""
    if not os.path.isdir(base_dir):
        return
    exclude = exclude or set()
    for root, dirs, files in os.walk(base_dir, topdown=False):
        for d in dirs:
            if d in exclude:
                continue
            dir_path = os.path.join(root, d)
            try:
                os.rmdir(dir_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Track file index (scan real files on disk for fuzzy matching)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation/whitespace for fuzzy matching."""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r'[^a-z0-9 ]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def build_track_index(tracks_dir: str) -> dict[tuple[str, str], str]:
    """
    Scan tracks/ and build a lookup from (normalized_artist, normalized_track)
    to the real filename on disk.

    Expected format: "Track - Artist.flac"
    """
    index: dict[tuple[str, str], str] = {}
    if not os.path.isdir(tracks_dir):
        return index

    for fname in os.listdir(tracks_dir):
        if not fname.endswith(".flac"):
            continue

        stem = fname[:-5]  # strip .flac
        parts = stem.split(" - ", 1)

        if len(parts) == 2:
            track = parts[0].strip()
            artist = parts[1].strip()
        else:
            continue

        key = (_normalize(artist), _normalize(track))
        if key not in index:
            index[key] = fname

    return index


def find_track_file(index: dict[tuple[str, str], str],
                    artist: str, track_name: str) -> str:
    """Look up a real filename from the index. Returns filename or ""."""
    key = (_normalize(artist), _normalize(track_name))
    return index.get(key, "")


# ---------------------------------------------------------------------------
# Album JSON generation
# ---------------------------------------------------------------------------

def generate_album_json(output_dir: str, album_id: str, album_name: str,
                        artist_name: str, track_files: list[dict]):
    """Write a single album metadata JSON to albums/."""
    albums_dir = os.path.join(output_dir, "albums")
    os.makedirs(albums_dir, exist_ok=True)

    if artist_name and album_name:
        safe_name = sanitize_filename(f"{artist_name} - {album_name}")
    else:
        safe_name = sanitize_filename(artist_name or album_name or "")
    safe_name = safe_name or album_id
    filepath = os.path.join(albums_dir, f"{safe_name}.json")

    album_data = {
        "album_id": album_id,
        "album_name": album_name,
        "artist_name": artist_name,
        "generated_at": datetime.utcnow().isoformat(),
        "tracks": track_files,
    }

    with open(filepath, "w") as f:
        json.dump(album_data, f, indent=2)

    log.info("Wrote album JSON: %s (%d tracks)", safe_name, len(track_files))


def generate_all_album_jsons(tracker, output_dir: str, spotify_client=None):
    """Regenerate album JSONs for all downloaded albums."""
    tracks_dir = os.path.join(output_dir, "tracks")
    index = build_track_index(tracks_dir)

    album_downloads = tracker.conn.execute(
        "SELECT spotify_id, name, artist FROM downloads WHERE type = 'album'"
    ).fetchall()

    for row in album_downloads:
        album_id = row["spotify_id"]
        album_name = row["name"] or ""
        artist_name = row["artist"] or ""

        track_list = []

        # Try Spotify API for ordered tracklist
        if spotify_client:
            try:
                api_tracks = spotify_client.get_album_track_names(album_id)
                for i, (tname, tartist) in enumerate(api_tracks, 1):
                    a = tartist or artist_name
                    fname = find_track_file(index, a, tname)
                    track_list.append({
                        "track_number": i,
                        "track_name": tname,
                        "artist_name": a,
                        "filename": fname,
                    })
            except Exception as e:
                log.warning("API tracklist failed for %s: %s", album_name, e)

        # Fallback: plays table
        if not track_list:
            rows = tracker.conn.execute("""
                SELECT DISTINCT track_name, artist_name
                FROM plays WHERE album_id = ? AND track_name IS NOT NULL
                ORDER BY track_name
            """, (album_id,)).fetchall()
            for i, r in enumerate(rows, 1):
                fname = find_track_file(index, r["artist_name"], r["track_name"])
                track_list.append({
                    "track_number": i,
                    "track_name": r["track_name"],
                    "artist_name": r["artist_name"],
                    "filename": fname,
                })

        if track_list:
            generate_album_json(output_dir, album_id, album_name, artist_name, track_list)


# ---------------------------------------------------------------------------
# Playlist JSON generation
# ---------------------------------------------------------------------------

def generate_playlist_json(tracker, output_dir: str):
    """
    Generate playlist JSON files mapping playlists to flat track filenames.
    Writes to {output_dir}/playlists/{name}.json.
    """
    mapping = tracker.get_playlist_track_mapping()
    if not mapping:
        return

    playlist_dir = os.path.join(output_dir, "playlists")
    os.makedirs(playlist_dir, exist_ok=True)
    tracks_dir = os.path.join(output_dir, "tracks")
    index = build_track_index(tracks_dir)

    for context_uri, tracks in mapping.items():
        playlist_id = context_uri.split(":")[-1] if ":" in context_uri else context_uri

        # Get playlist name from DB
        playlist_name = None
        for table in ("playlist_scores", "playlist_tracks"):
            if playlist_name:
                break
            try:
                col = "playlist_name"
                row = tracker.conn.execute(
                    f"SELECT {col} FROM {table} WHERE playlist_id = ? LIMIT 1",
                    (playlist_id,),
                ).fetchone()
                if row and row[col]:
                    playlist_name = row[col]
            except Exception:
                pass

        track_entries = []
        for track in tracks:
            artist = track.get("artist_name") or ""
            tname = track.get("track_name") or ""
            filename = find_track_file(index, artist, tname)

            track_entries.append({
                "track_id": track["track_id"],
                "track_name": tname,
                "artist_name": artist,
                "album_name": track.get("album_name") or "",
                "filename": filename,
                "source": track.get("download_type") or "",
            })

        playlist_data = {
            "playlist_id": playlist_id,
            "playlist_uri": context_uri,
            "playlist_name": playlist_name or playlist_id,
            "generated_at": datetime.utcnow().isoformat(),
            "tracks": track_entries,
        }

        safe_name = sanitize_filename(playlist_name or playlist_id) or playlist_id
        filepath = os.path.join(playlist_dir, f"{safe_name}.json")
        with open(filepath, "w") as f:
            json.dump(playlist_data, f, indent=2)


# ---------------------------------------------------------------------------
# Artist JSON generation
# ---------------------------------------------------------------------------

def generate_all_artist_jsons(output_dir: str, tracker=None):
    """
    Generate one JSON per artist in artists/, listing all their tracks
    and which albums they belong to. Built from files on disk + album JSONs.
    """
    tracks_dir = os.path.join(output_dir, "tracks")
    albums_dir = os.path.join(output_dir, "albums")
    artists_dir = os.path.join(output_dir, "artists")
    os.makedirs(artists_dir, exist_ok=True)

    # Build artist -> tracks mapping from disk
    # Filename format: "Track - Artist.flac"
    artists: dict[str, list[dict]] = {}
    for fname in sorted(os.listdir(tracks_dir)) if os.path.isdir(tracks_dir) else []:
        if not fname.endswith(".flac"):
            continue
        stem = fname[:-5]
        parts = stem.split(" - ", 1)
        if len(parts) != 2:
            continue
        track_name = parts[0].strip()
        artist_name = parts[1].strip()

        artists.setdefault(artist_name, []).append({
            "track_name": track_name,
            "filename": fname,
        })

    # Build album lookup: track filename -> list of album names
    track_to_albums: dict[str, list[str]] = {}
    if os.path.isdir(albums_dir):
        for jf in os.listdir(albums_dir):
            if not jf.endswith(".json"):
                continue
            try:
                with open(os.path.join(albums_dir, jf)) as f:
                    album = json.load(f)
                album_name = album.get("album_name", "")
                for t in album.get("tracks", []):
                    fn = t.get("filename", "")
                    if fn:
                        track_to_albums.setdefault(fn, []).append(album_name)
            except Exception:
                continue

    # Write artist JSONs
    for artist_name, tracks in artists.items():
        # Annotate tracks with album info
        album_set = set()
        for t in tracks:
            albums = track_to_albums.get(t["filename"], [])
            t["album_name"] = albums[0] if albums else ""
            album_set.update(albums)

        artist_data = {
            "artist_name": artist_name,
            "albums": sorted(album_set),
            "generated_at": datetime.utcnow().isoformat(),
            "tracks": tracks,
        }

        safe_name = sanitize_filename(artist_name) or "Unknown Artist"
        filepath = os.path.join(artists_dir, f"{safe_name}.json")
        with open(filepath, "w") as f:
            json.dump(artist_data, f, indent=2)

    log.info("Wrote %d artist JSONs", len(artists))


# ---------------------------------------------------------------------------
# Retroactive rename (old format -> new format)
# ---------------------------------------------------------------------------

def rename_tracks_to_new_format(output_dir: str) -> dict:
    """
    Rename files from old 'Artist - Track.ext' to new 'Track - Artist.ext'.
    Also renames .albums -> albums, .playlists -> playlists.
    Returns stats dict.
    """
    stats = {"renamed": 0, "skipped": 0, "folders_moved": 0}

    # Rename metadata folders
    for old_name, new_name in [(".albums", "albums"), (".playlists", "playlists")]:
        old_path = os.path.join(output_dir, old_name)
        new_path = os.path.join(output_dir, new_name)
        if os.path.isdir(old_path) and not os.path.isdir(new_path):
            os.rename(old_path, new_path)
            stats["folders_moved"] += 1
            log.info("Renamed %s -> %s", old_name, new_name)

    # Rename track and lyrics files
    for subdir in ("tracks", "lyrics"):
        dir_path = os.path.join(output_dir, subdir)
        if not os.path.isdir(dir_path):
            continue

        ext = ".flac" if subdir == "tracks" else ".lrc"

        for fname in os.listdir(dir_path):
            if not fname.endswith(ext):
                continue

            stem = fname[: -len(ext)]
            parts = stem.split(" - ", 1)
            if len(parts) != 2:
                stats["skipped"] += 1
                continue

            first, second = parts[0].strip(), parts[1].strip()

            # New format: "Track - Artist.ext"
            new_name = f"{second} - {first}{ext}"
            old_full = os.path.join(dir_path, fname)
            new_full = os.path.join(dir_path, new_name)

            if os.path.exists(new_full):
                stats["skipped"] += 1
                continue

            os.rename(old_full, new_full)
            stats["renamed"] += 1

    return stats


# ---------------------------------------------------------------------------
# Migration from old directory structure
# ---------------------------------------------------------------------------

def migrate_existing_files(output_dir: str, tracker) -> dict:
    """
    One-time migration from old Artist/Album/Track and Artist/Track structure
    to flat tracks/ + lyrics/ structure. Idempotent (safe to re-run).

    Returns stats dict.
    """
    tracks_dir = os.path.join(output_dir, "tracks")
    lyrics_dir = os.path.join(output_dir, "lyrics")
    os.makedirs(tracks_dir, exist_ok=True)
    os.makedirs(lyrics_dir, exist_ok=True)

    stats = {"moved": 0, "duplicates": 0, "lyrics_moved": 0, "dirs_removed": 0}
    skip_dirs = {"tracks", "lyrics", "albums", "playlists", "artists", ".staging"}

    for artist_entry in os.scandir(output_dir):
        if not artist_entry.is_dir() or artist_entry.name.startswith("."):
            continue
        if artist_entry.name in skip_dirs:
            continue

        artist_name = artist_entry.name

        # Handle Artist/Track.flac (flat individual tracks)
        for entry in os.scandir(artist_entry.path):
            if entry.is_file() and entry.name.endswith(".flac"):
                track_name = os.path.splitext(entry.name)[0]
                _do_migrate_file(entry.path, artist_name, track_name,
                                 tracks_dir, lyrics_dir, stats)

        # Handle Artist/Album/Track.flac (album tracks)
        for sub_entry in os.scandir(artist_entry.path):
            if not sub_entry.is_dir():
                continue
            for entry in os.scandir(sub_entry.path):
                if entry.is_file() and entry.name.endswith(".flac"):
                    track_name = os.path.splitext(entry.name)[0]
                    # Strip leading track numbers
                    track_name = re.sub(r'^\d+[\s.\-_]+', '', track_name)
                    _do_migrate_file(entry.path, artist_name, track_name,
                                     tracks_dir, lyrics_dir, stats)

    # Clean up empty directories
    _cleanup_empty_dirs(output_dir, exclude=skip_dirs)
    # Count removed dirs
    for artist_entry in os.scandir(output_dir):
        pass  # dirs were removed in _cleanup_empty_dirs

    # Update file_path in downloads table
    for row in tracker.conn.execute(
        "SELECT id, name, artist, type FROM downloads"
    ).fetchall():
        name = row["name"] or ""
        artist = row["artist"] or ""
        if name and artist:
            new_path = "tracks/" + make_track_filename(artist, name)
            tracker.conn.execute(
                "UPDATE downloads SET file_path = ? WHERE id = ?",
                (new_path, row["id"]),
            )
    tracker.conn.commit()

    return stats


def _do_migrate_file(src_path: str, artist: str, track_name: str,
                     tracks_dir: str, lyrics_dir: str, stats: dict):
    """Move a single file into the flat structure during migration."""
    flat_name = make_track_filename(artist, track_name)
    dest = os.path.join(tracks_dir, flat_name)

    if os.path.exists(dest):
        os.remove(src_path)
        stats["duplicates"] += 1
    else:
        shutil.move(src_path, dest)
        stats["moved"] += 1

    # Handle .lrc
    lrc_src = os.path.splitext(src_path)[0] + ".lrc"
    if os.path.isfile(lrc_src):
        lrc_name = make_lyrics_filename(artist, track_name)
        lrc_dest = os.path.join(lyrics_dir, lrc_name)
        if os.path.exists(lrc_dest):
            os.remove(lrc_src)
        else:
            shutil.move(lrc_src, lrc_dest)
            stats["lyrics_moved"] += 1
