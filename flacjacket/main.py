"""FlacJacket CLI — automatic Spotify-to-FLAC download pipeline."""

import argparse
import logging
import os
import subprocess
import sys
import time
import re
from datetime import datetime, timezone

from .config import load_config
from .spotify_client import SpotifyAuth, SpotifyClient
from .play_tracker import PlayTracker
from .scorer import run_scoring
from .downloader import Downloader
from .importer import import_export, resolve_album_ids


log = logging.getLogger("flacjacket")


def setup_logging(log_file: str | None = None):
    handlers = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def cmd_auth(args):
    """Authenticate with Spotify."""
    cfg = load_config(args.config)
    if not cfg.spotify.client_id or not cfg.spotify.client_secret:
        print("Error: spotify.client_id and spotify.client_secret must be set in config.json")
        sys.exit(1)

    auth = SpotifyAuth(
        cfg.spotify.client_id,
        cfg.spotify.client_secret,
        cfg.spotify.redirect_uri,
        cfg.token_path,
    )
    auth.authorize_interactive()


def cmd_poll(args):
    """Poll Spotify for recent plays, score, and download."""
    cfg = load_config(args.config)
    setup_logging(cfg.log_file)

    auth = SpotifyAuth(
        cfg.spotify.client_id,
        cfg.spotify.client_secret,
        cfg.spotify.redirect_uri,
        cfg.token_path,
    )
    client = SpotifyClient(auth)
    tracker = PlayTracker(cfg.resolved_db_path)
    downloader = Downloader(cfg.spotiflac_cli_path, cfg.output_dir, cfg.staging_dir)

    try:
        if args.loop:
            log.info("Starting polling loop (every %d minutes)", cfg.poll_interval_minutes)
            while True:
                _poll_cycle(cfg, client, tracker, downloader, dry_run=args.dry_run)
                log.info("Sleeping %d minutes...", cfg.poll_interval_minutes)
                time.sleep(cfg.poll_interval_minutes * 60)
        else:
            _poll_cycle(cfg, client, tracker, downloader, dry_run=args.dry_run)
    finally:
        tracker.close()


