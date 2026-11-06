# ipod-manager

A local web UI for managing an iPod Shuffle (3rd/4th gen): upload (with automatic conversion of FLAC/OGG/WMA/etc. to AAC), delete, edit tags, play, download, and import from Google Drive. An iPod Nano 6G can be browsed, have its tags edited, and have tracks downloaded.

Runs on **macOS** and **Windows**. It only listens on `127.0.0.1:5757`.

## Windows

1. Install [Python 3](https://www.python.org/downloads/) and tick "Add python.exe to PATH" during setup.
2. Optional, needed only to convert FLAC/OGG/WMA/etc.: install ffmpeg with `winget install Gyan.FFmpeg`, or put `ffmpeg.exe` in a `resources\` folder next to `app.py`.
3. Double-click `start_windows.bat`. The first run sets up a `.venv`, and then the app opens in your browser.

The iPod is found automatically on any drive letter. **Eject** uses Explorer's own Eject action.

> An iPod that was formatted on a Mac (HFS+) can't be read by Windows. Shuffles are always FAT32, so they work, but a Mac-formatted Nano needs restoring in iTunes for Windows first.

## macOS

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ffmpeg   # optional, for conversion
.venv/bin/python app.py
```

Then open http://127.0.0.1:5757.

Converted files use Apple's AudioToolbox AAC encoder (`aac_at`) on macOS and ffmpeg's built-in `aac` encoder on Windows, both at about 256 kbps.
