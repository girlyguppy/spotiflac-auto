"""Download orchestrator — calls spotiflac-cli subprocess."""

import json
import subprocess
import shutil
from typing import Any


class Downloader:
    def __init__(self, cli_path: str, output_dir: str):
        self.cli_path = cli_path
        self.output_dir = output_dir
        self._validate()

    def _validate(self):
        if not shutil.which(self.cli_path):
            # Try as absolute path
            import os
            if not os.path.isfile(self.cli_path):
                raise FileNotFoundError(
                    f"spotiflac-cli not found at '{self.cli_path}'. "
                    f"Build it from SpotiFLAC source or set spotiflac_cli_path in config."
                )

    def download_track(self, spotify_url: str) -> dict:
        """Download a single track. Returns result dict."""
        return self._run(spotify_url)

    def download_album(self, spotify_url: str) -> dict:
        """Download a full album. Returns result dict."""
        return self._run(spotify_url)

    def _run(self, spotify_url: str) -> dict:
        cmd = [
            self.cli_path,
            "-output", self.output_dir,
            "-lyrics",
            "-json",
            spotify_url,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min timeout for albums
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Download timed out after 30 minutes",
                "url": spotify_url,
            }
        except FileNotFoundError:
            return {
                "success": False,
                "error": f"spotiflac-cli not found at {self.cli_path}",
                "url": spotify_url,
            }

        # Parse JSON output (comes after the human-readable progress)
        stdout = result.stdout
        json_results = None
        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith("["):
                try:
                    json_results = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        success = result.returncode == 0

        return {
            "success": success,
            "returncode": result.returncode,
            "results": json_results,
            "stdout": stdout,
            "stderr": result.stderr,
            "url": spotify_url,
        }

    def download_by_id(self, spotify_id: str, id_type: str = "track") -> dict:
        """Download by Spotify ID."""
        url = f"https://open.spotify.com/{id_type}/{spotify_id}"
        return self._run(url)