def _poll_cycle(cfg, client, tracker, downloader, dry_run=False):
    """Run one poll-score-download cycle."""
    log.info("--- Poll cycle start ---")

    # 1. Fetch recently played
    last_cursor = tracker.get_state("last_poll_cursor")
    params = {"limit": 50}
    if last_cursor:
        params["after"] = int(last_cursor)

    try:
        data = client.get_recently_played(**params)
    except Exception as e:
        log.error("Failed to fetch recently played: %s", e)
        return

    items = data.get("items", [])
    if not items:
        log.info("No new plays found")
    else:
        # 2. Insert plays
        new_count = 0
        latest_ts = None

        for item in items:
            track = item.get("track", {})
            context = item.get("context") or {}
            played_at = item.get("played_at", "")

            track_id = track.get("id", "")
            if not track_id:
                continue

            album = track.get("album", {})
            album_id = album.get("id", "")
            album_name = album.get("name", "")
            total_tracks = album.get("total_tracks", 0)
            release_date = album.get("release_date", "")
            artists = ", ".join(a["name"] for a in track.get("artists", []))
            album_artists = ", ".join(a["name"] for a in album.get("artists", []))

            context_type = context.get("type")
            context_uri = context.get("uri")

            inserted = tracker.insert_play(
                track_id=track_id,
                track_name=track.get("name", ""),
                artist_name=artists,
                album_id=album_id,
                album_name=album_name,
                album_total_tracks=total_tracks,
                context_type=context_type,
                context_uri=context_uri,
                played_at=played_at,
                duration_ms=track.get("duration_ms", 0),
            )
            if inserted:
                new_count += 1

            # Cache album info
            if album_id and not tracker.get_cached_album(album_id):
                tracker.cache_album(album_id, album_name, album_artists, total_tracks, release_date)

            # Track latest timestamp for cursor
            if latest_ts is None or played_at > latest_ts:
                latest_ts = played_at

        if latest_ts:
            # Convert ISO timestamp to epoch ms for cursor
            try:
                dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
                epoch_ms = int(dt.timestamp() * 1000)
                tracker.set_state("last_poll_cursor", str(epoch_ms))
            except ValueError:
                pass

        log.info("Fetched %d plays, %d new", len(items), new_count)

    # 2.5. Sync playlist membership (at most once per 24hrs)
    try:
        _sync_playlists_if_needed(client, tracker)
    except Exception as e:
        log.warning("Playlist sync skipped: %s", e)

    # 3. Score (always run — pending downloads may exist from previous cycles)
    album_scores, track_scores, playlist_scores = run_scoring(
        tracker, cfg.thresholds, spotify_client=client,
    )

    albums_to_dl = [s for s in album_scores if s["decision"] == "download_album"]
    tracks_to_dl = [s for s in track_scores if s["decision"] == "download"]
    playlists_to_dl = [s for s in playlist_scores if s["decision"] == "download_playlist"]
    log.info(
        "Scores: %d albums, %d tracks, %d playlists eligible",
        len(albums_to_dl), len(tracks_to_dl), len(playlists_to_dl),
    )

    # 4. Download
    pending_albums = tracker.get_pending_album_downloads()
    pending_tracks = tracker.get_pending_track_downloads()
    pending_playlists = tracker.get_pending_playlist_downloads()

    if not pending_albums and not pending_tracks and not pending_playlists:
        log.info("Nothing new to download")
        return

    if dry_run:
        log.info("DRY RUN — would download:")
        for a in pending_albums:
            log.info(
                "  Album: %s - %s (coverage: %.0f%%)",
                a["artist_name"], a["album_name"],
                a["coverage"] * 100,
            )
        for t in pending_tracks:
            log.info(
                "  Track: %s - %s (%d plays)",
                t["artist_name"], t["track_name"], t["total_plays"],
            )
        for p in pending_playlists:
            log.info(
                "  Playlist: %s (%d unique tracks)",
                p.get("playlist_name") or p["playlist_id"],
                p["unique_tracks_seen"],
            )
        return

    for album in pending_albums:
        album_id = album["album_id"]
        log.info(
            "Downloading album: %s - %s",
            album["artist_name"], album["album_name"],
        )
        result = downloader.download_by_id(album_id, "album")
        if result["success"]:
            from .file_organizer import flatten_download, generate_album_json
            processed = flatten_download(cfg.staging_dir, cfg.tracks_dir, cfg.lyrics_dir, result)
            # Build album JSON from the processed files
            track_files = [
                {"track_number": i + 1, "track_name": p["track_name"],
                 "artist_name": p["artist"], "filename": p["filename"]}
                for i, p in enumerate(processed)
            ]
            if track_files:
                generate_album_json(
                    cfg.output_dir, album_id,
                    album["album_name"], album["artist_name"], track_files,
                )
            tracker.record_download(
                album_id, "album", album["album_name"], album["artist_name"],
            )
            log.info("  Album download complete (%d tracks)", len(processed))
        else:
            log.error("  Album download failed: %s", result.get("stderr", "")[:200])

    for track in pending_tracks:
        track_id = track["track_id"]
        track_name = track["track_name"] or ""
        artist_name = track["artist_name"] or ""

        # Belt-and-suspenders: skip if we already downloaded this name+artist
        if track_name and artist_name:
            already = tracker.conn.execute(
                "SELECT 1 FROM downloads WHERE type = 'track' "
                "AND LOWER(name) = LOWER(?) AND LOWER(artist) = LOWER(?)",
                (track_name, artist_name),
            ).fetchone()
            if already:
                log.info(
                    "Skipping duplicate track: %s - %s (already downloaded under different ID)",
                    artist_name, track_name,
                )
                continue

        # Skip if the flat file already exists on disk (e.g., from an album download)
        if track_name and artist_name:
            from .file_organizer import make_track_filename
            expected = os.path.join(
                cfg.tracks_dir, make_track_filename(artist_name, track_name)
            )
            if os.path.isfile(expected):
                log.info(
                    "Skipping track %s - %s (file already exists)",
                    artist_name, track_name,
                )
                tracker.record_download(
                    track_id, "track", track_name, artist_name,
                    file_path="tracks/" + make_track_filename(artist_name, track_name),
                )
                continue

        log.info(
            "Downloading track: %s - %s",
            artist_name, track_name,
        )
        result = downloader.download_by_id(track_id, "track")
        if result["success"]:
            from .file_organizer import flatten_download
            processed = flatten_download(cfg.staging_dir, cfg.tracks_dir, cfg.lyrics_dir, result)
            file_path = "tracks/" + processed[0]["filename"] if processed else ""
            tracker.record_download(
                track_id, "track", track["track_name"], track["artist_name"],
                file_path=file_path,
            )
            log.info("  Track download complete")
        else:
            log.error("  Track download failed: %s", result.get("stderr", "")[:200])

    for playlist in pending_playlists:
        playlist_id = playlist["playlist_id"]
        name = playlist.get("playlist_name") or playlist_id
        log.info("Downloading playlist: %s", name)
        result = downloader.download_by_id(playlist_id, "playlist")
        if result["success"]:
            from .file_organizer import flatten_download as flatten_dl
            flatten_dl(cfg.staging_dir, cfg.tracks_dir, cfg.lyrics_dir, result)
            tracker.record_download(playlist_id, "playlist", name, "")
            log.info("  Playlist download complete")
        else:
            log.error("  Playlist download failed: %s", result.get("stderr", "")[:200])

    # 5. Clean up staging directory
    from .file_organizer import _cleanup_empty_dirs
    try:
        _cleanup_empty_dirs(cfg.staging_dir)
    except Exception:
        pass

    # 6. Regenerate album JSONs for all downloaded albums
    from .file_organizer import generate_all_album_jsons
    try:
        generate_all_album_jsons(tracker, cfg.output_dir, spotify_client=client)
    except Exception as e:
        log.error("Album JSON generation failed: %s", e)

    # 7. Generate playlist JSON index
    from .file_organizer import generate_playlist_json
    try:
        generate_playlist_json(tracker, cfg.output_dir)
    except Exception as e:
        log.error("Playlist JSON generation failed: %s", e)

    # 8. Post-download hook
    if cfg.post_download_command:
        log.info("Running post-download command: %s", cfg.post_download_command)
        try:
            subprocess.run(cfg.post_download_command, shell=True, timeout=300)
        except Exception as e:
            log.error("Post-download command failed: %s", e)

    log.info("--- Poll cycle end ---")


