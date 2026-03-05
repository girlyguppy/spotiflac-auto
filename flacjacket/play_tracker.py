"""SQLite-based play history tracking."""

import sqlite3
from datetime import datetime
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    track_id TEXT NOT NULL,
    track_name TEXT,
    artist_name TEXT,
    album_id TEXT,
    album_name TEXT,
    album_total_tracks INTEGER,
    context_type TEXT,
    context_uri TEXT,
    played_at TEXT NOT NULL UNIQUE,
    duration_ms INTEGER DEFAULT 0,
    source TEXT DEFAULT 'api'
);

CREATE TABLE IF NOT EXISTS album_cache (
    album_id TEXT PRIMARY KEY,
    album_name TEXT,
    artist_name TEXT,
    total_tracks INTEGER,
    release_date TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS album_scores (
    album_id TEXT PRIMARY KEY,
    album_name TEXT,
    artist_name TEXT,
    total_tracks INTEGER,
    unique_tracks_played INTEGER,
    total_plays INTEGER,
    album_context_plays INTEGER,
    coverage REAL,
    score REAL,
    first_played TEXT,
    last_played TEXT,
    decision TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS track_scores (
    track_id TEXT PRIMARY KEY,
    track_name TEXT,
    artist_name TEXT,
    album_id TEXT,
    total_plays INTEGER,
    playlist_plays INTEGER,
    score REAL,
    first_played TEXT,
    last_played TEXT,
    decision TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    spotify_id TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT,
    artist TEXT,
    file_path TEXT,
    downloaded_at TEXT,
    source_service TEXT
);

CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_plays_track_id ON plays(track_id);
CREATE INDEX IF NOT EXISTS idx_plays_album_id ON plays(album_id);
CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays(played_at);
CREATE INDEX IF NOT EXISTS idx_plays_context ON plays(context_type, context_uri);
CREATE INDEX IF NOT EXISTS idx_downloads_spotify_id ON downloads(spotify_id);

CREATE TABLE IF NOT EXISTS playlist_scores (
    playlist_id TEXT PRIMARY KEY,
    playlist_name TEXT,
    unique_tracks_seen INTEGER,
    total_plays INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    decision TEXT,
    updated_at TEXT
);
"""


class PlayTracker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # -- Play insertion --

    def insert_play(
        self,
        track_id: str,
        track_name: str,
        artist_name: str,
        album_id: str | None,
        album_name: str | None,
        album_total_tracks: int | None,
        context_type: str | None,
        context_uri: str | None,
        played_at: str,
        duration_ms: int = 0,
        source: str = "api",
    ) -> bool:
        """Insert a play record. Returns True if inserted, False if duplicate."""
        try:
            self.conn.execute(
                """INSERT INTO plays
                   (track_id, track_name, artist_name, album_id, album_name,
                    album_total_tracks, context_type, context_uri, played_at,
                    duration_ms, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track_id, track_name, artist_name, album_id, album_name,
                    album_total_tracks, context_type, context_uri, played_at,
                    duration_ms, source,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def insert_plays_bulk(self, plays: list[dict]) -> int:
        """Insert multiple plays. Returns count of newly inserted."""
        inserted = 0
        for play in plays:
            if self.insert_play(**play):
                inserted += 1
        return inserted

    # -- Album cache --

    def get_cached_album(self, album_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM album_cache WHERE album_id = ?", (album_id,)
        ).fetchone()
        return dict(row) if row else None

    def cache_album(self, album_id: str, album_name: str, artist_name: str,
                    total_tracks: int, release_date: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO album_cache
               (album_id, album_name, artist_name, total_tracks, release_date, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (album_id, album_name, artist_name, total_tracks, release_date,
             datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    # -- Scoring queries --

    def get_known_artists(self) -> set[str]:
        """Artists that have at least one play with a non-null context
        (i.e., intentional listening, not autoplay)."""
        rows = self.conn.execute("""
            SELECT DISTINCT artist_name FROM plays
            WHERE context_type IS NOT NULL AND artist_name IS NOT NULL
            UNION
            SELECT DISTINCT artist_name FROM plays
            WHERE source = 'import' AND artist_name IS NOT NULL
        """).fetchall()
        return {r["artist_name"] for r in rows}

    def get_album_play_stats(self, known_artists: set[str] | None = None) -> list[dict]:
        """Get aggregated play stats per album for scoring.
        If known_artists is provided, exclude plays where context_type is NULL
        and the artist is not in the known set (autoplay filtering)."""
        if known_artists is not None:
            self.conn.execute("DROP TABLE IF EXISTS _tmp_known_artists")
            self.conn.execute("CREATE TEMP TABLE _tmp_known_artists (name TEXT)")
            if known_artists:
                self.conn.executemany(
                    "INSERT INTO _tmp_known_artists VALUES (?)",
                    [(a,) for a in known_artists],
                )

            rows = self.conn.execute("""
                SELECT
                    p.album_id,
                    COALESCE(c.album_name, p.album_name) AS album_name,
                    COALESCE(c.artist_name, p.artist_name) AS artist_name,
                    COALESCE(c.total_tracks, p.album_total_tracks, 0) AS total_tracks,
                    COUNT(DISTINCT p.track_id) AS unique_tracks_played,
                    COUNT(*) AS total_plays,
                    SUM(CASE WHEN p.context_type = 'album'
                             AND p.context_uri LIKE '%' || p.album_id || '%'
                        THEN 1 ELSE 0 END) AS album_context_plays,
                    MIN(p.played_at) AS first_played,
                    MAX(p.played_at) AS last_played
                FROM plays p
                LEFT JOIN album_cache c ON p.album_id = c.album_id
                WHERE p.album_id IS NOT NULL AND p.album_id != ''
                AND (
                    p.context_type IS NOT NULL
                    OR p.source = 'import'
                    OR p.artist_name IN (SELECT name FROM _tmp_known_artists)
                )
                GROUP BY p.album_id
            """).fetchall()

            self.conn.execute("DROP TABLE IF EXISTS _tmp_known_artists")
        else:
            rows = self.conn.execute("""
                SELECT
                    p.album_id,
                    COALESCE(c.album_name, p.album_name) AS album_name,
                    COALESCE(c.artist_name, p.artist_name) AS artist_name,
                    COALESCE(c.total_tracks, p.album_total_tracks, 0) AS total_tracks,
                    COUNT(DISTINCT p.track_id) AS unique_tracks_played,
                    COUNT(*) AS total_plays,
                    SUM(CASE WHEN p.context_type = 'album'
                             AND p.context_uri LIKE '%' || p.album_id || '%'
                        THEN 1 ELSE 0 END) AS album_context_plays,
                    MIN(p.played_at) AS first_played,
                    MAX(p.played_at) AS last_played
                FROM plays p
                LEFT JOIN album_cache c ON p.album_id = c.album_id
                WHERE p.album_id IS NOT NULL AND p.album_id != ''
                GROUP BY p.album_id
            """).fetchall()
        return [dict(r) for r in rows]

    def get_track_play_stats(self, known_artists: set[str] | None = None) -> list[dict]:
        """Get aggregated play stats per track for scoring.
        Same autoplay filtering as get_album_play_stats."""
        if known_artists is not None:
            self.conn.execute("DROP TABLE IF EXISTS _tmp_known_artists")
            self.conn.execute("CREATE TEMP TABLE _tmp_known_artists (name TEXT)")
            if known_artists:
                self.conn.executemany(
                    "INSERT INTO _tmp_known_artists VALUES (?)",
                    [(a,) for a in known_artists],
                )

            rows = self.conn.execute("""
                SELECT
                    p.track_id,
                    p.track_name,
                    p.artist_name,
                    p.album_id,
                    COUNT(*) AS total_plays,
                    SUM(CASE WHEN p.context_type = 'playlist' THEN 1 ELSE 0 END) AS playlist_plays,
                    MIN(p.played_at) AS first_played,
                    MAX(p.played_at) AS last_played
                FROM plays p
                WHERE (
                    p.context_type IS NOT NULL
                    OR p.source = 'import'
                    OR p.artist_name IN (SELECT name FROM _tmp_known_artists)
                )
                GROUP BY p.track_id
            """).fetchall()

            self.conn.execute("DROP TABLE IF EXISTS _tmp_known_artists")
        else:
            rows = self.conn.execute("""
                SELECT
                    p.track_id,
                    p.track_name,
                    p.artist_name,
                    p.album_id,
                    COUNT(*) AS total_plays,
                    SUM(CASE WHEN p.context_type = 'playlist' THEN 1 ELSE 0 END) AS playlist_plays,
                    MIN(p.played_at) AS first_played,
                    MAX(p.played_at) AS last_played
                FROM plays p
                GROUP BY p.track_id
            """).fetchall()
        return [dict(r) for r in rows]

    # -- Score storage --

    def save_album_scores(self, scores: list[dict]):
        now = datetime.utcnow().isoformat()
        self.conn.executemany(
            """INSERT OR REPLACE INTO album_scores
               (album_id, album_name, artist_name, total_tracks,
                unique_tracks_played, total_plays, album_context_plays,
                coverage, score, first_played, last_played, decision, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    s["album_id"], s["album_name"], s["artist_name"],
                    s["total_tracks"], s["unique_tracks_played"], s["total_plays"],
                    s["album_context_plays"], s["coverage"], s["score"],
                    s["first_played"], s["last_played"], s["decision"], now,
                )
                for s in scores
            ],
        )
        self.conn.commit()

    def save_track_scores(self, scores: list[dict]):
        now = datetime.utcnow().isoformat()
        self.conn.executemany(
            """INSERT OR REPLACE INTO track_scores
               (track_id, track_name, artist_name, album_id,
                total_plays, playlist_plays, score, first_played, last_played,
                decision, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    s["track_id"], s["track_name"], s["artist_name"],
                    s["album_id"], s["total_plays"], s["playlist_plays"],
                    s["score"], s["first_played"], s["last_played"],
                    s["decision"], now,
                )
                for s in scores
            ],
        )
        self.conn.commit()

    def get_playlist_play_stats(self) -> list[dict]:
        """Get aggregated play stats per playlist from context_uri."""
        rows = self.conn.execute("""
            SELECT
                REPLACE(REPLACE(p.context_uri, 'spotify:playlist:', ''),
                        'spotify:user:', '') AS playlist_id,
                COUNT(DISTINCT p.track_id) AS unique_tracks_seen,
                COUNT(*) AS total_plays,
                MIN(p.played_at) AS first_seen,
                MAX(p.played_at) AS last_seen
            FROM plays p
            WHERE p.context_type = 'playlist'
            AND p.context_uri IS NOT NULL
            AND p.context_uri LIKE 'spotify:playlist:%'
            GROUP BY p.context_uri
        """).fetchall()
        return [dict(r) for r in rows]

    def save_playlist_scores(self, scores: list[dict]):
        now = datetime.utcnow().isoformat()
        self.conn.executemany(
            """INSERT OR REPLACE INTO playlist_scores
               (playlist_id, playlist_name, unique_tracks_seen, total_plays,
                first_seen, last_seen, decision, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    s["playlist_id"], s.get("playlist_name", ""),
                    s["unique_tracks_seen"], s["total_plays"],
                    s["first_seen"], s["last_seen"], s["decision"], now,
                )
                for s in scores
            ],
        )
        self.conn.commit()

    def get_pending_playlist_downloads(self) -> list[dict]:
        """Playlists scored for download that haven't been downloaded yet."""
        rows = self.conn.execute("""
            SELECT s.* FROM playlist_scores s
            WHERE s.decision = 'download_playlist'
            AND s.playlist_id NOT IN (
                SELECT spotify_id FROM downloads WHERE type = 'playlist'
            )
            ORDER BY s.unique_tracks_seen DESC
        """).fetchall()
        return [dict(r) for r in rows]

    # -- Download tracking --

    def is_downloaded(self, spotify_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM downloads WHERE spotify_id = ?", (spotify_id,)
        ).fetchone()
        return row is not None

    def record_download(self, spotify_id: str, dl_type: str, name: str,
                        artist: str, file_path: str = "", source_service: str = ""):
        self.conn.execute(
            """INSERT INTO downloads
               (spotify_id, type, name, artist, file_path, downloaded_at, source_service)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (spotify_id, dl_type, name, artist, file_path,
             datetime.utcnow().isoformat(), source_service),
        )
        self.conn.commit()

    def get_downloads(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM downloads ORDER BY downloaded_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Poll state --

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM poll_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO poll_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    # -- Decisions that need action --

    def get_pending_album_downloads(self) -> list[dict]:
        """Albums scored for download that haven't been downloaded yet."""
        rows = self.conn.execute("""
            SELECT s.* FROM album_scores s
            WHERE s.decision = 'download_album'
            AND s.album_id NOT IN (SELECT spotify_id FROM downloads WHERE type = 'album')
            ORDER BY s.score DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def get_pending_track_downloads(self) -> list[dict]:
        """Tracks scored for download that haven't been downloaded yet,
        excluding tracks whose album is already downloaded."""
        rows = self.conn.execute("""
            SELECT s.* FROM track_scores s
            WHERE s.decision = 'download'
            AND s.track_id NOT IN (SELECT spotify_id FROM downloads WHERE type = 'track')
            AND (s.album_id IS NULL OR s.album_id NOT IN (
                SELECT spotify_id FROM downloads WHERE type = 'album'
            ))
            ORDER BY s.score DESC
        """).fetchall()
        return [dict(r) for r in rows]

    # -- Stats --

    def get_stats(self) -> dict:
        plays = self.conn.execute("SELECT COUNT(*) as n FROM plays").fetchone()["n"]
        tracks = self.conn.execute(
            "SELECT COUNT(DISTINCT track_id) as n FROM plays"
        ).fetchone()["n"]
        albums = self.conn.execute(
            "SELECT COUNT(DISTINCT album_id) as n FROM plays WHERE album_id IS NOT NULL"
        ).fetchone()["n"]
        downloads = self.conn.execute(
            "SELECT COUNT(*) as n FROM downloads"
        ).fetchone()["n"]
        latest = self.conn.execute(
            "SELECT MAX(played_at) as t FROM plays"
        ).fetchone()["t"]

        return {
            "total_plays": plays,
            "unique_tracks": tracks,
            "unique_albums": albums,
            "total_downloads": downloads,
            "latest_play": latest,
        }
