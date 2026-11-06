"""Reads/writes the iTunesSD playback database used by 3rd/4th-gen iPod Shuffle.

Format verified byte-for-byte against a real Apple-written iTunesSD file
(header layout, 372-byte track entries, playlist section) -- not just from
reverse-engineering docs, which disagreed on some field names/order.
"""
import glob
import os
import random
import string
import struct
import subprocess

import mutagen

import platform_support

HEADER_LEN = 64
TRACK_ENTRY_LEN = 372
TRACK_HEADER_FIXED_LEN = 20  # id(4) + total_length(4) + number_of_tracks(4) + unknown(8)
FILENAME_FIELD_LEN = 256

# The Shuffle's firmware only decodes MP3/AAC/WAV (Apple's own spec, no
# lossless support at all, not even ALAC). These common formats need
# transcoding to AAC before they can go on the device.
NEEDS_CONVERSION = {".flac", ".ogg", ".oga", ".wma", ".aiff", ".aif", ".alac", ".opus", ".dsf"}


def _ffmpeg_path():
    """A statically-linked ffmpeg shipped in resources/ (portable builds),
    else ffmpeg from PATH."""
    return platform_support.ffmpeg_path(os.path.dirname(os.path.abspath(__file__)))

# Verified byte-for-byte from a real device: id(4) shdb-order-reversed "hphs",
# total_length(4)=0x48, number_of_playlists(4)=1, then 56 bytes of
# type-specific-count sentinels (semantics undocumented, but this exact byte
# pattern is what a real sync produced for "one master playlist, nothing else").
PLAYLIST_HEADER_TEMPLATE = (
    b"hphs" + struct.pack("<III", 0x48, 1, 0)
    + b"\xff\xff\xff\xff\x00\x00\x00\x00" * 4
    + b"\x00" * 20
)
assert len(PLAYLIST_HEADER_TEMPLATE) == 0x44  # + 4-byte offset field = 0x48 total

FILETYPES = {".mp3": 1, ".m4a": 2, ".m4b": 2, ".aac": 2, ".wav": 4}


def find_ipod_mount():
    """Return the mount point of a connected classic iPod Shuffle, or None."""
    for vol in platform_support.candidate_volumes():
        if os.path.isfile(os.path.join(vol, "iPod_Control", "iTunes", "iTunesSD")):
            return vol
    return None


def music_dir(mount):
    return os.path.join(mount, "iPod_Control", "Music")


def list_tracks(mount):
    """All mp3/m4a/etc. files currently under iPod_Control/Music, sorted."""
    paths = []
    for f_dir in sorted(glob.glob(os.path.join(music_dir(mount), "F*"))):
        if os.path.isdir(f_dir):
            for name in sorted(os.listdir(f_dir)):
                # Skip macOS AppleDouble sidecar files (e.g. "._Song.mp3"):
                # metadata stubs FAT/exFAT volumes pick up from Finder
                # copies, not real audio, even though the name matches.
                if name.startswith("._"):
                    continue
                full = os.path.join(f_dir, name)
                if os.path.isfile(full) and os.path.splitext(name)[1].lower() in FILETYPES:
                    paths.append(full)
    return paths


def track_info(path):
    tags = None
    try:
        tags = mutagen.File(path, easy=True)
    except Exception:
        pass
    title = artist = None
    duration_s = 0
    if tags is not None:
        if tags.info is not None:
            duration_s = getattr(tags.info, "length", 0) or 0
        if tags.tags:
            title = (tags.tags.get("title") or [None])[0]
            artist = (tags.tags.get("artist") or [None])[0]
    return {
        "path": path,
        "name": os.path.basename(path),
        "title": title or os.path.splitext(os.path.basename(path))[0],
        "artist": artist,
        "duration_s": duration_s,
        "size": os.path.getsize(path),
    }


