# YouRip-

NEXUS media suite.

| File | Role |
|------|------|
| `main.py` | **Entry point.** Startup animation, then the desktop GUI |
| `interface.py` | PySide6 intro animation + placeholder homepage (GUI not built out yet) |
| `backend.py` | YouTube search / yt-dlp download workers, with a terminal shell. No Qt dependency |
| `ghost_window.py` | Floating translucent player; runs as a separate process |

## Run

```bash
python -m pip install -r requirements.txt
python main.py            # startup animation -> GUI
python main.py --shell    # backend terminal shell (extra args go to the backend)
python backend.py         # backend shell directly
```

Downloads go to `~/Downloads/YouRip` by default (override with `-d`).
FFmpeg is optional (needed for mp3 and merged mp4).

On Debian/Ubuntu, the ghost player needs `sudo apt install libpulse0` if Qt Multimedia fails to load.
