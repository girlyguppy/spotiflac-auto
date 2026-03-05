"""Import Spotify extended streaming history export."""

import json
import os
import glob
from datetime import datetime


def parse_export_file(filepath: str, min_play_ms: int = 30000) -> list[dict]:
    """
    Parse a single Spotify export JSON file (endsong_*.json).

    Filters out plays shorter than min_play_ms (default 30 seconds).
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


def find_export_files(directory: str) -> list[str]:
    """Find all endsong_*.json files in the export directory."""
    patterns = [
        os.path.join(directory, "endsong_*.json"),
        os.path.join(directory, "Streaming_History_Audio_*.json"),
        os.path.join(directory, "StreamingHistory*.json"),
    ]
    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def import_export(tracker, directory: str, min_play_ms: int = 30000) -> dict:
    """
    Import all Spotify export files from a directory.

    Returns summary dict with counts.
    """
    files = find_export_files(directory)
    if not files:
        return {
            "error": f"No Spotify export files found in {directory}. "
                     "Expected endsong_*.json or Streaming_History_Audio_*.json files.",
            "files_found": 0,
            "total_entries": 0,
            "inserted": 0,
            "skipped": 0,
        }

    total_entries = 0
    total_inserted = 0
    total_skipped = 0

    for filepath in files:
        filename = os.path.basename(filepath)
        print(f"  Parsing {filename}...")
        plays = parse_export_file(filepath, min_play_ms)
        total_entries += len(plays)

        inserted = tracker.insert_plays_bulk(plays)
        skipped = len(plays) - inserted
        total_inserted += inserted
        total_skipped += skipped

        print(f"    {inserted} new, {skipped} duplicates")

    return {
        "files_found": len(files),
        "total_entries": total_entries,
        "inserted": total_inserted,
        "skipped": total_skipped,
    }


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
