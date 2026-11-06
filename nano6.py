"""Read/edit access to the SQLite-based library used by iPod Nano 5G/6G/7G.

Unlike the classic iPod_Control/iTunes/iTunesDB format (used by the
Shuffle, Classic, and Nano 1-4G -- see itunessd.py), these devices store
their library in plain SQLite databases under
iPod_Control/iTunes/iTunes Library.itlp/. Verified against a real Nano 6G:
Library.itdb holds track metadata (the `item` table); Locations.itdb holds
on-disk file paths (the `location` table, keyed by item.pid), rooted at
whatever iPod_Control/Music-equivalent path `base_location` names.

Scope, deliberately limited to reading and editing existing track
title/artist. Adding or deleting tracks would require rewriting
Locations.itdb, which is authenticated by a signature file
(Locations.itdb.cbk) that -- for Nano 6G/7G specifically -- is computed
with a white-box AES scheme Apple calls "HashAB": an obfuscated,
DRM-style algorithm with no clean, from-scratch reimplementation. The
only community solution (the iOpenPod project) relies on a compiled WASM
module built from reverse-engineered Apple binary logic, not a portable
algorithm. Title/artist edits only touch Library.itdb, never
Locations.itdb, so they need no signature and carry none of that risk.
"""
import glob
import os
import sqlite3
import time

MAC_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01 (Cocoa/CFAbsoluteTime epoch)


def find_nano_mount():
    """Return the mount point of a connected Nano 5G/6G/7G-style iPod, or None."""
    for vol in glob.glob("/Volumes/*"):
        if os.path.isfile(_library_path(vol)):
            return vol
    return None


def _library_path(mount):
    return os.path.join(mount, "iPod_Control", "iTunes", "iTunes Library.itlp", "Library.itdb")


def _locations_path(mount):
    return os.path.join(mount, "iPod_Control", "iTunes", "iTunes Library.itlp", "Locations.itdb")


def _connect(mount, readonly=True):
    mode = "ro" if readonly else "rw"
    lib_uri = f"file:{_library_path(mount)}?mode={mode}"
    conn = sqlite3.connect(lib_uri, uri=True)
    # Locations.itdb is always opened read-only: we never touch file
    # placement, only Library.itdb metadata columns.
    loc_uri = f"file:{_locations_path(mount)}?mode=ro"
    conn.execute(f"ATTACH DATABASE '{loc_uri}' AS loc")
    return conn


def list_tracks(mount):
    conn = _connect(mount)
    try:
        cur = conn.execute("""
            SELECT item.pid, item.title, item.artist, item.album, item.total_time_ms,
                   loc.base_location.path, loc.location.location
            FROM item
            JOIN loc.location ON loc.location.item_pid = item.pid
            JOIN loc.base_location ON loc.base_location.id = loc.location.base_location_id
            WHERE item.is_song = 1
            ORDER BY item.title COLLATE NOCASE
        """)
        tracks = []
        for pid, title, artist, album, total_time_ms, base_path, relpath in cur.fetchall():
            tracks.append({
                "pid": pid,
                "name": str(pid),
                "title": title or os.path.basename(relpath),
                "artist": artist,
                "album": album,
                "duration_s": (total_time_ms or 0) / 1000.0,
                "path": os.path.join(mount, base_path, relpath),
            })
        return tracks
    finally:
        conn.close()


def track_by_pid(mount, pid):
    for t in list_tracks(mount):
        if t["pid"] == pid:
            return t
    return None


def update_tags(mount, pid, title, artist):
    conn = _connect(mount, readonly=False)
    try:
        now_mac_epoch = int(time.time()) - MAC_EPOCH_OFFSET
        cur = conn.execute(
            "UPDATE item SET title = ?, artist = ?, date_modified = ? WHERE pid = ?",
            (title, artist, now_mac_epoch, pid),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no track with pid {pid}")
    finally:
        conn.close()
