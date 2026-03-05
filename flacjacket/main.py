"""FlacJacket CLI — automatic Spotify-to-FLAC download pipeline."""

import argparse
import logging
import os
import subprocess
import sys
import time
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
    downloader = Downloader(cfg.spotiflac_cli_path, cfg.output_dir)

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

    # 3. Score (always run — pending downloads may exist from previous cycles)
    album_scores, track_scores, playlist_scores = run_scoring(tracker, cfg.thresholds)

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
            tracker.record_download(
                album_id, "album", album["album_name"], album["artist_name"],
            )
            log.info("  Album download complete")
        else:
            log.error("  Album download failed: %s", result.get("stderr", "")[:200])

    for track in pending_tracks:
        track_id = track["track_id"]
        log.info(
            "Downloading track: %s - %s",
            track["artist_name"], track["track_name"],
        )
        result = downloader.download_by_id(track_id, "track")
        if result["success"]:
            tracker.record_download(
                track_id, "track", track["track_name"], track["artist_name"],
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
            tracker.record_download(playlist_id, "playlist", name, "")
            log.info("  Playlist download complete")
        else:
            log.error("  Playlist download failed: %s", result.get("stderr", "")[:200])

    # 5. Post-download hook
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

    try:
        album_scores, track_scores, playlist_scores = run_scoring(tracker, cfg.thresholds)
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
    """Import Spotify extended streaming history export."""
    cfg = load_config(args.config)
    tracker = PlayTracker(cfg.resolved_db_path)

    try:
        print(f"Importing from: {args.path}")
        result = import_export(
            tracker, args.path,
            min_play_ms=cfg.thresholds.min_play_seconds * 1000,
        )

        if result.get("error"):
            print(f"Error: {result['error']}")
            sys.exit(1)

        print(f"\nImport complete:")
        print(f"  Files processed: {result['files_found']}")
        print(f"  Total entries:   {result['total_entries']}")
        print(f"  New plays:       {result['inserted']}")
        print(f"  Duplicates:      {result['skipped']}")

        if args.resolve:
            print("\nResolving album IDs via Spotify API...")
            auth = SpotifyAuth(
                cfg.spotify.client_id,
                cfg.spotify.client_secret,
                cfg.spotify.redirect_uri,
                cfg.token_path,
            )
            client = SpotifyClient(auth)
            resolve_album_ids(tracker, client)

        print("\nRun 'flacjacket score' to see what would be downloaded.")
    finally:
        tracker.close()


def cmd_download(args):
    """Force-download a specific Spotify URL."""
    cfg = load_config(args.config)
    downloader = Downloader(cfg.spotiflac_cli_path, cfg.output_dir)

    print(f"Downloading: {args.url}")
    result = downloader._run(args.url)

    if result["success"]:
        print("Download complete!")
        if result.get("stdout"):
            print(result["stdout"])
    else:
        print(f"Download failed (exit code {result.get('returncode')})")
        if result.get("stderr"):
            print(result["stderr"])
        sys.exit(1)


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
        help="Resolve album IDs via Spotify API after import",
    )

    # download
    dl_p = sub.add_parser("download", help="Force-download a specific Spotify URL")
    dl_p.add_argument("url", help="Spotify track/album/playlist URL")

    # history
    hist_p = sub.add_parser("history", help="Show recent play history")
    hist_p.add_argument("-n", "--limit", type=int, help="Number of entries (default 30)")

    # downloads
    sub.add_parser("downloads", help="Show download history")

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
        "history": cmd_history,
        "downloads": cmd_downloads,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
