"""Behavior-based scoring for album, track, and playlist download decisions.

Core principle: behavior reveals preference, no arbitrary time gates.
- If you listened to most of an album, you want it
- If you keep coming back to a track, you want it
- If you're engaging with a playlist, you want the whole thing
- Autoplay from unknown artists is ignored
"""

from .config import Thresholds


def score_albums(
    album_stats: list[dict],
    thresholds: Thresholds,
    blacklisted_ids: set[str] | None = None,
) -> list[dict]:
    """
    Score albums for download.

    Decision: download_album if coverage >= threshold AND unique_tracks >= min.
    No time gate — behavior is the signal.
    Blacklisted albums get decision = "blacklisted".
    """
    results = []
    for album in album_stats:
        total_tracks = album.get("total_tracks") or 0
        unique_played = album.get("unique_tracks_played") or 0
        total_plays = album.get("total_plays") or 0
        album_context_plays = album.get("album_context_plays") or 0

        if total_tracks == 0:
            coverage = 0.0
        else:
            coverage = unique_played / total_tracks

        if blacklisted_ids and album["album_id"] in blacklisted_ids:
            decision = "blacklisted"
        elif (
            coverage >= thresholds.album_coverage
            and unique_played >= thresholds.album_min_unique_tracks
        ):
            decision = "download_album"
        else:
            decision = "skip"

        results.append({
            "album_id": album["album_id"],
            "album_name": album.get("album_name", ""),
            "artist_name": album.get("artist_name", ""),
            "total_tracks": total_tracks,
            "unique_tracks_played": unique_played,
            "total_plays": total_plays,
            "album_context_plays": album_context_plays,
            "coverage": round(coverage, 3),
            "score": round(coverage, 3),
            "first_played": album.get("first_played"),
            "last_played": album.get("last_played"),
            "decision": decision,
        })

    return results


def score_tracks(
    track_stats: list[dict],
    album_decisions: dict[str, str],
    thresholds: Thresholds,
    blacklisted_ids: set[str] | None = None,
    covered_tracks: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """
    Score individual tracks for download.

    A track is marked for download if:
    - It has >= track_plays threshold total plays
    - Its album is NOT already being downloaded as a full album
    - It is not blacklisted
    - It is not covered by an album being downloaded (compilation dedup)
    """
    results = []
    for track in track_stats:
        total_plays = track.get("total_plays") or 0
        playlist_plays = track.get("playlist_plays") or 0
        album_id = track.get("album_id")

        score = total_plays + playlist_plays * 0.5

        album_already_covered = (
            album_id
            and album_id in album_decisions
            and album_decisions[album_id] == "download_album"
        )

        if blacklisted_ids and track["track_id"] in blacklisted_ids:
            decision = "blacklisted"
        elif album_already_covered:
            decision = "skip"
        elif (
            covered_tracks
            and (
                (track.get("track_name") or "").lower(),
                (track.get("artist_name") or "").lower(),
            ) in covered_tracks
        ):
            decision = "covered_by_album"
        elif total_plays >= thresholds.track_plays:
            decision = "download"
        else:
            decision = "skip"

        results.append({
            "track_id": track["track_id"],
            "track_name": track.get("track_name", ""),
            "artist_name": track.get("artist_name", ""),
            "album_id": album_id,
            "total_plays": total_plays,
            "playlist_plays": playlist_plays,
            "score": round(score, 3),
            "first_played": track.get("first_played"),
            "last_played": track.get("last_played"),
            "decision": decision,
        })

    return results


def score_playlists(
    playlist_stats: list[dict],
    thresholds: Thresholds,
) -> list[dict]:
    """
    Score playlists for full download.

    A playlist is marked for download if we've seen >= playlist_min_tracks
    unique tracks played from it.
    """
    results = []
    for pl in playlist_stats:
        unique_tracks = pl.get("unique_tracks_seen") or 0
        total_plays = pl.get("total_plays") or 0

        if unique_tracks >= thresholds.playlist_min_tracks:
            decision = "download_playlist"
        else:
            decision = "skip"

        results.append({
            "playlist_id": pl["playlist_id"],
            "playlist_name": pl.get("playlist_name", ""),
            "unique_tracks_seen": unique_tracks,
            "total_plays": total_plays,
            "first_seen": pl.get("first_seen"),
            "last_seen": pl.get("last_seen"),
            "decision": decision,
        })

    return results


def run_scoring(
    tracker, thresholds: Thresholds, spotify_client=None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Run full scoring cycle: albums, tracks, playlists. Save results to DB.

    If spotify_client is provided, fetches full album tracklists so the
    dedup catches tracks even when the import resolved them to single IDs.

    Returns (album_scores, track_scores, playlist_scores).
    """
    # Fetch blacklisted IDs once
    blacklisted_ids = {b["spotify_id"] for b in tracker.get_blacklist()}

    known_artists = tracker.get_known_artists() if thresholds.ignore_autoplay_unknown else None

    album_stats = tracker.get_album_play_stats(known_artists)
    album_scores = score_albums(album_stats, thresholds, blacklisted_ids)
    tracker.save_album_scores(album_scores)

    album_decisions = {s["album_id"]: s["decision"] for s in album_scores}

    # Build covered_tracks for compilation dedup:
    # tracks from albums being downloaded + already-downloaded albums
    download_album_ids = [
        s["album_id"] for s in album_scores if s["decision"] == "download_album"
    ]
    downloaded_album_rows = tracker.conn.execute(
        "SELECT spotify_id FROM downloads WHERE type = 'album'"
    ).fetchall()
    downloaded_album_ids = [r["spotify_id"] for r in downloaded_album_rows]
    all_covered_album_ids = list(set(download_album_ids + downloaded_album_ids))
    covered_tracks = tracker.get_tracks_covered_by_albums(all_covered_album_ids)

    # If we have API access, fetch full tracklists for download albums
    # so we catch tracks the import resolved to single IDs
    if spotify_client and all_covered_album_ids:
        for album_id in all_covered_album_ids:
            try:
                track_names = spotify_client.get_album_track_names(album_id)
                for name, artist in track_names:
                    covered_tracks.add((name.lower(), artist.lower()))
            except Exception:
                pass  # API failure is non-fatal, DB-only dedup still works

    track_stats = tracker.get_track_play_stats(known_artists)
    track_scores = score_tracks(
        track_stats, album_decisions, thresholds,
        blacklisted_ids, covered_tracks,
    )
    tracker.save_track_scores(track_scores)

    playlist_stats = tracker.get_playlist_play_stats()
    playlist_scores = score_playlists(playlist_stats, thresholds)

    # Enrich playlist scores with names from playlist_tracks table
    for ps in playlist_scores:
        if not ps.get("playlist_name"):
            row = tracker.conn.execute(
                "SELECT playlist_name FROM playlist_tracks WHERE playlist_id = ? LIMIT 1",
                (ps["playlist_id"],),
            ).fetchone()
            if row and row["playlist_name"]:
                ps["playlist_name"] = row["playlist_name"]

    tracker.save_playlist_scores(playlist_scores)

    return album_scores, track_scores, playlist_scores
