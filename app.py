"""Local web UI for a connected iPod Shuffle (full add/delete/convert) or
iPod Nano 6G (browse + edit tags -- see nano6.py for why writes are limited).

Run: python3 app.py
Then open http://127.0.0.1:5757

Works on macOS and Windows (OS-specific bits live in platform_support.py).
Only talks to the mounted iPod volume on this computer -- binds to
localhost only.
"""
import os
import re
import tempfile
import uuid
import zipfile

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

import gdrive
import itunessd
import nano6
import platform_support
from platform_support import VOLUMES_HINT

_UNSAFE_FILENAME_CHARS = re.compile(r'[/:*?"<>|\\]')


def _download_name(title, artist, ext):
    base = f"{artist} - {title}" if artist else (title or "track")
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip() or "track"
    return base + ext

app = Flask(__name__)
app.secret_key = os.urandom(24)

# token -> (staged_file_path, dest_filename), populated by /upload-stage and
# consumed by /upload-commit. In-memory only: fine for a single-user local
# tool: if a stage is never committed the temp file just leaks until the
# server restarts (acceptable for a personal script).
STAGED_UPLOADS = {}


if platform_support.IS_WINDOWS:
    _ACCESS_HINT = "Windows denied access to the iPod's top-level folder. Check the drive isn't write-protected or open in another program."
else:
    _ACCESS_HINT = (
        "macOS hasn't granted this app access to the iPod's top-level folder. "
        "Grant it in System Settings → Privacy & Security → Files and Folders "
        "(or Removable Volumes), or run the app from Terminal instead."
    )


def require_mount():
    mount = itunessd.find_ipod_mount()
    if not mount:
        flash(f"No iPod Shuffle detected under {VOLUMES_HINT}. Plug it in and refresh.", "error")
    return mount


def find_any_mount():
    """(mount, device_type) for whichever supported iPod is connected --
    device_type is 'shuffle' or 'nano6', or (None, None) if none found."""
    mount = itunessd.find_ipod_mount()
    if mount:
        return mount, "shuffle"
    mount = nano6.find_nano_mount()
    if mount:
        return mount, "nano6"
    return None, None


@app.route("/")
def index():
    mount, device_type = find_any_mount()
    tracks = []
    orphans = []
    if device_type == "shuffle":
        tracks = [itunessd.track_info(p) for p in itunessd.list_tracks(mount)]
        tracks.sort(key=lambda t: t["title"].lower())
        try:
            orphans = [os.path.basename(p) for p in itunessd.list_orphans(mount)]
        except PermissionError:
            flash("Can't check for loose files outside the library: " + _ACCESS_HINT, "error")
    elif device_type == "nano6":
        tracks = nano6.list_tracks(mount)
        tracks.sort(key=lambda t: (t["title"] or "").lower())

    gcfg = gdrive.load_config()
    url_prefix = "" if device_type == "shuffle" else "/nano"
    return render_template(
        "index.html", mount=mount, device_type=device_type, tracks=tracks, orphans=orphans,
        url_prefix=url_prefix,
        gdrive_configured=gdrive.is_configured(gcfg), gdrive_logged_in=gdrive.is_logged_in(gcfg),
    )