def cmd_score(args):
    """Show current scores (dry run)."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    # Try to get a Spotify client for API-based dedup
    client = None
    try:
        auth = SpotifyAuth(
            cfg.spotify.client_id,
            cfg.spotify.client_secret,
            cfg.spotify.redirect_uri,
            cfg.token_path,
        )
        client = SpotifyClient(auth)
    except Exception:
        pass  # Score works without API, just less accurate dedup

    try:
        album_scores, track_scores, playlist_scores = run_scoring(
            tracker, cfg.thresholds, spotify_client=client,
        )
    finally:
        tracker.close()

    albums_dl = [s for s in album_scores if s["decision"] == "download_album"]
    tracks_dl = [s for s in track_scores if s["decision"] == "download"]
    playlists_dl = [s for s in playlist_scores if s["decision"] == "download_playlist"]

    if albums_dl:
        print(f"\nAlbums to download ({len(albums_dl)}):")
        print(f"{'Artist':<30} {'Album':<35} {'Cov%':>5} {'Tracks':>7} {'Plays':>6}")
        print("-" * 90)
        for s in sorted(albums_dl, key=lambda x: x["coverage"], reverse=True):
            print(
                f"{s['artist_name'][:29]:<30} {s['album_name'][:34]:<35} "
                f"{s['coverage']*100:>4.0f}% "
                f"{s['unique_tracks_played']}/{s['total_tracks']:>3} "
                f"{s['total_plays']:>6}"
            )
    else:
        print("\nNo albums qualify for download yet.")

    if tracks_dl:
        print(f"\nIndividual tracks to download ({len(tracks_dl)}):")
        print(f"{'Artist':<30} {'Track':<40} {'Plays':>6}")
        print("-" * 80)
        for s in sorted(tracks_dl, key=lambda x: x["total_plays"], reverse=True):
            print(
                f"{s['artist_name'][:29]:<30} {s['track_name'][:39]:<40} "
                f"{s['total_plays']:>6}"
            )
    else:
        print("\nNo individual tracks qualify for download yet.")

    if playlists_dl:
        print(f"\nPlaylists to download ({len(playlists_dl)}):")
        print(f"{'Playlist ID':<30} {'Tracks Seen':>12} {'Plays':>6}")
        print("-" * 55)
        for s in sorted(playlists_dl, key=lambda x: x["unique_tracks_seen"], reverse=True):
            name = s.get("playlist_name") or s["playlist_id"]
            print(
                f"{name[:29]:<30} {s['unique_tracks_seen']:>12} "
                f"{s['total_plays']:>6}"
            )

    # Show "almost there" albums
    almost = [
        s for s in album_scores
        if s["decision"] == "skip"
        and s["coverage"] >= 0.2
        and s["unique_tracks_played"] >= 1
    ]
    if almost:
        print(f"\nAlbums approaching threshold ({len(almost)}):")
        print(f"{'Artist':<30} {'Album':<30} {'Cov%':>5} {'Tracks':>7} {'Plays':>6}")
        print("-" * 85)
        for s in sorted(almost, key=lambda x: x["coverage"], reverse=True)[:15]:
            print(
                f"{s['artist_name'][:29]:<30} {s['album_name'][:29]:<30} "
                f"{s['coverage']*100:>4.0f}% "
                f"{s['unique_tracks_played']}/{s['total_tracks']:>3} "
                f"{s['total_plays']:>6}"
            )


def cmd_status(args):
    """Show database stats."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        stats = tracker.get_stats()
    finally:
        tracker.close()

    print(f"Database: {cfg.resolved_db_path}")
    print(f"Total plays tracked:  {stats['total_plays']}")
    print(f"Unique tracks:        {stats['unique_tracks']}")
    print(f"Unique albums:        {stats['unique_albums']}")
    print(f"Downloads completed:  {stats['total_downloads']}")
    print(f"Latest play:          {stats['latest_play'] or 'none'}")


