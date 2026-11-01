# YouRip-

NEXUS is a YouTube media workspace with a PySide6 desktop interface, `yt-dlp`
search/download backend, and a floating ghost player.

## Run

```bash
python -m pip install -r requirements.txt
python main.py
```

`main.py` is the desktop application entry point. Downloads are saved to
`~/Downloads/YouRip` (or `~/YouRip` when the Downloads folder is absent).
Install FFmpeg if you want yt-dlp to merge separate audio and video streams.

The floating player uses Qt Multimedia. Supported stream formats depend on the
Qt Multimedia backend and available system codecs. On Debian/Ubuntu, install
the PulseAudio runtime library with `sudo apt install libpulse0` if Qt
Multimedia fails to load.