def update_tags(mount, filename, title, artist):
    """Rewrite the title/artist ID3 (or MP4) tags of an on-device track.

    Doesn't touch iTunesSD -- the shuffle has no screen and never reads
    these fields; this is purely for keeping the library organized here.
    """
    for path in list_tracks(mount):
        if os.path.basename(path) == filename:
            audio = mutagen.File(path, easy=True)
            if audio is None:
                raise ValueError("unsupported or unreadable audio file")
            if audio.tags is None:
                audio.add_tags()
            audio.tags["title"] = title
            audio.tags["artist"] = artist
            audio.save()
            return
    raise FileNotFoundError(filename)


def _least_full_folder(mount):
    music = music_dir(mount)
    f_dirs = sorted(d for d in glob.glob(os.path.join(music, "F*")) if os.path.isdir(d))
    if not f_dirs:
        f_dirs = [os.path.join(music, "F00")]
        os.makedirs(f_dirs[0], exist_ok=True)
    return min(f_dirs, key=lambda d: len(os.listdir(d)))


def _random_name(existing_lower, ext):
    while True:
        name = "".join(random.choices(string.ascii_uppercase, k=4)) + ext
        if name.lower() not in existing_lower:
            return name


def convert_lossless(src_path, dest_path):
    """Transcode any ffmpeg-readable audio file to AAC ~256kbps (better
    quality-per-byte than MP3, and natively supported by the Shuffle),
    carrying over tags. See platform_support.aac_encoder_args for the
    per-OS encoder choice.

    -ar 44100 is required, not cosmetic: the Shuffle (and aac_at) top out
    well below 96kHz, so hi-res sources (e.g. 24-bit/96kHz FLAC) must be
    resampled.

    Raises RuntimeError with ffmpeg's stderr on failure.
    """
    result = subprocess.run(
        [
            _ffmpeg_path(), "-y", "-i", src_path,
            "-map", "0:a:0", "-map_metadata", "0",
            "-ar", "44100", *platform_support.aac_encoder_args(),
            dest_path,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **platform_support.SUBPROCESS_FLAGS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[-500:]}")


def add_track(mount, src_path, original_filename):
    """Copy src_path into the iPod's music tree under a randomized name.

    Returns the destination path.
    """
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in FILETYPES:
        raise ValueError(f"Unsupported file type: {ext}")
    dest_dir = _least_full_folder(mount)
    existing_lower = {n.lower() for n in os.listdir(dest_dir)}
    dest_name = _random_name(existing_lower, ext)
    dest_path = os.path.join(dest_dir, dest_name)
    with open(src_path, "rb") as fsrc, open(dest_path, "wb") as fdst:
        fdst.write(fsrc.read())
    return dest_path


def list_orphans(mount):
    """Audio files sitting loose in the volume root -- not under
    iPod_Control/Music, so the shuffle firmware can't see or play them."""
    orphans = []
    for name in sorted(os.listdir(mount)):
        if name.startswith("._"):
            continue
        full = os.path.join(mount, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in FILETYPES:
            orphans.append(full)
    return orphans


def import_orphan(mount, orphan_path):
    """Move a loose root-level audio file into the managed Music tree."""
    dest_path = add_track(mount, orphan_path, os.path.basename(orphan_path))
    os.remove(orphan_path)
    return dest_path


def delete_track(mount, filename):
    """Delete a track by its on-device filename (e.g. 'HXVJ.mp3'), wherever it lives."""
    for path in list_tracks(mount):
        if os.path.basename(path) == filename:
            os.remove(path)
            return True
    return False


def _device_relpath(mount, path):
    rel = os.path.relpath(path, mount).replace(os.sep, "/")
    return "/" + rel


def build_itunessd(mount, tracks):
    """Build the full iTunesSD bytes for the given list of track dicts
    (as returned by track_info), in the order they should appear."""
    n = len(tracks)

    tracks_header_len = TRACK_HEADER_FIXED_LEN + 4 * n
    track_data_start = HEADER_LEN + tracks_header_len
    playlist_header_offset = track_data_start + n * TRACK_ENTRY_LEN

    # --- main header (bdhs) ---
    header = bytearray(HEADER_LEN)
    header[0:4] = b"bdhs"
    struct.pack_into("<I", header, 0x04, 0x02010001)  # version, copied from a real device
    struct.pack_into("<I", header, 0x08, HEADER_LEN)
    struct.pack_into("<I", header, 0x0C, n)
    struct.pack_into("<I", header, 0x10, 1)  # one playlist ("All Songs")
    struct.pack_into("<B", header, 0x1C, 0)  # max_volume: unlimited
    struct.pack_into("<B", header, 0x1D, 0)  # voiceover disabled
    struct.pack_into("<I", header, 0x20, n)  # no podcasts/audiobooks
    struct.pack_into("<I", header, 0x24, HEADER_LEN)
    struct.pack_into("<I", header, 0x28, playlist_header_offset)

    # --- tracks header / offset index (hths) ---
    th = bytearray(tracks_header_len)
    th[0:4] = b"hths"
    struct.pack_into("<I", th, 0x04, tracks_header_len)
    struct.pack_into("<I", th, 0x08, n)
    for i in range(n):
        offset = track_data_start + i * TRACK_ENTRY_LEN
        struct.pack_into("<I", th, TRACK_HEADER_FIXED_LEN + 4 * i, offset)

    # --- individual track entries (rths) ---
    track_blobs = []
    for t in tracks:
        entry = bytearray(TRACK_ENTRY_LEN)
        entry[0:4] = b"rths"
        struct.pack_into("<I", entry, 0x04, TRACK_ENTRY_LEN)
        struct.pack_into("<I", entry, 0x08, 0)  # start_at_pos_ms
        duration_ms = int(round((t["duration_s"] or 0) * 1000))
        struct.pack_into("<I", entry, 0x0C, duration_ms)  # stop_at_pos_ms
        struct.pack_into("<I", entry, 0x10, 0)  # volume_gain
        ext = os.path.splitext(t["path"])[1].lower()
        struct.pack_into("<I", entry, 0x14, FILETYPES[ext])
        relpath = _device_relpath(mount, t["path"]).encode("ascii", "ignore")
        if len(relpath) >= FILENAME_FIELD_LEN:
            raise ValueError(f"Path too long for iTunesSD: {relpath}")
        entry[0x18:0x18 + len(relpath)] = relpath
        # bytes 0x118..0x174 (bookmark, flags, gapless/album/track/dbid/artist
        # fields) are left zero -- confirmed safe defaults by a real-world
        # iTunesSD writer (foo_dop's ipod_manager), only stop_at_pos_ms and
        # filetype are required for correct playback.
        track_blobs.append(bytes(entry))

    # --- playlist header (hphs) ---
    ph = bytearray(PLAYLIST_HEADER_TEMPLATE)
    playlist_entry_offset = playlist_header_offset + len(PLAYLIST_HEADER_TEMPLATE)
    ph += struct.pack("<I", playlist_entry_offset)
    assert len(ph) == 0x48

    # --- playlist entry: "All Songs" master playlist (lphs) ---
    playlist_len = 44 + 4 * n
    pl = bytearray(playlist_len)
    pl[0:4] = b"lphs"
    struct.pack_into("<I", pl, 0x04, playlist_len)
    struct.pack_into("<I", pl, 0x08, n)
    struct.pack_into("<I", pl, 0x0C, n)
    # dbid (0x10, 8 bytes) stays zero -> "All Songs" convention
    struct.pack_into("<I", pl, 0x18, 1)  # type: master
    # unknown_1 (0x1C, 16 bytes) stays zero
    for i in range(n):
        struct.pack_into("<I", pl, 0x2C + 4 * i, i)

    return bytes(header) + bytes(th) + b"".join(track_blobs) + bytes(ph) + bytes(pl)


def rebuild(mount):
    """Rescan the on-device music folders and rewrite iTunesSD to match.
    Keeps a one-generation backup at iTunesSD.bak."""
    tracks = [track_info(p) for p in list_tracks(mount)]
    data = build_itunessd(mount, tracks)

    sd_path = os.path.join(mount, "iPod_Control", "iTunes", "iTunesSD")
    bak_path = sd_path + ".bak"
    tmp_path = sd_path + ".tmp"

    if os.path.exists(sd_path):
        with open(sd_path, "rb") as f:
            old = f.read()
        with open(bak_path, "wb") as f:
            f.write(old)

    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, sd_path)
    return len(tracks)
