"""Spotify Web API client with OAuth token management."""

import json
import hashlib
import secrets
import base64
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SCOPES = "user-read-recently-played user-top-read"


class SpotifyAuth:
    """Handles OAuth PKCE flow and token persistence."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, token_path: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self._token: dict | None = None

    def _load_token(self) -> dict | None:
        try:
            with open(self.token_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_token(self, token: dict):
        Path(self.token_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump(token, f, indent=2)

    def get_access_token(self) -> str:
        if self._token is None:
            self._token = self._load_token()

        if self._token is None:
            raise RuntimeError(
                "No Spotify token found. Run 'flacjacket auth' first to authenticate."
            )

        # Refresh if expired (with 60s buffer)
        if self._token.get("expires_at", 0) < time.time() + 60:
            self._refresh_token()

        return self._token["access_token"]

    def _refresh_token(self):
        if not self._token or "refresh_token" not in self._token:
            raise RuntimeError("No refresh token available. Run 'flacjacket auth' again.")

        data = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._token["refresh_token"],
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()

        req = urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Token refresh failed ({e.code}): {body}")

        token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        # Keep existing refresh_token if not returned in response
        if "refresh_token" not in token_data:
            token_data["refresh_token"] = self._token["refresh_token"]

        self._token = token_data
        self._save_token(token_data)

    def authorize_interactive(self):
        """Run the OAuth flow interactively (opens browser, listens for callback)."""
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        state = secrets.token_urlsafe(16)

        auth_params = urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
        })

        auth_url = f"{SPOTIFY_AUTH_URL}?{auth_params}"
        print(f"Opening browser for Spotify authorization...")
        print(f"If it doesn't open, visit: {auth_url}")
        webbrowser.open(auth_url)

        # Listen for callback
        parsed_redirect = urlparse(self.redirect_uri)
        port = parsed_redirect.port or 8888

        auth_code = None
        received_state = None

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal auth_code, received_state
                query = parse_qs(urlparse(self.path).query)
                auth_code = query.get("code", [None])[0]
                received_state = query.get("state", [None])[0]

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authorization successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )

            def log_message(self, format, *args):
                pass  # Suppress HTTP logs

        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        server.timeout = 120
        server.handle_request()

        if auth_code is None:
            raise RuntimeError("No authorization code received. Try again.")

        if received_state != state:
            raise RuntimeError("State mismatch — possible CSRF attack. Try again.")

        # Exchange code for token
        data = urlencode({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": code_verifier,
        }).encode()

        req = urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Token exchange failed ({e.code}): {body}")

        token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        self._token = token_data
        self._save_token(token_data)
        print("Authentication successful! Token saved.")


class SpotifyClient:
    """Spotify Web API client for reading listening history."""

    def __init__(self, auth: SpotifyAuth):
        self.auth = auth

    def _request(self, endpoint: str, params: dict | None = None) -> Any:
        url = f"{SPOTIFY_API_BASE}{endpoint}"
        if params:
            url += "?" + urlencode(params)

        token = self.auth.get_access_token()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                print(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                return self._request(endpoint, params)
            body = e.read().decode()
            raise RuntimeError(f"Spotify API error ({e.code}): {body}")

    def get_recently_played(self, limit: int = 50, after: int | None = None) -> dict:
        params = {"limit": min(limit, 50)}
        if after is not None:
            params["after"] = after
        return self._request("/me/player/recently-played", params)

    def get_top_tracks(self, time_range: str = "short_term", limit: int = 50) -> dict:
        return self._request("/me/top/tracks", {
            "time_range": time_range,
            "limit": min(limit, 50),
        })

    def get_top_artists(self, time_range: str = "short_term", limit: int = 50) -> dict:
        return self._request("/me/top/artists", {
            "time_range": time_range,
            "limit": min(limit, 50),
        })

    def get_album(self, album_id: str) -> dict:
        return self._request(f"/albums/{album_id}")

    def get_track(self, track_id: str) -> dict:
        return self._request(f"/tracks/{track_id}")

    def get_several_tracks(self, track_ids: list[str]) -> dict:
        """Fetch up to 50 tracks at once."""
        return self._request("/tracks", {"ids": ",".join(track_ids[:50])})

    def get_playlist(self, playlist_id: str) -> dict:
        """Fetch playlist metadata (name, owner, tracks count)."""
        return self._request(f"/playlists/{playlist_id}", {
            "fields": "id,name,owner(display_name),tracks(total)",
        })