def _stage_local_file(tmp_path, original_filename):
    """Given bytes already on disk (from an upload or a Drive download),
    convert if needed and register under a token for /upload-commit.
    Shared by /upload-stage and /gdrive/stage. Takes ownership of tmp_path
    (removes it on failure, or after conversion consumes it)."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in itunessd.FILETYPES and ext not in itunessd.NEEDS_CONVERSION:
        os.remove(tmp_path)
        raise ValueError("Unsupported file type")

    dest_path, dest_name, converted = tmp_path, original_filename, False
    try:
        if ext in itunessd.NEEDS_CONVERSION:
            aac_path = tmp_path + ".m4a"
            itunessd.convert_lossless(tmp_path, aac_path)
            os.remove(tmp_path)
            dest_path = aac_path
            dest_name = os.path.splitext(original_filename)[0] + ".m4a"
            converted = True
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    token = uuid.uuid4().hex
    STAGED_UPLOADS[token] = (dest_path, dest_name)
    return token, converted


@app.route("/upload-stage", methods=["POST"])
def upload_stage():
    """Stage 1: receive one file and convert it if the Shuffle can't play its
    format natively. This is the network+ffmpeg-bound step -- whatever time
    this call takes is genuinely "converting" (or just "uploading" for
    formats that don't need conversion)."""
    mount = itunessd.find_ipod_mount()
    if not mount:
        return jsonify(ok=False, error="No iPod Shuffle detected"), 400

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="No file"), 400

    ext = os.path.splitext(f.filename)[1].lower()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        f.save(tmp)
        tmp_path = tmp.name

    try:
        token, converted = _stage_local_file(tmp_path, f.filename)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, token=token, converted=converted)


@app.route("/upload-commit", methods=["POST"])
def upload_commit():
    """Stage 2: copy an already-staged file onto the iPod. This is the real
    on-device write, so this call's duration is genuinely "uploading"."""
    mount = require_mount()
    if not mount:
        return jsonify(ok=False, error="No iPod Shuffle detected"), 400

    token = request.form.get("token", "")
    staged = STAGED_UPLOADS.pop(token, None)
    if not staged:
        return jsonify(ok=False, error="Unknown or expired upload"), 400

    path, name = staged
    try:
        itunessd.add_track(mount, path, name)
        return jsonify(ok=True, name=name)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.route("/gdrive/status")
def gdrive_status():
    cfg = gdrive.load_config()
    return jsonify(configured=gdrive.is_configured(cfg), logged_in=gdrive.is_logged_in(cfg))


@app.route("/gdrive/setup", methods=["POST"])
def gdrive_setup():
    cfg = gdrive.load_config()
    cfg["client_id"] = request.form.get("client_id", "").strip()
    cfg["client_secret"] = request.form.get("client_secret", "").strip()
    gdrive.save_config(cfg)
    return redirect("/gdrive/login")


@app.route("/gdrive/login")
def gdrive_login():
    cfg = gdrive.load_config()
    if not gdrive.is_configured(cfg):
        flash("Enter a Google Drive Client ID/Secret first.", "error")
        return redirect(url_for("index"))
    return redirect(gdrive.auth_url(cfg))


@app.route("/gdrive/oauth-callback")
def gdrive_oauth_callback():
    error = request.args.get("error")
    if error:
        flash(f"Google Drive login failed: {error}", "error")
        return redirect(url_for("index"))
    code = request.args.get("code")
    cfg = gdrive.load_config()
    try:
        gdrive.exchange_code(cfg, code)
        flash("Google Drive connected.", "ok")
    except Exception as e:
        flash(f"Google Drive login failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/gdrive/browse")
def gdrive_browse():
    cfg = gdrive.load_config()
    if not gdrive.is_logged_in(cfg):
        return jsonify(ok=False, error="Not connected to Google Drive"), 400
    folder_id = request.args.get("folder_id", "root")
    try:
        token = gdrive.get_access_token(cfg)
        folders, files = gdrive.list_folder(token, folder_id)
        name = gdrive.folder_name(token, folder_id)
        importable = itunessd.FILETYPES.keys() | itunessd.NEEDS_CONVERSION
        for f in files:
            f["importable"] = f["ext"] in importable
        return jsonify(ok=True, folder_id=folder_id, folder_name=name, folders=folders, files=files)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.route("/gdrive/stage", methods=["POST"])
