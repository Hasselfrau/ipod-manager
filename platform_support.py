"""OS-specific bits: where removable volumes show up, how to eject one,
and which ffmpeg/AAC encoder to use. Everything else in the app is plain
file I/O on the mounted volume and works the same on macOS and Windows.
"""
import glob
import os
import string
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Keep ffmpeg/PowerShell from flashing a console window on Windows when
# the app is started without one (e.g. via pythonw).
SUBPROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

# Shown in "no iPod found" messages.
VOLUMES_HINT = "your drive letters" if IS_WINDOWS else "/Volumes"


def candidate_volumes():
    """Root paths of mounted volumes that might be an iPod."""
    if IS_WINDOWS:
        return _windows_drives()
    return glob.glob("/Volumes/*")


def _windows_drives():
    import ctypes

    kernel32 = ctypes.windll.kernel32
    # Without this, probing an empty card-reader slot pops a
    # "There is no disk in the drive" dialog.
    SEM_FAILCRITICALERRORS = 0x0001
    old_mode = kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)
    try:
        bitmask = kernel32.GetLogicalDrives()
        drives = []
        for i, letter in enumerate(string.ascii_uppercase):
            if not bitmask & (1 << i):
                continue
            root = f"{letter}:\\"
            # 2 = removable, 3 = fixed. iPods normally report as removable,
            # but some USB bridges present them as fixed disks. Skip network
            # shares, CD drives and RAM disks.
            if kernel32.GetDriveTypeW(root) in (2, 3):
                drives.append(root)
        return drives
    finally:
        kernel32.SetErrorMode(old_mode)


def eject(mount):
    """Eject the volume so the iPod is safe to unplug. Raises RuntimeError
    with a human-readable reason on failure."""
    if IS_WINDOWS:
        _eject_windows(mount)
    else:
        result = subprocess.run(["diskutil", "eject", mount], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="ignore").strip() or "diskutil eject failed")


def _eject_windows(mount):
    """Use Explorer's own "Eject" action (same as right-click -> Eject),
    which flushes and performs a proper safe removal. It reports nothing
    back, so success is judged by the drive going away."""
    drive = os.path.splitdrive(mount)[0]  # "F:"
    script = (
        "(New-Object -ComObject Shell.Application).Namespace(17)"
        f".ParseName('{drive}').InvokeVerb('Eject')"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=30, **SUBPROCESS_FLAGS,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if not os.path.exists(mount):
            return
        time.sleep(0.5)
    raise RuntimeError(
        "Windows didn't release the drive -- close any Explorer windows or apps "
        "using it and try again, or use \"Safely Remove Hardware\" in the taskbar."
    )


def ffmpeg_path(base_dir):
    """A bundled static ffmpeg in resources/ (used by portable builds), or
    whatever is on PATH."""
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    bundled = os.path.join(base_dir, "resources", exe)
    return bundled if os.path.exists(bundled) else "ffmpeg"


def aac_encoder_args():
    """ffmpeg encoder flags for ~256 kbps AAC.

    macOS: Apple's AudioToolbox encoder (aac_at) -- best quality, but only
    exists in macOS ffmpeg builds. It needs s16 samples, and abr mode
    because its vbr mode overshoots the bitrate by ~40%.

    Elsewhere: ffmpeg's built-in "aac" encoder, available in every build
    (it works on float samples, so no -sample_fmt here).
    """
    if IS_MAC:
        return ["-sample_fmt", "s16", "-codec:a", "aac_at", "-aac_at_mode", "abr", "-b:a", "256k"]
    return ["-codec:a", "aac", "-b:a", "256k"]