def cmd_import(args):
    """Import Spotify streaming history export (extended or Account Data format)."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
            print(f"Filtering to plays after: {args.since}")
        except ValueError:
            print(f"Error: --since must be YYYY-MM-DD format, got '{args.since}'")
            sys.exit(1)

    # If --resolve is set, we need a Spotify client for both
    # Account Data track resolution and album ID resolution
    client = None
    if args.resolve:
        auth = SpotifyAuth(
            cfg.spotify.client_id,
            cfg.spotify.client_secret,
            cfg.spotify.redirect_uri,
            cfg.token_path,
        )
        client = SpotifyClient(auth)

    try:
        print(f"Importing from: {args.path}")
        result = import_export(
            tracker, args.path,
            min_play_ms=cfg.thresholds.min_play_seconds * 1000,
            since=since,
            spotify_client=client,
        )

        if result.get("error"):
            print(f"Error: {result['error']}")
            sys.exit(1)

        print(f"\nImport complete:")
        print(f"  Files processed: {result['files_found']}")
        print(f"  Total entries:   {result['total_entries']}")
        print(f"  New plays:       {result['inserted']}")
        print(f"  Duplicates:      {result['skipped']}")
        if result.get("unresolved"):
            print(f"  Unresolved:      {result['unresolved']} (no track ID found)")

        if args.resolve:
            print("\nResolving missing album IDs via Spotify API...")
            resolve_album_ids(tracker, client)

        print("\nRun 'flacjacket score' to see what would be downloaded.")
    finally:
        tracker.close()


def cmd_add(args):
    """Add a Spotify URL to the whitelist and download it immediately."""
    cfg = load_config(args.config)
    setup_logging(cfg.log_file)
    spotify_id, entity_type = parse_spotify_url(args.url)
    if not spotify_id:
        print(f"Error: Could not parse Spotify URL/URI: {args.url}")
        sys.exit(1)

    tracker = PlayTracker(cfg.resolved_db_path)
    downloader = Downloader(cfg.spotiflac_cli_path, cfg.output_dir, cfg.staging_dir)

    try:
        # Add to whitelist (idempotent)
        was_new = tracker.add_to_whitelist(spotify_id, entity_type)
        if was_new:
            print(f"Added to whitelist: {entity_type} {spotify_id}")
        else:
            print(f"Already in whitelist: {entity_type} {spotify_id}")

        # Check if already downloaded
        if tracker.is_downloaded(spotify_id):
            name, artist = tracker.lookup_name(spotify_id, entity_type)
            label = f"{artist} - {name}" if name else spotify_id
            print(f"Already downloaded: {label}")
            return

        # Download
        print(f"Downloading {entity_type}: {args.url}")
        result = downloader.download_by_id(spotify_id, entity_type)
        if not result["success"]:
            print(f"Download failed: {result.get('stderr', result.get('error', ''))[:300]}")
            sys.exit(1)

        # Flatten
        from .file_organizer import (
            flatten_download, generate_album_json, generate_all_album_jsons,
            generate_playlist_json, _cleanup_empty_dirs,
        )

        processed = flatten_download(cfg.staging_dir, cfg.tracks_dir, cfg.lyrics_dir, result)

        if entity_type == "album":
            # Build album JSON from processed files
            track_files = [
                {"track_number": i + 1, "track_name": p["track_name"],
                 "artist_name": p["artist"], "filename": p["filename"]}
                for i, p in enumerate(processed)
            ]
            album_name = processed[0]["album_name"] if processed else ""
            artist_name = processed[0]["artist"] if processed else ""
            if track_files:
                generate_album_json(
                    cfg.output_dir, spotify_id, album_name, artist_name, track_files,
                )
            tracker.record_download(spotify_id, "album", album_name, artist_name)
            tracker.update_whitelist_metadata(spotify_id, "album", album_name, artist_name)
            print(f"Album downloaded: {artist_name} - {album_name} ({len(processed)} tracks)")

        elif entity_type == "track":
            file_path = "tracks/" + processed[0]["filename"] if processed else ""
            track_name = processed[0]["track_name"] if processed else ""
            artist_name = processed[0]["artist"] if processed else ""
            tracker.record_download(
                spotify_id, "track", track_name, artist_name, file_path=file_path,
            )
            tracker.update_whitelist_metadata(spotify_id, "track", track_name, artist_name)
            print(f"Track downloaded: {artist_name} - {track_name}")

        elif entity_type == "playlist":
            tracker.record_download(spotify_id, "playlist", "", "")
            print(f"Playlist downloaded ({len(processed)} tracks)")

        # Clean up
        try:
            _cleanup_empty_dirs(cfg.staging_dir)
        except Exception:
            pass

    finally:
        tracker.close()


def cmd_download(args):
    """Force-download a specific Spotify URL (legacy, use 'add' instead)."""
    # Redirect to cmd_add for backwards compatibility
    cmd_add(args)


def cmd_unadd(args):
    """Remove a Spotify item from the whitelist."""
    cfg = load_config(args.config)
    spotify_id, entity_type = parse_spotify_url(args.url)
    if not spotify_id:
        print(f"Error: Could not parse Spotify URL/URI: {args.url}")
        sys.exit(1)

    tracker = PlayTracker(cfg.resolved_db_path)
    try:
        removed = tracker.remove_from_whitelist(spotify_id, entity_type)
        if removed:
            print(f"Removed from whitelist: {spotify_id} ({entity_type})")
            print("Note: downloaded files are kept. Use 'manage' to delete them.")
        else:
            print(f"Not found in whitelist: {spotify_id}")
    finally:
        tracker.close()


def cmd_whitelist(args):
    """Show all whitelisted items."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        items = tracker.get_whitelist()
        if not items:
            print("Whitelist is empty.")
            return

        print(f"{'Type':<9} {'Artist':<25} {'Name':<35} {'Added':<20}")
        print("-" * 92)
        for w in items:
            added = (w.get("added_at") or "")[:19].replace("T", " ")
            print(
                f"{w['type']:<9} {(w['artist'] or '')[:24]:<25} "
                f"{(w['name'] or w['spotify_id'])[:34]:<35} "
                f"{added:<20}"
            )
    finally:
        tracker.close()


