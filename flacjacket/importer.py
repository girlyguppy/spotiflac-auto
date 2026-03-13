"""Import Spotify streaming history exports (extended and Account Data formats)."""

import json
import os
import glob
import time
from datetime import datetime


def _detect_format(entries: list[dict]) -> str:
    """Detect whether entries are extended history or Account Data format.

    Extended format has: ts, ms_played, spotify_track_uri, master_metadata_*
    Account Data format has: endTime, artistName, trackName, msPlayed
    """
    if not entries:
        return "unknown"
    sample = entries[0]
    if "spotify_track_uri" in sample and "ms_played" in sample:
        return "extended"
    if "endTime" in sample and "artistName" in sample:
        return "account_data"
    return "unknown"


def parse_export_file(filepath: str, min_play_ms: int = 30000,
                      since: datetime | None = None) -> list[dict]:
    """
    Parse a single Spotify export JSON file (extended format: endsong_*.json).

    Filters out plays shorter than min_play_ms (default 30 seconds).
    If since is provided, filters out plays before that date.
    Returns list of play dicts ready for insert_play().
    """
    with open(filepath) as f:
        entries = json.load(f)

    plays = []
    for entry in entries:
        ms_played = entry.get("ms_played", 0)
        if ms_played < min_play_ms:
            continue

        track_uri = entry.get("spotify_track_uri", "")
        if not track_uri or not track_uri.startswith("spotify:track:"):
            continue

        track_id = track_uri.split(":")[-1]
        track_name = entry.get("master_metadata_track_name")
        artist_name = entry.get("master_metadata_album_artist_name")
        album_name = entry.get("master_metadata_album_album_name")

        if not track_name:
            continue

        # Spotify export timestamps look like: 2024-01-15T10:30:00Z
        played_at = entry.get("ts", "")
        if not played_at:
            continue

        if since:
            try:
                entry_dt = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
                if entry_dt.replace(tzinfo=None) < since:
                    continue
            except ValueError:
                pass

        plays.append({
            "track_id": track_id,
            "track_name": track_name,
            "artist_name": artist_name or "",
            "album_id": None,  # Export doesn't include album ID — resolved later
            "album_name": album_name or "",
            "album_total_tracks": None,
            "context_type": None,  # Not available in export
            "context_uri": None,
            "played_at": played_at,
            "duration_ms": ms_played,
            "source": "import",
        })

    return plays


def parse_account_data_file(filepath: str, min_play_ms: int = 30000,
                            since: datetime | None = None) -> list[dict]:
    """
    Parse a Spotify Account Data streaming history file.

    Account Data format has: endTime, artistName, trackName, msPlayed
    but NO track URIs — these must be resolved via search afterward.

    Returns list of dicts with artist_name, track_name, played_at, duration_ms.
    The track_id field is left as None (needs resolution).
    """
    with open(filepath) as f:
        entries = json.load(f)

    plays = []
    for entry in entries:
        ms_played = entry.get("msPlayed", 0)
        if ms_played < min_play_ms:
            continue

        artist_name = entry.get("artistName", "")
        track_name = entry.get("trackName", "")
        end_time = entry.get("endTime", "")

        if not track_name or not end_time:
            continue

        # endTime format: "2025-03-07 00:01" — convert to ISO
        try:
            dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        if since and dt < since:
            continue

        played_at = dt.strftime("%Y-%m-%dT%H:%M:%S")

        plays.append({
            "track_id": None,  # Needs resolution via search
            "track_name": track_name,
            "artist_name": artist_name,
            "album_id": None,
            "album_name": "",
            "album_total_tracks": None,
            "context_type": None,
            "context_uri": None,
            "played_at": played_at,
            "duration_ms": ms_played,
            "source": "import",
        })

    return plays


def resolve_track_ids(plays: list[dict], spotify_client) -> list[dict]:
    """
    Resolve track IDs for Account Data plays using Spotify search API.

    Caches results by (artist, track) to avoid redundant API calls.
    Returns only the plays where a track ID was successfully found.
    """
    cache = {}  # (artist_lower, track_lower) -> track_id or None
    resolved = []
    total = len(plays)
    failures = 0

    for i, play in enumerate(plays):
        key = (play["artist_name"].lower(), play["track_name"].lower())

        if key not in cache:
            query = f"track:{play['track_name']} artist:{play['artist_name']}"
            try:
                result = spotify_client.search_track(query, limit=1)
                items = result.get("tracks", {}).get("items", [])
                if items:
                    track = items[0]
                    album = track.get("album", {})
                    cache[key] = {
                        "track_id": track["id"],
                        "album_id": album.get("id"),
                        "album_name": album.get("name", ""),
                        "album_total_tracks": album.get("total_tracks"),
                    }
                else:
                    cache[key] = None
                    failures += 1
                # Be gentle with rate limits
                time.sleep(0.05)
            except Exception as e:
                print(f"    Search failed for '{play['artist_name']} - {play['track_name']}': {e}")
                cache[key] = None
                failures += 1
                time.sleep(1)  # Back off on errors

        cached = cache.get(key)
        if cached:
            play["track_id"] = cached["track_id"]
            play["album_id"] = cached["album_id"]
            play["album_name"] = cached["album_name"]
            play["album_total_tracks"] = cached["album_total_tracks"]
            resolved.append(play)

        if (i + 1) % 100 == 0:
            unique_searched = len(cache)
            print(f"    Progress: {i + 1}/{total} plays processed "
                  f"({unique_searched} unique tracks searched)")

    unique_found = sum(1 for v in cache.values() if v is not None)
    unique_total = len(cache)
    print(f"  Search complete: {unique_found}/{unique_total} unique tracks resolved, "
          f"{len(resolved)}/{total} plays matched")

    return resolved