def gdrive_stage():
    """Drive equivalent of /upload-stage: download one file from Drive,
    then run it through the same convert-if-needed staging as a local
    upload. Whatever this call's duration is covers both the Drive
    download and any conversion."""
    mount = itunessd.find_ipod_mount()
    if not mount:
        return jsonify(ok=False, error="No iPod Shuffle detected"), 400

    cfg = gdrive.load_config()
    if not gdrive.is_logged_in(cfg):
        return jsonify(ok=False, error="Not connected to Google Drive"), 400

    file_id = request.form.get("file_id", "")
    name = request.form.get("name", "")
    if not file_id or not name:
        return jsonify(ok=False, error="Missing file_id/name"), 400

    ext = os.path.splitext(name)[1].lower()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name

    try:
        token = gdrive.get_access_token(cfg)
        gdrive.download_file(token, file_id, tmp_path)
        stage_token, converted = _stage_local_file(tmp_path, name)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, token=stage_token, converted=converted)


@app.route("/upload-finalize", methods=["POST"])
def upload_finalize():
    """Stage 3: rebuild iTunesSD once for the whole batch, not once per file."""
    mount = require_mount()
    if not mount:
        return jsonify(ok=False, error="No iPod Shuffle detected"), 400
    n = itunessd.rebuild(mount)
    return jsonify(ok=True, count=n)


@app.route("/delete/<filename>", methods=["POST"])
def delete(filename):
    mount = require_mount()
    if not mount:
        return redirect(url_for("index"))

    if itunessd.delete_track(mount, filename):
        n = itunessd.rebuild(mount)
        flash(f"Deleted {filename}. iTunesSD rebuilt with {n} tracks.", "ok")
    else:
        flash(f"{filename} not found on device.", "error")
    return redirect(url_for("index"))


@app.route("/delete-selected", methods=["POST"])
def delete_selected():
    mount = require_mount()
    if not mount:
        return redirect(url_for("index"))

    filenames = request.form.getlist("filenames")
    if not filenames:
        flash("No tracks selected.", "error")
        return redirect(url_for("index"))

    deleted, missing = 0, []
    for fn in filenames:
        if itunessd.delete_track(mount, fn):
            deleted += 1
        else:
            missing.append(fn)

    if deleted:
        n = itunessd.rebuild(mount)
        flash(f"Deleted {deleted} track(s). iTunesSD rebuilt with {n} tracks.", "ok")
    if missing:
        flash("Not found: " + ", ".join(missing), "error")
    return redirect(url_for("index"))


@app.route("/edit/<filename>", methods=["POST"])
def edit(filename):
    mount = require_mount()
    if not mount:
        return redirect(url_for("index"))

    title = request.form.get("title", "").strip()
    artist = request.form.get("artist", "").strip()
    try:
        itunessd.update_tags(mount, filename, title, artist)
        flash(f"Updated tags for {filename}.", "ok")
    except Exception as e:
        flash(f"Failed to update {filename}: {e}", "error")
    return redirect(url_for("index"))


@app.route("/nano/edit/<pid>", methods=["POST"])
def nano_edit(pid):
    mount = nano6.find_nano_mount()
    if not mount:
        flash(f"No Nano detected under {VOLUMES_HINT}. Plug it in and refresh.", "error")
        return redirect(url_for("index"))

    title = request.form.get("title", "").strip()
    artist = request.form.get("artist", "").strip()
    try:
        nano6.update_tags(mount, int(pid), title, artist)
        flash("Updated tags.", "ok")
    except Exception as e:
        flash(f"Failed to update tags: {e}", "error")
    return redirect(url_for("index"))


@app.route("/nano/audio/<pid>")
def nano_audio(pid):
    mount = nano6.find_nano_mount()
    if not mount:
        abort(404)
    track = nano6.track_by_pid(mount, int(pid))
    if not track:
        abort(404)
    return send_file(track["path"], conditional=True)


@app.route("/nano/download/<pid>")
def nano_download(pid):
    mount = nano6.find_nano_mount()
    if not mount:
        abort(404)
    track = nano6.track_by_pid(mount, int(pid))
    if not track:
        abort(404)
    ext = os.path.splitext(track["path"])[1]
    return send_file(
        track["path"], as_attachment=True,
        download_name=_download_name(track["title"], track["artist"], ext),
    )