def cmd_history(args):
    """Show recent play history from the database."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        limit = args.limit or 30
        rows = tracker.conn.execute(
            "SELECT * FROM plays ORDER BY played_at DESC LIMIT ?", (limit,)
        ).fetchall()

        if not rows:
            print("No plays recorded yet.")
            return

        print(f"{'Played At':<22} {'Artist':<25} {'Track':<35} {'Context':<10}")
        print("-" * 95)
        for r in rows:
            r = dict(r)
            played = r["played_at"][:19].replace("T", " ")
            ctx = r.get("context_type") or "-"
            print(
                f"{played:<22} {(r['artist_name'] or '')[:24]:<25} "
                f"{(r['track_name'] or '')[:34]:<35} {ctx:<10}"
            )
    finally:
        tracker.close()


def cmd_downloads(args):
    """Show download history."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        downloads = tracker.get_downloads()
        if not downloads:
            print("No downloads recorded yet.")
            return

        print(f"{'Date':<22} {'Type':<7} {'Artist':<25} {'Name':<35}")
        print("-" * 90)
        for d in downloads:
            date = d["downloaded_at"][:19].replace("T", " ")
            print(
                f"{date:<22} {d['type']:<7} {(d['artist'] or '')[:24]:<25} "
                f"{(d['name'] or '')[:34]:<35}"
            )
    finally:
        tracker.close()


def parse_spotify_url(url: str) -> tuple[str, str]:
    """Parse a Spotify URL or URI into (spotify_id, entity_type).

    Handles:
      - https://open.spotify.com/track/ABC123?si=...
      - spotify:track:ABC123
    Returns ("", "") if unparseable.
    """
    # URI format: spotify:track:ID
    uri_match = re.match(r"^spotify:(track|album|playlist):([A-Za-z0-9]+)$", url)
    if uri_match:
        return uri_match.group(2), uri_match.group(1)

    # URL format: https://open.spotify.com/track/ID?...
    url_match = re.match(
        r"^https?://open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)",
        url,
    )
    if url_match:
        return url_match.group(2), url_match.group(1)

    return "", ""


def cmd_skip(args):
    """Add a Spotify item to the blacklist (skip future downloads)."""
    cfg = load_config(args.config)
    spotify_id, entity_type = parse_spotify_url(args.url)
    if not spotify_id:
        print(f"Error: Could not parse Spotify URL/URI: {args.url}")
        sys.exit(1)

    tracker = PlayTracker(cfg.resolved_db_path)
    try:
        name, artist = tracker.lookup_name(spotify_id, entity_type)
        reason = args.reason or ""
        added = tracker.add_to_blacklist(spotify_id, entity_type, name, artist, reason)
        if added:
            label = f"{artist} - {name}" if name else spotify_id
            print(f"Blacklisted {entity_type}: {label}")
        else:
            print(f"Already blacklisted: {spotify_id}")
    finally:
        tracker.close()


def cmd_unskip(args):
    """Remove a Spotify item from the blacklist."""
    cfg = load_config(args.config)
    spotify_id, entity_type = parse_spotify_url(args.url)
    if not spotify_id:
        print(f"Error: Could not parse Spotify URL/URI: {args.url}")
        sys.exit(1)

    tracker = PlayTracker(cfg.resolved_db_path)
    try:
        removed = tracker.remove_from_blacklist(spotify_id, entity_type)
        if removed:
            print(f"Removed from blacklist: {spotify_id} ({entity_type})")
        else:
            print(f"Not found in blacklist: {spotify_id}")
    finally:
        tracker.close()


