import json
import os
from pathlib import Path
from dataclasses import dataclass, field

DEFAULT_CONFIG = {
    "spotify": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
    },
    "output_dir": "",
    "spotiflac_cli_path": "spotiflac-cli",
    "poll_interval_minutes": 30,
    "thresholds": {
        "album_coverage": 0.5,
        "album_min_unique_tracks": 2,
        "track_plays": 3,
        "playlist_min_tracks": 5,
        "min_play_seconds": 30,
        "ignore_autoplay_unknown": True,
    },
    "post_download_command": None,
    "log_file": None,
    "db_path": None,
}


@dataclass
class SpotifyConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8888/callback"


@dataclass
class Thresholds:
    album_coverage: float = 0.5
    album_min_unique_tracks: int = 2
    track_plays: int = 3
    playlist_min_tracks: int = 5
    min_play_seconds: int = 30
    ignore_autoplay_unknown: bool = True


@dataclass
class Config:
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)
    output_dir: str = ""
    spotiflac_cli_path: str = "spotiflac-cli"
    poll_interval_minutes: int = 30
    thresholds: Thresholds = field(default_factory=Thresholds)
    post_download_command: str | None = None
    log_file: str | None = None
    db_path: str | None = None
    _config_dir: str = ""

    @property
    def resolved_db_path(self) -> str:
        if self.db_path:
            return self.db_path
        return os.path.join(self._config_dir or ".", "flacjacket.db")

    @property
    def token_path(self) -> str:
        return os.path.join(self._config_dir or ".", "spotify_token.json")

    @property
    def tracks_dir(self) -> str:
        return os.path.join(self.output_dir, "tracks")

    @property
    def lyrics_dir(self) -> str:
        return os.path.join(self.output_dir, "lyrics")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.output_dir, ".staging")

    @property
    def albums_meta_dir(self) -> str:
        return os.path.join(self.output_dir, "albums")

    @property
    def playlists_meta_dir(self) -> str:
        return os.path.join(self.output_dir, "playlists")


def find_config() -> str | None:
    candidates = [
        os.environ.get("FLACJACKET_CONFIG"),
        os.path.join(os.getcwd(), "config.json"),
        os.path.expanduser("~/.config/flacjacket/config.json"),
        "/etc/flacjacket/config.json",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def load_config(path: str | None = None) -> Config:
    if path is None:
        path = find_config()

    if path is None:
        raise FileNotFoundError(
            "No config.json found. Create one at ~/.config/flacjacket/config.json "
            "or set FLACJACKET_CONFIG. See config.example.json for format."
        )

    with open(path) as f:
        raw = json.load(f)

    config_dir = os.path.dirname(os.path.abspath(path))

    spotify_raw = raw.get("spotify", {})
    spotify = SpotifyConfig(
        client_id=spotify_raw.get("client_id", ""),
        client_secret=spotify_raw.get("client_secret", ""),
        redirect_uri=spotify_raw.get("redirect_uri", "http://127.0.0.1:8888/callback"),
    )

    thresh_raw = raw.get("thresholds", {})
    thresholds = Thresholds(
        album_coverage=thresh_raw.get("album_coverage", 0.5),
        album_min_unique_tracks=thresh_raw.get("album_min_unique_tracks", 2),
        track_plays=thresh_raw.get("track_plays", 3),
        playlist_min_tracks=thresh_raw.get("playlist_min_tracks", 5),
        min_play_seconds=thresh_raw.get("min_play_seconds", 30),
        ignore_autoplay_unknown=thresh_raw.get("ignore_autoplay_unknown", True),
    )

    output_dir = raw.get("output_dir", "")
    if not output_dir:
        output_dir = os.path.expanduser("~/Music")

    return Config(
        spotify=spotify,
        output_dir=output_dir,
        spotiflac_cli_path=raw.get("spotiflac_cli_path", "spotiflac-cli"),
        poll_interval_minutes=raw.get("poll_interval_minutes", 30),
        thresholds=thresholds,
        post_download_command=raw.get("post_download_command"),
        log_file=raw.get("log_file"),
        db_path=raw.get("db_path"),
        _config_dir=config_dir,
    )