@app.route("/nano/download-zip", methods=["POST"])
def nano_download_zip():
    mount = nano6.find_nano_mount()
    if not mount:
        abort(404)
    pids = {int(p) for p in request.form.getlist("filenames")}
    tracks = [t for t in nano6.list_tracks(mount) if t["pid"] in pids]
    return _send_zip(tracks)


@app.route("/import-orphans", methods=["POST"])
def import_orphans():
    mount = require_mount()
    if not mount:
        return redirect(url_for("index"))

    try:
        orphans = itunessd.list_orphans(mount)
    except PermissionError:
        flash(_ACCESS_HINT, "error")
        return redirect(url_for("index"))

    imported, failed = 0, []
    for path in orphans:
        try:
            itunessd.import_orphan(mount, path)
            imported += 1
        except Exception as e:
            failed.append(f"{os.path.basename(path)} ({e})")

    if imported:
        n = itunessd.rebuild(mount)
        flash(f"Imported {imported} loose file(s) into the library. iTunesSD rebuilt with {n} tracks.", "ok")
    if failed:
        flash("Failed: " + "; ".join(failed), "error")
    if not orphans:
        flash("No loose files found to import.", "error")
    return redirect(url_for("index"))


@app.route("/audio/<filename>")
def audio(filename):
    mount = itunessd.find_ipod_mount()
    if not mount:
        abort(404)
    for path in itunessd.list_tracks(mount):
        if os.path.basename(path) == filename:
            # conditional=True enables HTTP Range requests, so the <audio>
            # element can seek/scrub instead of only playing from the start.
            return send_file(path, conditional=True)
    abort(404)


def _send_zip(tracks):
    """Build a temp zip of the given itunessd/nano6 track dicts (each with
    at least path/title/artist) and stream it back as one download. The
    temp file is removed once the response has actually finished sending
    (call_on_close, not right after return -- send_file streams it lazily)."""
    if not tracks:
        abort(400)
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        used_names = set()
        for t in tracks:
            ext = os.path.splitext(t["path"])[1]
            name = _download_name(t["title"], t["artist"], ext)
            # avoid collisions when multiple tracks share the same display name
            unique_name, n = name, 1
            while unique_name in used_names:
                n += 1
                unique_name = f"{os.path.splitext(name)[0]} ({n}){ext}"
            used_names.add(unique_name)
            zf.write(t["path"], arcname=unique_name)

    resp = send_file(tmp_path, as_attachment=True, download_name="iPod Tracks.zip", mimetype="application/zip")
    def _cleanup():
        try:
            os.remove(tmp_path)
        except OSError:
            pass  # already gone, or (Windows) still locked -- it's just a temp file
    resp.call_on_close(_cleanup)
    return resp


@app.route("/download/<filename>")
def download(filename):
    mount = itunessd.find_ipod_mount()
    if not mount:
        abort(404)
    for path in itunessd.list_tracks(mount):
        if os.path.basename(path) == filename:
            info = itunessd.track_info(path)
            ext = os.path.splitext(path)[1]
            return send_file(path, as_attachment=True, download_name=_download_name(info["title"], info["artist"], ext))
    abort(404)


@app.route("/download-zip", methods=["POST"])
def download_zip():
    mount = itunessd.find_ipod_mount()
    if not mount:
        abort(404)
    filenames = set(request.form.getlist("filenames"))
    tracks = [itunessd.track_info(p) for p in itunessd.list_tracks(mount) if os.path.basename(p) in filenames]
    return _send_zip(tracks)


@app.route("/eject", methods=["POST"])
def eject():
    mount, _device_type = find_any_mount()
    if not mount:
        flash(f"No iPod detected under {VOLUMES_HINT}.", "error")
        return redirect(url_for("index"))
    try:
        platform_support.eject(mount)
        flash(f"Ejected {mount}. Safe to unplug.", "ok")
    except Exception as e:
        flash(f"Eject failed: {e}", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5757, debug=False)
