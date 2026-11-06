"""Export a Spotify playlist's track list (title/artist/album/duration) to CSV.

This only reads metadata via Spotify's own Web API -- it does not download
audio. What you do with the resulting list (buy the tracks, match them
against music you already own, etc.) is up to you.

One-time setup:
  1. Go to https://developer.spotify.com/dashboard -> Create app.
  2. App name/description: anything. Redirect URI: exactly
         http://127.0.0.1:8888/callback
     (must match exactly, including the port).
  3. Save, then open the app's Settings to copy the Client ID and
     Client Secret.

Usage:
  python3 spotify_export.py login                 # one-time browser login
  python3 spotify_export.py list                   # list your playlists
  python3 spotify_export.py export <playlist_id_or_url> [-o out.csv]
"""
import argparse
import csv
import getpass
import json
import os
import re
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

CONFIG_DIR = os.path.expanduser("~/.config/ipod_shuffle_manager")
CONFIG_PATH = os.path.join(CONFIG_DIR, "spotify.json")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
REDIRECT_PORT = 8888
SCOPES = "playlist-read-private playlist-read-collaborative"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def ensure_client_credentials(cfg):
    if cfg.get("client_id") and cfg.get("client_secret"):
        return cfg
    print("No Spotify API credentials on file yet.")
    print("Create an app at https://developer.spotify.com/dashboard")
    print(f"  Redirect URI must be exactly: {REDIRECT_URI}")
    cfg["client_id"] = input("Client ID: ").strip()
    cfg["client_secret"] = getpass.getpass("Client Secret (hidden): ").strip()
    save_config(cfg)
    return cfg


class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result["code"] = params.get("code", [None])[0]
        _CallbackHandler.result["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "You can close this tab and return to the terminal."
        self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

    def log_message(self, fmt, *args):
        pass  # silence default request logging


def do_login(cfg):
    cfg = ensure_client_credentials(cfg)
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("Opening your browser to log in to Spotify...")
    webbrowser.open(url)

    _CallbackHandler.result = {}
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.timeout = 120
    server.handle_request()  # blocks for exactly one request

    code = _CallbackHandler.result.get("code")
    if not code:
        err = _CallbackHandler.result.get("error", "no callback received (timed out?)")
        print(f"Login failed: {err}", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
    )
    resp.raise_for_status()
    tokens = resp.json()
    cfg["refresh_token"] = tokens["refresh_token"]
    save_config(cfg)
    print("Logged in. Refresh token saved -- you won't need to log in again.")


def get_access_token(cfg):
    cfg = ensure_client_credentials(cfg)
    if not cfg.get("refresh_token"):
        print("Not logged in yet. Run: python3 spotify_export.py login", file=sys.stderr)
        sys.exit(1)
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "refresh_token" in data:  # Spotify sometimes rotates it
        cfg["refresh_token"] = data["refresh_token"]
        save_config(cfg)
    return data["access_token"]


def api_get(token, path, params=None):
    resp = requests.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, params=params)
    resp.raise_for_status()
    return resp.json()


def list_playlists(token):
    playlists = []
    url_path, params = "/me/playlists", {"limit": 50}
    while url_path:
        data = api_get(token, url_path, params)
        playlists.extend(data["items"])
        nxt = data.get("next")
        if nxt:
            parsed = urllib.parse.urlparse(nxt)
            url_path = parsed.path.replace("/v1", "")
            params = dict(urllib.parse.parse_qsl(parsed.query))
        else:
            url_path = None
    return playlists


PLAYLIST_ID_RE = re.compile(r"playlist[/:]([A-Za-z0-9]+)")


def parse_playlist_id(text):
    m = PLAYLIST_ID_RE.search(text)
    return m.group(1) if m else text.strip()


def fetch_playlist_tracks(token, playlist_id):
    tracks = []
    url_path = f"/playlists/{playlist_id}/tracks"
    params = {"limit": 100}
    while url_path:
        data = api_get(token, url_path, params)
        for item in data["items"]:
            t = item.get("track")
            if not t:
                continue
            tracks.append({
                "title": t.get("name", ""),
                "artists": ", ".join(a["name"] for a in t.get("artists", [])),
                "album": (t.get("album") or {}).get("name", ""),
                "duration_ms": t.get("duration_ms", 0),
                "spotify_url": (t.get("external_urls") or {}).get("spotify", ""),
            })
        nxt = data.get("next")
        if nxt:
            parsed = urllib.parse.urlparse(nxt)
            url_path = parsed.path.replace("/v1", "")
            params = dict(urllib.parse.parse_qsl(parsed.query))
        else:
            url_path = None
    return tracks


def write_csv(tracks, out_path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Artist", "Album", "Duration", "Spotify URL"])
        for t in tracks:
            ms = t["duration_ms"]
            duration = f"{ms // 60000}:{(ms // 1000) % 60:02d}"
            writer.writerow([t["title"], t["artists"], t["album"], duration, t["spotify_url"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="One-time browser login, saves a refresh token")
    sub.add_parser("list", help="List your playlists (name + id)")
    exp = sub.add_parser("export", help="Export a playlist's tracks to CSV")
    exp.add_argument("playlist", help="Playlist URL or ID")
    exp.add_argument("-o", "--output", default=None, help="Output CSV path (default: <playlist name>.csv)")

    args = parser.parse_args()
    cfg = load_config()

    if args.command == "login":
        do_login(cfg)
        return

    token = get_access_token(cfg)

    if args.command == "list":
        for p in list_playlists(token):
            print(f"{p['id']}  {p['name']}  ({p['tracks']['total']} tracks)")
        return

    if args.command == "export":
        playlist_id = parse_playlist_id(args.playlist)
        meta = api_get(token, f"/playlists/{playlist_id}", {"fields": "name"})
        tracks = fetch_playlist_tracks(token, playlist_id)
        out_path = args.output or f"{meta['name']}.csv"
        write_csv(tracks, out_path)
        print(f"Exported {len(tracks)} tracks from '{meta['name']}' to {out_path}")


if __name__ == "__main__":
    main()