def find_export_files(directory: str) -> list[str]:
    """Find all streaming history JSON files in the export directory."""
    patterns = [
        os.path.join(directory, "endsong_*.json"),
        os.path.join(directory, "Streaming_History_Audio_*.json"),
        os.path.join(directory, "StreamingHistory*.json"),
    ]
    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def import_export(tracker, directory: str, min_play_ms: int = 30000,
                  since: datetime | None = None,
                  spotify_client=None) -> dict:
    """
    Import all Spotify export files from a directory.

    Handles both extended streaming history and Account Data formats.
    For Account Data format, spotify_client is required to resolve track IDs.

    Returns summary dict with counts.
    """
    files = find_export_files(directory)
    if not files:
        return {
            "error": f"No Spotify export files found in {directory}. "
                     "Expected endsong_*.json, Streaming_History_Audio_*.json, "
                     "or StreamingHistory*.json files.",
            "files_found": 0,
            "total_entries": 0,
            "inserted": 0,
            "skipped": 0,
        }

    total_entries = 0
    total_inserted = 0
    total_skipped = 0
    total_unresolved = 0

    for filepath in files:
        filename = os.path.basename(filepath)
        print(f"  Parsing {filename}...")

        # Detect format by peeking at the file
        with open(filepath) as f:
            entries = json.load(f)
        fmt = _detect_format(entries)

        if fmt == "extended":
            plays = parse_export_file(filepath, min_play_ms, since)
        elif fmt == "account_data":
            plays = parse_account_data_file(filepath, min_play_ms, since)
            if not spotify_client:
                print(f"    Account Data format detected but no Spotify client available.")
                print(f"    Use --resolve to search for track IDs via Spotify API.")
                print(f"    Skipping {len(plays)} entries (no track IDs).")
                total_unresolved += len(plays)
                continue
            print(f"    Account Data format: resolving {len(plays)} plays via Spotify search...")
            pre_resolve = len(plays)
            plays = resolve_track_ids(plays, spotify_client)
            total_unresolved += pre_resolve - len(plays)
        else:
            print(f"    Unknown format, skipping")
            continue

        total_entries += len(plays)

        inserted = tracker.insert_plays_bulk(plays)
        skipped = len(plays) - inserted
        total_inserted += inserted
        total_skipped += skipped

        print(f"    {inserted} new, {skipped} duplicates")

    result = {
        "files_found": len(files),
        "total_entries": total_entries,
        "inserted": total_inserted,
        "skipped": total_skipped,
    }
    if total_unresolved > 0:
        result["unresolved"] = total_unresolved
    return result


def resolve_album_ids(tracker, spotify_client, batch_size: int = 50) -> int:
    """
    For imported plays missing album_id, resolve them via Spotify API.

    Groups tracks, fetches album IDs, updates the plays table.
    Returns number of tracks resolved.
    """
    # Find tracks with no album_id
    rows = tracker.conn.execute("""
        SELECT DISTINCT track_id FROM plays
        WHERE (album_id IS NULL OR album_id = '')
        AND source = 'import'
    """).fetchall()

    track_ids = [r["track_id"] for r in rows]
    if not track_ids:
        return 0

    resolved = 0
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i:i + batch_size]
        try:
            data = spotify_client.get_several_tracks(batch)
        except Exception as e:
            print(f"  Warning: failed to fetch track batch: {e}")
            continue

        for track in data.get("tracks", []):
            if track is None:
                continue
            track_id = track["id"]
            album = track.get("album", {})
            album_id = album.get("id")
            album_name = album.get("name", "")
            total_tracks = album.get("total_tracks", 0)
            release_date = album.get("release_date", "")
            artists = ", ".join(a["name"] for a in album.get("artists", []))

            if album_id:
                tracker.conn.execute(
                    """UPDATE plays SET album_id = ?, album_name = ?,
                       album_total_tracks = ? WHERE track_id = ?""",
                    (album_id, album_name, total_tracks, track_id),
                )
                # Also cache the album
                tracker.cache_album(
                    album_id, album_name, artists, total_tracks, release_date,
                )
                resolved += 1

    tracker.conn.commit()
    print(f"  Resolved album IDs for {resolved}/{len(track_ids)} tracks")
    return resolved