def cmd_blacklist(args):
    """Show all blacklisted items."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        items = tracker.get_blacklist()
        if not items:
            print("Blacklist is empty.")
            return

        print(f"{'Type':<9} {'Artist':<25} {'Name':<35} {'Reason':<20}")
        print("-" * 92)
        for b in items:
            print(
                f"{b['type']:<9} {(b['artist'] or '')[:24]:<25} "
                f"{(b['name'] or b['spotify_id'])[:34]:<35} "
                f"{(b['reason'] or '')[:19]:<20}"
            )
    finally:
        tracker.close()


def cmd_manage(args):
    """Interactive download manager — review, delete, blacklist."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        downloads = tracker.get_downloads()
        if not downloads:
            print("No downloads recorded.")
            return

        # Display numbered list with disk status
        entries = []
        for i, d in enumerate(downloads, 1):
            file_path = d.get("file_path") or ""
            if d["type"] == "album":
                # Albums live under output_dir/Artist/Album/
                exists = "?"  # Can't easily check without scanning
            elif file_path:
                full = os.path.join(cfg.output_dir, file_path) if not os.path.isabs(file_path) else file_path
                exists = "Y" if os.path.exists(full) else "N"
            else:
                exists = "?"
            entries.append({**d, "_num": i, "_exists": exists, "_file": file_path})

        print(f"\n{'#':<4} {'Type':<7} {'Disk':<5} {'Artist':<25} {'Name':<35}")
        print("-" * 80)
        for e in entries:
            print(
                f"{e['_num']:<4} {e['type']:<7} {e['_exists']:<5} "
                f"{(e['artist'] or '')[:24]:<25} "
                f"{(e['name'] or '')[:34]:<35}"
            )

        print(f"\nTotal: {len(entries)} downloads")
        print("Select items (e.g. 1-5,7,9) then action: [d]elete, [b]lacklist, [db] both, [q]uit")

        while True:
            try:
                sel = input("\nSelect> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not sel or sel.lower() == "q":
                break

            # Parse selection
            selected = _parse_selection(sel, len(entries))
            if not selected:
                print("Invalid selection. Use numbers like 1-5,7,9")
                continue

            try:
                action = input("Action [d/b/db/q]> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if action == "q":
                break

            for idx in selected:
                e = entries[idx - 1]
                label = f"{e['artist']} - {e['name']}" if e['name'] else e['spotify_id']

                if action in ("d", "db"):
                    _delete_download_files(cfg.output_dir, e)
                    tracker.remove_download(e["spotify_id"], e["type"])
                    print(f"  Deleted: {label}")

                if action in ("b", "db"):
                    tracker.add_to_blacklist(
                        e["spotify_id"], e["type"],
                        e.get("name", ""), e.get("artist", ""),
                        reason="manage",
                    )
                    print(f"  Blacklisted: {label}")

    finally:
        tracker.close()


def _parse_selection(text: str, max_num: int) -> list[int]:
    """Parse a selection string like '1-5,7,9' into sorted list of ints."""
    result = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
                result.update(range(start, end + 1))
            except ValueError:
                return []
        else:
            try:
                result.add(int(part))
            except ValueError:
                return []
    # Validate range
    valid = sorted(n for n in result if 1 <= n <= max_num)
    return valid if len(valid) == len(result) else []


def _delete_download_files(output_dir: str, download: dict):
    """Best-effort delete files for a download entry."""
    from .file_organizer import make_track_filename, make_lyrics_filename, sanitize_filename

    dl_type = download["type"]
    name = download.get("name", "")
    artist = download.get("artist", "")
    tracks_dir = os.path.join(output_dir, "tracks")
    lyrics_dir = os.path.join(output_dir, "lyrics")

    if dl_type == "track" and name and artist:
        fname = make_track_filename(artist, name)
        flac_path = os.path.join(tracks_dir, fname)
        if os.path.isfile(flac_path):
            os.remove(flac_path)
        lrc_name = make_lyrics_filename(artist, name)
        lrc_path = os.path.join(lyrics_dir, lrc_name)
        if os.path.isfile(lrc_path):
            os.remove(lrc_path)
    elif dl_type == "album" and name and artist:
        # Remove album JSON
        albums_dir = os.path.join(output_dir, ".albums")
        safe_name = sanitize_filename(f"{artist} - {name}")
        json_path = os.path.join(albums_dir, f"{safe_name}.json")
        if os.path.isfile(json_path):
            try:
                import json as _json
                with open(json_path) as f:
                    album_data = _json.load(f)
                for t in album_data.get("tracks", []):
                    fname = t.get("filename", "")
                    if fname:
                        flac_path = os.path.join(tracks_dir, fname)
                        if os.path.isfile(flac_path):
                            os.remove(flac_path)
                        lrc_name = os.path.splitext(fname)[0] + ".lrc"
                        lrc_path = os.path.join(lyrics_dir, lrc_name)
                        if os.path.isfile(lrc_path):
                            os.remove(lrc_path)
            except Exception:
                pass
            os.remove(json_path)


def cmd_reset(args):
    """Clear all download records so the system re-downloads everything."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        count = tracker.conn.execute("SELECT COUNT(*) as n FROM downloads").fetchone()["n"]
        if count == 0:
            print("No download records to clear.")
            return

        if not args.yes:
            print(f"This will clear {count} download records from the database.")
            if args.delete_files:
                print("Files on disk WILL also be deleted.")
            else:
                print("Files on disk will NOT be deleted (use --delete-files for that).")
            try:
                confirm = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
            if confirm != "y":
                print("Aborted.")
                return

        if args.delete_files:
            downloads = tracker.get_downloads()
            for d in downloads:
                _delete_download_files(cfg.output_dir, d)
            print(f"Deleted files for {len(downloads)} downloads.")

        removed = tracker.clear_downloads()
        print(f"Cleared {removed} download records. Next poll will re-download everything.")
    finally:
        tracker.close()


def cmd_sync_playlists(args):
    """Fetch user's Spotify playlists and sync track membership to DB."""
    cfg = load_config(args.config)
    setup_logging(cfg.log_file)

    auth = SpotifyAuth(
        cfg.spotify.client_id,
        cfg.spotify.client_secret,
        cfg.spotify.redirect_uri,
        cfg.token_path,
    )
    client = SpotifyClient(auth)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        _sync_playlists(client, tracker, verbose=True)
    finally:
        tracker.close()


def _parse_playlist_full(client, playlist_id: str) -> list[dict]:
    """Fallback: fetch full playlist object and extract tracks from it.
    The /playlists/{id} endpoint sometimes works when /playlists/{id}/tracks returns 403."""
    data = client.get_playlist_full(playlist_id)
    tracks_obj = data.get("tracks", {})
    items = tracks_obj.get("items", [])
    tracks = []
    for item in items:
        track = item.get("track")
        if not track or not track.get("id"):
            continue
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        tracks.append({
            "track_id": track["id"],
            "track_name": track.get("name", ""),
            "artist_name": artists,
            "added_at": item.get("added_at", ""),
        })
    # Note: only gets first ~100 tracks; full playlist endpoint doesn't paginate
    # but for most user playlists this is enough, and it avoids the 403
    return tracks


def _sync_playlists(client, tracker, verbose=False):
    """Fetch all user playlists and store track membership in DB."""
    if verbose:
        print("Fetching your Spotify playlists...")

    playlists = []
    offset = 0
    while True:
        data = client.get_user_playlists(limit=50, offset=offset)
        items = data.get("items", [])
        if not items:
            break
        playlists.extend(items)
        offset += len(items)
        if offset >= data.get("total", 0):
            break

    if verbose:
        print(f"Found {len(playlists)} playlists")

    total_tracks = 0
    for pl in playlists:
        pl_id = pl["id"]
        pl_name = pl.get("name", pl_id)
        if verbose:
            print(f"  Syncing: {pl_name}...", end="", flush=True)

        tracks = []
        offset = 0
        try:
            while True:
                data = client.get_playlist_tracks(pl_id, limit=100, offset=offset)
                items = data.get("items", [])
                if not items:
                    break
                for item in items:
                    track = item.get("track")
                    if not track or not track.get("id"):
                        continue
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    tracks.append({
                        "track_id": track["id"],
                        "track_name": track.get("name", ""),
                        "artist_name": artists,
                        "added_at": item.get("added_at", ""),
                    })
                offset += len(items)
                if data.get("next") is None:
                    break
        except Exception as e:
            # Fallback: try fetching full playlist object (includes first 100 tracks)
            if "403" in str(e):
                if verbose:
                    print(f" tracks endpoint 403, trying full playlist endpoint...", end="", flush=True)
                try:
                    tracks = _parse_playlist_full(client, pl_id)
                except Exception as e2:
                    if verbose:
                        print(f" FAILED: {e2}")
                    log.warning("Failed to fetch tracks for playlist %s via both methods: %s / %s", pl_name, e, e2)
                    continue
            else:
                if verbose:
                    print(f" FAILED: {e}")
                log.warning("Failed to fetch tracks for playlist %s: %s", pl_name, e)
                continue

        tracker.upsert_playlist_tracks_bulk(pl_id, pl_name, tracks)
        total_tracks += len(tracks)
        if verbose:
            print(f" {len(tracks)} tracks")

        time.sleep(0.1)

    if verbose:
        print(f"\nSync complete: {len(playlists)} playlists, {total_tracks} total track entries")
        print("Run 'flacjacket score' to see updated playlist scoring.")

    log.info("Playlist sync complete: %d playlists, %d tracks", len(playlists), total_tracks)


def cmd_organize(args):
    """Regenerate album and playlist metadata JSONs."""
    cfg = load_config(args.config)
    setup_logging(cfg.log_file)
    tracker = PlayTracker(cfg.resolved_db_path)

    # Try to get Spotify client for album tracklists
    client = None
    try:
        auth = SpotifyAuth(
            cfg.spotify.client_id, cfg.spotify.client_secret,
            cfg.spotify.redirect_uri, cfg.token_path,
        )
        client = SpotifyClient(auth)
    except Exception:
        pass

    try:
        from .file_organizer import generate_all_album_jsons, generate_playlist_json

        print("Regenerating album JSONs...")
        generate_all_album_jsons(tracker, cfg.output_dir, spotify_client=client)

        print("Regenerating playlist JSONs...")
        generate_playlist_json(tracker, cfg.output_dir)

        print("Done.")
    finally:
        tracker.close()


def cmd_migrate(args):
    """One-time migration from old directory structure to flat tracks/ layout."""
    cfg = load_config(args.config)
    setup_logging(cfg.log_file)
    tracker = PlayTracker(cfg.resolved_db_path)

    # Try to get Spotify client for album JSON generation
    client = None
    try:
        auth = SpotifyAuth(
            cfg.spotify.client_id, cfg.spotify.client_secret,
            cfg.spotify.redirect_uri, cfg.token_path,
        )
        client = SpotifyClient(auth)
    except Exception:
        pass

    try:
        from .file_organizer import (
            migrate_existing_files, generate_all_album_jsons, generate_playlist_json,
        )

        print(f"Migrating files in {cfg.output_dir} to flat structure...")
        stats = migrate_existing_files(cfg.output_dir, tracker)
        print(f"\nMigration complete:")
        print(f"  Files moved:      {stats['moved']}")
        print(f"  Duplicates found: {stats['duplicates']}")
        print(f"  Lyrics moved:     {stats['lyrics_moved']}")

        print("\nGenerating album JSONs...")
        generate_all_album_jsons(tracker, cfg.output_dir, spotify_client=client)

        print("Regenerating playlist JSONs...")
        generate_playlist_json(tracker, cfg.output_dir)

        print("Done.")
    finally:
        tracker.close()


def _sync_playlists_if_needed(client, tracker):
    """Sync playlists at most once per day during poll cycles."""
    last_sync = tracker.get_state("last_playlist_sync")
    if last_sync:
        from datetime import timedelta
        try:
            last_dt = datetime.fromisoformat(last_sync)
            now = datetime.now(timezone.utc)
            if now - last_dt < timedelta(hours=24):
                return
        except ValueError:
            pass

    _sync_playlists(client, tracker, verbose=False)
    tracker.set_state("last_playlist_sync", datetime.now(timezone.utc).isoformat())


def main():
    parser = argparse.ArgumentParser(
        prog="flacjacket",
        description="Automatic Spotify-to-FLAC download pipeline",
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to config.json",
    )

    sub = parser.add_subparsers(dest="command")

    # auth
    sub.add_parser("auth", help="Authenticate with Spotify")

    # poll
    poll_p = sub.add_parser("poll", help="Poll Spotify, score, and download")
    poll_p.add_argument("--loop", action="store_true", help="Run continuously")
    poll_p.add_argument("--dry-run", action="store_true", help="Don't actually download")

    # score
    sub.add_parser("score", help="Show current scores and download candidates")

    # status
    sub.add_parser("status", help="Show database statistics")

    # import
    import_p = sub.add_parser("import", help="Import Spotify export data")
    import_p.add_argument("path", help="Directory containing Spotify export JSON files")
    import_p.add_argument(
        "--resolve", action="store_true",
        help="Resolve track/album IDs via Spotify API (required for Account Data format)",
    )
    import_p.add_argument(
        "--since", type=str, metavar="YYYY-MM-DD",
        help="Only import plays after this date",
    )

    # download (legacy, redirects to add)
    dl_p = sub.add_parser("download", help="Force-download a specific Spotify URL")
    dl_p.add_argument("url", help="Spotify track/album/playlist URL")

    # add (whitelist + download)
    add_p = sub.add_parser("add", help="Add to whitelist and download immediately")
    add_p.add_argument("url", help="Spotify track/album/playlist URL")

    # unadd (remove from whitelist)
    unadd_p = sub.add_parser("unadd", help="Remove item from whitelist")
    unadd_p.add_argument("url", help="Spotify URL or URI")

    # whitelist
    sub.add_parser("whitelist", help="Show all whitelisted items")

    # history
    hist_p = sub.add_parser("history", help="Show recent play history")
    hist_p.add_argument("-n", "--limit", type=int, help="Number of entries (default 30)")

    # downloads
    sub.add_parser("downloads", help="Show download history")

    # skip (blacklist)
    skip_p = sub.add_parser("skip", help="Blacklist a Spotify item (skip future downloads)")
    skip_p.add_argument("url", help="Spotify URL or URI")
    skip_p.add_argument("--reason", type=str, default="", help="Reason for skipping")

    # unskip
    unskip_p = sub.add_parser("unskip", help="Remove item from blacklist")
    unskip_p.add_argument("url", help="Spotify URL or URI")

    # blacklist
    sub.add_parser("blacklist", help="Show all blacklisted items")

    # manage
    sub.add_parser("manage", help="Interactive download manager (review, delete, blacklist)")

    # reset
    reset_p = sub.add_parser("reset", help="Clear download records (re-download on next poll)")
    reset_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    reset_p.add_argument(
        "--delete-files", action="store_true",
        help="Also delete downloaded files from disk",
    )

    # sync-playlists
    sub.add_parser("sync-playlists", help="Fetch Spotify playlists and sync track membership")

    # organize
    org_p = sub.add_parser("organize", help="Regenerate album and playlist metadata JSONs")
    org_p.add_argument("--dry-run", action="store_true", help="Show what would change")

    # migrate
    sub.add_parser("migrate", help="One-time migration to flat tracks/ file structure")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "auth": cmd_auth,
        "poll": cmd_poll,
        "score": cmd_score,
        "status": cmd_status,
        "import": cmd_import,
        "download": cmd_download,
        "add": cmd_add,
        "unadd": cmd_unadd,
        "whitelist": cmd_whitelist,
        "history": cmd_history,
        "downloads": cmd_downloads,
        "skip": cmd_skip,
        "unskip": cmd_unskip,
        "blacklist": cmd_blacklist,
        "manage": cmd_manage,
        "reset": cmd_reset,
        "sync-playlists": cmd_sync_playlists,
        "organize": cmd_organize,
        "migrate": cmd_migrate,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
