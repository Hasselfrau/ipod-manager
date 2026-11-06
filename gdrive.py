"""Browse Google Drive and pull audio files down for import.

Read-only by design (drive.readonly scope) -- this app only ever reads
files out of Drive, never writes or modifies anything there.

One-time setup (done by the user, not this code):
  1. console.cloud.google.com -> create/select a project.
  2. APIs & Services -> Library -> enable the Google Drive API.
  3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
     -> Application type: Desktop app.
  4. Add http://localhost:5757/gdrive/oauth-callback as an authorized
     redirect URI on that client.
  5. Enter the resulting Client ID/Secret into this app's Drive setup form.
"""
import json
import os
import urllib.parse

import requests

CONFIG_PATH = os.path.expanduser("~/.config/ipod_shuffle_manager/gdrive.json")
REDIRECT_URI = "http://localhost:5757/gdrive/oauth-callback"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def is_configured(cfg):
    return bool(cfg.get("client_id") and cfg.get("client_secret"))


def is_logged_in(cfg):
    return bool(cfg.get("refresh_token"))


def auth_url(cfg):
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # ensures a refresh_token is issued even on repeat logins
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(cfg, code):
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    })
    resp.raise_for_status()
    data = resp.json()
    cfg["refresh_token"] = data["refresh_token"]
    save_config(cfg)


def get_access_token(cfg):
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_folder(token, folder_id="root"):
    """Folders and audio files directly inside folder_id, folders first."""
    resp = requests.get(f"{API_BASE}/files", headers={"Authorization": f"Bearer {token}"}, params={
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "files(id,name,mimeType,size)",
        "orderBy": "folder,name",
        "pageSize": 200,
    })
    resp.raise_for_status()
    items = resp.json().get("files", [])

    folders, files = [], []
    for item in items:
        if item["mimeType"] == FOLDER_MIME:
            folders.append({"id": item["id"], "name": item["name"]})
        else:
            ext = os.path.splitext(item["name"])[1].lower()
            files.append({
                "id": item["id"],
                "name": item["name"],
                "ext": ext,
                "size": int(item.get("size", 0)),
            })
    return folders, files


def folder_name(token, folder_id):
    if folder_id == "root":
        return "My Drive"
    resp = requests.get(f"{API_BASE}/files/{folder_id}", headers={"Authorization": f"Bearer {token}"},
                         params={"fields": "name"})
    resp.raise_for_status()
    return resp.json()["name"]


def download_file(token, file_id, dest_path):
    resp = requests.get(f"{API_BASE}/files/{file_id}", headers={"Authorization": f"Bearer {token}"},
                         params={"alt": "media"}, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
