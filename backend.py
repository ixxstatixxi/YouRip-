#!/usr/bin/env python3
"""
NEXUS backend
=============
  * SearchService   - YouTube search through yt-dlp metadata (title, thumbnail, channel, ...)
  * ThumbnailCache  - downloads/caches thumbnails so a UI can populate cards
  * WorkerPool      - background workers that hand URLs to yt-dlp, with retries + backoff
  * Debugger        - ring-buffer log, live echo, timing spans, tracebacks, yt-dlp log capture
  * EventBus        - thread-safe pub/sub so the PySide6 UI can react to jobs and results
  * NexusShell      - terminal look-alike with a rainbow ASCII title (python backend.py)

Everything (downloads, thumbnail cache, debug log) lives in one folder: ~/Downloads/YouRip by
default, or whatever you pass with -d. It is created on first start if it does not exist.

Requires: Python 3.10+, `pip install yt-dlp`  (ffmpeg optional, needed for mp3 / merged mp4)

The backend has no Qt dependency. Event callbacks fire on worker threads, so a Qt UI should
forward them through a Signal instead of touching widgets directly.
"""
from __future__ import annotations

import argparse
import colorsys
import concurrent.futures
import enum
import itertools
import json
import os
import queue
import random
import re
import shlex
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

try:
    import yt_dlp
except ImportError:  # the shell still starts; actions explain what is missing
    yt_dlp = None

try:
    import readline  # noqa: F401  (line editing + history in the shell)
    HAS_READLINE = True
except ImportError:
    HAS_READLINE = False

APP_NAME = "NEXUS"
MAX_RESULTS = 100  # every search asks for (and is capped at) 100 results
APP_DIR_NAME = "YouRip"
VIDEO_FORMATS = ("mp4", "mkv", "webm")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "wav")
ALL_FORMATS = VIDEO_FORMATS + AUDIO_FORMATS
DEFAULT_VIDEO_HEIGHT = 1080  # max height when no quality is given (0 = best available)
DEFAULT_AUDIO_KBPS = 192     # lossy audio bitrate when no quality is given
YT_URL_RE = re.compile(r"^https?://", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ════════════════════════════════════════════════════════════════════════════
# Colour helpers
# ════════════════════════════════════════════════════════════════════════════
class Style:
    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    RESET = "\x1b[0m"

    @classmethod
    def paint(cls, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}{cls.RESET}" if cls.enabled else text


def dim(t): return Style.paint(t, "2")
def bold(t): return Style.paint(t, "1")
def red(t): return Style.paint(t, "31")
def green(t): return Style.paint(t, "32")
def yellow(t): return Style.paint(t, "33")
def cyan(t): return Style.paint(t, "36")
def grey(t): return Style.paint(t, "90")


def rainbow(text: str, offset: float = 0.0, spread: float = 0.035) -> str:
    """Per-character truecolor rainbow. `offset` shifts the hue (used for animation)."""
    if not Style.enabled:
        return text
    out = []
    for i, ch in enumerate(text):
        if ch.isspace():
            out.append(ch)
            continue
        r, g, b = (int(v * 255) for v in colorsys.hsv_to_rgb((offset + i * spread) % 1.0, 0.85, 1.0))
        out.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
    return "".join(out) + Style.RESET


def fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_count(n: Optional[int]) -> str:
    if n is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(n)


def fmt_date(raw: Optional[str]) -> str:
    if raw and re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw or "?"


def fmt_size(n: Optional[float]) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ════════════════════════════════════════════════════════════════════════════
# Folder + format helpers
# ════════════════════════════════════════════════════════════════════════════
def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / APP_DIR_NAME


def prepare_dir(path: "Path | str | None" = None) -> tuple[Path, str]:
    """Use the folder if it exists, otherwise create it. -> (absolute path, 'existing' | 'created')."""
    p = Path(path).expanduser() if path else default_download_dir()
    if p.exists():
        if not p.is_dir():
            raise ValueError(f"{p} exists but is not a folder")
        status = "existing"
    else:
        p.mkdir(parents=True, exist_ok=True)
        status = "created"
    if not os.access(p, os.W_OK):
        raise ValueError(f"{p} is not writable")
    return p.resolve(), status


def normalize_format(fmt: Optional[str]) -> str:
    f = (fmt or "mp4").lower().lstrip(".")
    f = {"video": "mp4", "audio": "mp3", "aac": "m4a", "ogg": "opus"}.get(f, f)
    if f not in ALL_FORMATS:
        raise ValueError(f"unknown format '{fmt}' (choose: {', '.join(ALL_FORMATS)})")
    return f


def format_mode(fmt: str) -> str:
    return "audio" if fmt in AUDIO_FORMATS else "video"


def parse_quality(value: "str | int | None") -> Optional[int]:
    """Video: max height in px. Audio: bitrate in kbps. 'best' (or 0) = no cap."""
    if value is None or value == "":
        return None
    if str(value).lower() == "best":
        return 0
    q = int(value)
    if q < 0:
        raise ValueError("quality must be positive")
    return q


# ════════════════════════════════════════════════════════════════════════════
# Debugger
# ════════════════════════════════════════════════════════════════════════════
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
LEVEL_COLOR = {"DEBUG": "90", "INFO": "36", "WARN": "33", "ERROR": "31"}


@dataclass
class LogRecord:
    ts: float
    level: str
    source: str
    msg: str
    data: dict = field(default_factory=dict)
    tb: Optional[str] = None

    def format(self, color: bool = True) -> str:
        stamp = datetime.fromtimestamp(self.ts).strftime("%H:%M:%S.%f")[:-3]
        extra = " ".join(f"{k}={v!r}" for k, v in self.data.items())
        line = f"{stamp} {self.level:<5} {self.source:<10} {self.msg}" + (f"  {extra}" if extra else "")
        if color and Style.enabled:
            line = Style.paint(line, LEVEL_COLOR[self.level])
        return line


class Debugger:
    """Always-on, low-overhead debugger: ring buffer + optional file + optional live echo."""

    def __init__(self, capacity: int = 1500, logfile: Optional[Path] = None,
                 live: bool = False, live_level: str = "DEBUG"):
        self._buf: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self.live = live
        self.live_level = live_level
        self.counts: Counter = Counter()
        self.last_error: Optional[LogRecord] = None
        self._file = None
        if logfile:
            logfile = Path(logfile)
            logfile.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(logfile, "a", encoding="utf-8")

    # -- core -----------------------------------------------------------------
    def log(self, level: str, source: str, msg: str, exc: Optional[BaseException] = None, **data):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else None
        rec = LogRecord(time.time(), level, source, msg, data, tb)
        with self._lock:
            self._buf.append(rec)
            self.counts[level] += 1
            if level == "ERROR":
                self.last_error = rec
            if self._file:
                self._file.write(rec.format(color=False) + ("\n" + tb if tb else "") + "\n")
                self._file.flush()
        if self.live and LEVELS[level] >= LEVELS[self.live_level]:
            print(rec.format(), file=sys.stderr, flush=True)

    def debug(self, source, msg, **d): self.log("DEBUG", source, msg, **d)
    def info(self, source, msg, **d): self.log("INFO", source, msg, **d)
    def warn(self, source, msg, **d): self.log("WARN", source, msg, **d)
    def error(self, source, msg, exc=None, **d): self.log("ERROR", source, msg, exc=exc, **d)

    # -- tools ----------------------------------------------------------------
    @contextmanager
    def span(self, source: str, label: str, **data):
        """Time a block: `with dbg.span("search", "ytsearch", q=query): ...`"""
        start = time.perf_counter()
        self.debug(source, f"> {label}", **data)
        try:
            yield
        except BaseException as exc:
            self.error(source, f"x {label} failed after {time.perf_counter() - start:.2f}s: {exc}", exc=exc)
            raise
        else:
            self.debug(source, f"< {label} ok in {time.perf_counter() - start:.2f}s")

    def tail(self, n: int = 40, min_level: str = "DEBUG") -> list[LogRecord]:
        with self._lock:
            recs = [r for r in self._buf if LEVELS[r.level] >= LEVELS[min_level]]
        return recs[-n:]

    def dump(self, path: Path) -> Path:
        path = Path(path)
        with self._lock:
            path.write_text("\n".join(r.format(color=False) + (("\n" + r.tb) if r.tb else "")
                                      for r in self._buf), encoding="utf-8")
        return path

    def install_hooks(self):
        """Route uncaught exceptions (main + threads) into the debugger."""
        prev_main, prev_thread = sys.excepthook, threading.excepthook

        def main_hook(tp, val, tb):
            self.error("uncaught", f"{tp.__name__}: {val}", exc=val)
            prev_main(tp, val, tb)

        def thread_hook(args):
            self.error("uncaught", f"thread {args.thread.name if args.thread else '?'}: "
                                   f"{args.exc_type.__name__}: {args.exc_value}", exc=args.exc_value)
            prev_thread(args)

        sys.excepthook, threading.excepthook = main_hook, thread_hook

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


class YDLLogger:
    """Adapter so yt-dlp's own output lands in the debugger instead of the terminal."""

    def __init__(self, dbg: Debugger, source: str):
        self.dbg, self.source = dbg, source

    def debug(self, msg):
        if msg.startswith("[debug] "):
            self.dbg.debug(self.source, msg[8:])
        else:
            self.dbg.info(self.source, ANSI_RE.sub("", msg))

    def info(self, msg): self.dbg.info(self.source, ANSI_RE.sub("", msg))
    def warning(self, msg): self.dbg.warn(self.source, ANSI_RE.sub("", msg))
    def error(self, msg): self.dbg.error(self.source, ANSI_RE.sub("", msg))


# ════════════════════════════════════════════════════════════════════════════
# Events
# ════════════════════════════════════════════════════════════════════════════
class EventBus:
    """emit('job.updated', job=...) -> every subscriber gets (name, data). Callbacks never raise."""

    def __init__(self, dbg: Debugger):
        self._subs: list[Callable[[str, dict], None]] = []
        self._lock = threading.Lock()
        self.dbg = dbg

    def subscribe(self, cb: Callable[[str, dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)
        return lambda: self._subs.remove(cb) if cb in self._subs else None

    def emit(self, name: str, **data):
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(name, data)
            except Exception as exc:
                self.dbg.error("events", f"subscriber failed on {name}", exc=exc)


# ════════════════════════════════════════════════════════════════════════════
# Errors + retry engine
# ════════════════════════════════════════════════════════════════════════════
class Cancelled(Exception):
    pass


class MissingDependency(Exception):
    pass


class FetchError(Exception):
    """Raised when an operation gave up. `kind` is 'permanent' or 'exhausted'."""

    def __init__(self, message: str, *, kind: str, attempts: int):
        super().__init__(message)
        self.kind, self.attempts = kind, attempts


_PERMANENT = (
    "private video", "video unavailable", "this video is unavailable", "has been removed",
    "copyright", "not available in your country", "members-only", "join this channel",
    "confirm your age", "age-restricted", "unsupported url", "is not a valid url",
    "no video formats", "account associated", "terminated", "sign in to confirm",
    "premieres in", "live event will begin",
)
_RATE_LIMITED = ("http error 429", "too many requests", "rate limit", "rate-limit")


def clean_message(exc: BaseException) -> str:
    msg = ANSI_RE.sub("", str(exc)).strip()
    return re.sub(r"^(ERROR:\s*)+", "", msg) or exc.__class__.__name__


def classify_error(exc: BaseException) -> str:
    """-> 'permanent' (never retry), 'rate_limited' (retry slowly) or 'transient' (retry)."""
    if isinstance(exc, (ValueError, TypeError, MissingDependency, PermissionError, FileNotFoundError)):
        return "permanent"
    msg = str(exc).lower()
    if any(p in msg for p in _PERMANENT):
        return "permanent"
    if any(p in msg for p in _RATE_LIMITED):
        return "rate_limited"
    return "transient"


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 2.0
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.25

    def delay(self, attempt: int, kind: str = "transient") -> float:
        d = min(self.max_delay, self.base_delay * self.factor ** (attempt - 1))
        if kind == "rate_limited":
            d = min(self.max_delay * 2, d * 2)
        return max(0.0, d * (1 + random.uniform(-self.jitter, self.jitter)))


def _is_cancel(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, Cancelled):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def call_with_retries(fn: Callable[[], Any], policy: RetryPolicy, *, label: str, dbg: Debugger,
                      cancel: Optional[threading.Event] = None,
                      on_attempt: Optional[Callable[[int], None]] = None,
                      on_retry: Optional[Callable[[int, BaseException, float], None]] = None) -> Any:
    """Run fn() up to policy.max_attempts times with exponential backoff + jitter."""
    for attempt in range(1, policy.max_attempts + 1):
        if cancel and cancel.is_set():
            raise Cancelled()
        if on_attempt:
            on_attempt(attempt)
        try:
            return fn()
        except BaseException as exc:
            if _is_cancel(exc):
                raise Cancelled() from exc
            if not isinstance(exc, Exception):  # KeyboardInterrupt etc.
                raise
            kind = classify_error(exc)
            msg = clean_message(exc)
            if kind == "permanent":
                dbg.error("retry", f"{label}: permanent failure, not retrying: {msg}", exc=exc)
                raise FetchError(msg, kind="permanent", attempts=attempt) from exc
            if attempt >= policy.max_attempts:
                dbg.error("retry", f"{label}: gave up after {attempt} attempts: {msg}", exc=exc)
                raise FetchError(msg, kind="exhausted", attempts=attempt) from exc
            delay = policy.delay(attempt, kind)
            dbg.warn("retry", f"{label}: attempt {attempt}/{policy.max_attempts} failed ({kind}): {msg}; "
                              f"retrying in {delay:.1f}s")
            if on_retry:
                on_retry(attempt, exc, delay)
            if cancel:
                if cancel.wait(delay):
                    raise Cancelled()
            else:
                time.sleep(delay)
    raise AssertionError("unreachable")


def require_ytdlp():
    if yt_dlp is None:
        raise MissingDependency("yt-dlp is not installed. Run:  pip install -U yt-dlp")


# ════════════════════════════════════════════════════════════════════════════
# Metadata model
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class VideoMeta:
    id: str
    title: str
    url: str
    channel: str = ""
    channel_url: str = ""
    duration: Optional[int] = None
    views: Optional[int] = None
    upload_date: Optional[str] = None
    description: str = ""
    thumbnail: str = ""
    thumbnail_path: Optional[str] = None
    like_count: Optional[int] = None
    is_live: bool = False
    detailed: bool = False  # True once enriched via a full (non-flat) extraction

    @property
    def duration_str(self) -> str:
        return "LIVE" if self.is_live else fmt_duration(self.duration)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["duration_str"] = self.duration_str
        return d

    @classmethod
    def from_ydl(cls, info: dict, *, detailed: bool = False) -> Optional["VideoMeta"]:
        vid = info.get("id")
        if not vid:
            return None
        url = info.get("webpage_url") or info.get("url") or ""
        if not YT_URL_RE.match(url):
            url = f"https://www.youtube.com/watch?v={vid}"
        date = info.get("upload_date")
        if not date and info.get("timestamp"):
            date = datetime.utcfromtimestamp(info["timestamp"]).strftime("%Y%m%d")
        return cls(
            id=vid,
            title=info.get("title") or "(untitled)",
            url=url,
            channel=info.get("channel") or info.get("uploader") or "",
            channel_url=info.get("channel_url") or info.get("uploader_url") or "",
            duration=int(info["duration"]) if info.get("duration") else None,
            views=info.get("view_count"),
            upload_date=date,
            description=info.get("description") or "",
            thumbnail=pick_thumbnail(info, vid),
            like_count=info.get("like_count"),
            is_live=bool(info.get("is_live")) or info.get("live_status") == "is_live",
            detailed=detailed,
        )


def pick_thumbnail(info: dict, vid: str) -> str:
    """Largest JPEG thumbnail if present (Qt reads JPEG everywhere, WebP only with a plugin)."""
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    jpgs = [t for t in thumbs if urllib.parse.urlparse(t["url"]).path.lower().endswith((".jpg", ".jpeg"))]
    pool = jpgs or thumbs
    if pool:
        best = max(pool, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0) or t.get("preference") or 0)
        return best["url"]
    return info.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


# ════════════════════════════════════════════════════════════════════════════
# Search + thumbnails
# ════════════════════════════════════════════════════════════════════════════
class SearchService:
    def __init__(self, dbg: Debugger, policy: RetryPolicy, ttl: float = 300.0):
        self.dbg, self.policy, self.ttl = dbg, policy, ttl
        self._cache: dict[tuple, tuple[float, list[VideoMeta]]] = {}
        self._lock = threading.Lock()

    def _opts(self, flat: bool) -> dict:
        o = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 15, "logger": YDLLogger(self.dbg, "yt-dlp"),
        }
        if flat:
            o["extract_flat"] = "in_playlist"
        return o

    def search(self, query: str, limit: int = MAX_RESULTS, *, use_cache: bool = True,
               cancel: Optional[threading.Event] = None) -> list[VideoMeta]:
        require_ytdlp()
        limit = max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))
        query = query.strip()
        if not query:
            raise ValueError("empty search query")
        key = (query.lower(), limit)
        with self._lock:
            hit = self._cache.get(key)
        if use_cache and hit and time.time() - hit[0] < self.ttl:
            self.dbg.debug("search", "cache hit", query=query)
            return hit[1]

        target = query if YT_URL_RE.match(query) else f"ytsearch{limit}:{query}"

        def run():
            with self.dbg.span("search", "extract_info", target=target):
                with yt_dlp.YoutubeDL(self._opts(flat=True)) as ydl:
                    return ydl.extract_info(target, download=False)

        info = call_with_retries(run, self.policy, label=f"search '{query}'", dbg=self.dbg, cancel=cancel)
        entries = (info or {}).get("entries") or [info]
        results = [m for e in entries if e for m in [VideoMeta.from_ydl(e)] if m][:limit]
        self.dbg.info("search", f"{len(results)} results", query=query)
        with self._lock:
            self._cache[key] = (time.time(), results)
        return results

    def details(self, meta: VideoMeta, cancel: Optional[threading.Event] = None) -> VideoMeta:
        """Full extraction for upload date, description, likes, ... Mutates + returns `meta`."""
        require_ytdlp()
        if meta.detailed:
            return meta

        def run():
            with self.dbg.span("search", "details", id=meta.id):
                with yt_dlp.YoutubeDL(self._opts(flat=False)) as ydl:
                    return ydl.extract_info(meta.url, download=False)

        info = call_with_retries(run, self.policy, label=f"details {meta.id}", dbg=self.dbg, cancel=cancel)
        full = VideoMeta.from_ydl(info, detailed=True)
        if full:
            full.thumbnail_path = meta.thumbnail_path
            meta.__dict__.update(full.__dict__)
        return meta


class ThumbnailCache:
    def __init__(self, directory: Path, dbg: Debugger, policy: Optional[RetryPolicy] = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dbg = dbg
        self.policy = policy or RetryPolicy(max_attempts=3, base_delay=1.0)

    def get(self, meta: VideoMeta, cancel: Optional[threading.Event] = None) -> Optional[Path]:
        for existing in self.dir.glob(f"{meta.id}.*"):
            if existing.suffix != ".part" and existing.stat().st_size > 0:
                meta.thumbnail_path = str(existing)
                return existing
        if not meta.thumbnail:
            return None
        ext = Path(urllib.parse.urlparse(meta.thumbnail).path).suffix.lower()
        dest = self.dir / f"{meta.id}{ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'}"

        def fetch():
            req = urllib.request.Request(meta.thumbnail, headers={"User-Agent": "Mozilla/5.0 NEXUS"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if not data:
                raise IOError("empty thumbnail response")
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)

        with self.dbg.span("thumb", "fetch", id=meta.id):
            call_with_retries(fetch, self.policy, label=f"thumbnail {meta.id}", dbg=self.dbg, cancel=cancel)
        meta.thumbnail_path = str(dest)
        return dest


# ════════════════════════════════════════════════════════════════════════════
# Download jobs + worker pool
# ════════════════════════════════════════════════════════════════════════════
class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE = (JobState.QUEUED, JobState.RUNNING, JobState.RETRYING)


@dataclass
class Job:
    id: int
    url: str
    title: str
    mode: str = "video"  # "video" | "audio" (derived from fmt)
    fmt: str = "mp4"
    quality: Optional[int] = None  # video: max height, audio: kbps, 0 = best
    state: JobState = JobState.QUEUED
    attempts: int = 0
    max_attempts: int = 4
    progress: float = 0.0
    speed: Optional[float] = None
    eta: Optional[int] = None
    error: Optional[str] = None
    filepath: Optional[str] = None
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    retry_at: Optional[float] = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {k: (v.value if isinstance(v, JobState) else v)
                for k, v in self.__dict__.items() if k != "cancel"}


class WorkerPool:
    def __init__(self, outdir: Path, dbg: Debugger, bus: EventBus, policy: RetryPolicy, workers: int = 2,
                 fmt: str = "mp4", quality: Optional[int] = None):
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.fmt, self.quality = normalize_format(fmt), quality
        self.dbg, self.bus, self.policy = dbg, bus, policy
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._q: queue.Queue[Optional[int]] = queue.Queue()
        self._lock = threading.RLock()
        self._threads = [threading.Thread(target=self._loop, name=f"nexus-worker-{i + 1}", daemon=True)
                         for i in range(workers)]
        for t in self._threads:
            t.start()
        self.dbg.info("worker", f"{workers} workers started", outdir=str(self.outdir))

    # -- public ---------------------------------------------------------------
    def submit(self, url: str, title: str = "", fmt: Optional[str] = None,
               quality: Optional[int] = None) -> Job:
        """Queue a download. fmt/quality default to the pool's current settings."""
        if not YT_URL_RE.match(url):
            raise ValueError(f"not a URL: {url!r}")
        fmt = normalize_format(fmt) if fmt else self.fmt
        if quality is None and format_mode(fmt) == format_mode(self.fmt):
            quality = self.quality  # default quality only carries over within video or within audio
        with self._lock:
            for j in self._jobs.values():  # don't queue the same thing twice
                if j.url == url and j.fmt == fmt and j.quality == quality and j.state in ACTIVE:
                    self.dbg.debug("worker", "duplicate submit ignored", job=j.id)
                    return j
            job = Job(next(self._ids), url, title or url, format_mode(fmt), fmt, quality,
                      max_attempts=self.policy.max_attempts)
            self._jobs[job.id] = job
        self._q.put(job.id)
        self._notify(job, "queued")
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.id)

    def get(self, job_id: int) -> Optional[Job]:
        return self._jobs.get(job_id)

    def cancel(self, job_id: int) -> bool:
        job = self.get(job_id)
        if not job or job.state not in ACTIVE:
            return False
        job.cancel.set()
        if job.state == JobState.QUEUED:  # no worker owns it yet
            self._finish(job, JobState.CANCELLED)
        return True

    def retry(self, job_id: int) -> bool:
        """Manually re-queue a failed/cancelled job (keeps partial files so yt-dlp can resume)."""
        job = self.get(job_id)
        if not job or job.state in ACTIVE:
            return False
        with self._lock:
            job.cancel = threading.Event()
            job.state, job.attempts, job.error, job.progress = JobState.QUEUED, 0, None, 0.0
            job.finished = job.retry_at = None
        self._q.put(job.id)
        self._notify(job, "requeued")
        return True

    def close(self, timeout: float = 3.0):
        for j in self.jobs():
            if j.state in ACTIVE:
                j.cancel.set()
        for _ in self._threads:
            self._q.put(None)
        deadline = time.time() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.time()))

    # -- internals ------------------------------------------------------------
    def _notify(self, job: Job, why: str):
        self.dbg.info("worker", f"job {job.id} {why}", state=job.state.value, attempt=job.attempts)
        self.bus.emit("job.updated", job=job, why=why)

    def _finish(self, job: Job, state: JobState, error: Optional[str] = None):
        job.state, job.finished, job.error, job.retry_at = state, time.time(), error, None
        if state == JobState.DONE:
            job.progress = 1.0
        self._notify(job, state.value)

    def _loop(self):
        while True:
            job_id = self._q.get()
            if job_id is None:
                return
            job = self._jobs[job_id]
            if job.state != JobState.QUEUED:  # cancelled while waiting
                continue
            try:
                self._run(job)
            except Exception as exc:  # a worker must never die
                self.dbg.error("worker", f"job {job.id} crashed", exc=exc)
                self._finish(job, JobState.FAILED, clean_message(exc))

    def _run(self, job: Job):
        job.state, job.started = JobState.RUNNING, time.time()
        self._notify(job, "started")

        def on_attempt(n):
            job.attempts, job.state, job.retry_at = n, JobState.RUNNING, None
            if n > 1:
                self._notify(job, f"attempt {n}")

        def on_retry(n, exc, delay):
            job.state, job.error, job.retry_at = JobState.RETRYING, clean_message(exc), time.time() + delay
            self._notify(job, f"retry in {delay:.0f}s")

        try:
            path = call_with_retries(lambda: self._download(job), self.policy, label=f"job {job.id}",
                                     dbg=self.dbg, cancel=job.cancel,
                                     on_attempt=on_attempt, on_retry=on_retry)
        except Cancelled:
            self._finish(job, JobState.CANCELLED)
        except FetchError as exc:
            self._finish(job, JobState.FAILED, str(exc))
        else:
            job.filepath = path
            self._finish(job, JobState.DONE)

    def _opts(self, job: Job, hook) -> dict:
        ffmpeg = shutil.which("ffmpeg") is not None
        o: dict[str, Any] = {
            "outtmpl": str(self.outdir / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "logger": YDLLogger(self.dbg, f"ydl#{job.id}"),
            "progress_hooks": [hook],
            "retries": 3, "fragment_retries": 3, "socket_timeout": 20, "continuedl": True,
        }
        fmt, q = job.fmt, job.quality
        if job.mode == "audio":
            kbps = DEFAULT_AUDIO_KBPS if q is None else q
            if ffmpeg:
                o["format"] = "bestaudio/best"
                pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
                if fmt in ("mp3", "m4a", "opus") and kbps:
                    pp["preferredquality"] = str(kbps)
                o["postprocessors"] = [pp]
            else:
                self.dbg.warn("worker", f"ffmpeg not found: keeping native audio, no conversion to {fmt}")
                o["format"] = "bestaudio[ext=m4a]/bestaudio/best" if fmt == "m4a" else "bestaudio/best"
        else:
            h = DEFAULT_VIDEO_HEIGHT if q is None else q
            cap = f"[height<={h}]" if h else ""
            if ffmpeg:
                if fmt == "mp4":
                    o["format"] = f"bv*{cap}[ext=mp4]+ba[ext=m4a]/bv*{cap}+ba/b{cap}/b"
                elif fmt == "webm":
                    o["format"] = f"bv*{cap}[ext=webm]+ba[ext=webm]/bv*{cap}+ba/b{cap}/b"
                else:
                    o["format"] = f"bv*{cap}+ba/b{cap}/b"
                o["merge_output_format"] = fmt
            else:
                self.dbg.warn("worker", f"ffmpeg not found: using one pre-merged stream, {fmt} not guaranteed")
                o["format"] = f"b{cap}[ext={fmt}]/b{cap}/b"
        return o

    def _download(self, job: Job) -> str:
        require_ytdlp()
        last_emit = 0.0

        def hook(d: dict):
            nonlocal last_emit
            if job.cancel.is_set():
                raise Cancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job.progress = min(0.99, (d.get("downloaded_bytes") or 0) / total)
                job.speed, job.eta = d.get("speed"), d.get("eta")
                if time.time() - last_emit >= 0.25:
                    last_emit = time.time()
                    self.bus.emit("job.progress", job=job)
            elif d.get("status") == "finished":
                job.progress, job.speed, job.eta = 0.99, None, None
                self.bus.emit("job.progress", job=job)

        with self.dbg.span("worker", "yt-dlp download", job=job.id, url=job.url, mode=job.mode):
            with yt_dlp.YoutubeDL(self._opts(job, hook)) as ydl:
                info = ydl.extract_info(job.url, download=True)
                if info and info.get("title") and job.title == job.url:
                    job.title = info["title"]
                done = (info or {}).get("requested_downloads") or []
                return (done[0].get("filepath") if done else None) or ydl.prepare_filename(info)


# ════════════════════════════════════════════════════════════════════════════
# Backend facade (what the UI talks to)
# ════════════════════════════════════════════════════════════════════════════
class Backend:
    def __init__(self, download_dir: Optional[Path] = None, cache_dir: Optional[Path] = None,
                 workers: int = 2, policy: Optional[RetryPolicy] = None,
                 debug: bool = False, logfile: Optional[Path] = None,
                 fmt: str = "mp4", quality: Optional[int] = None):
        # One folder holds everything: downloads + .nexus/ (thumbnails, debug log).
        self.download_dir, self.dir_status = prepare_dir(download_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else self.download_dir / ".nexus"
        self.policy = policy or RetryPolicy()
        self.debugger = Debugger(live=debug, logfile=logfile or self.cache_dir / "nexus-debug.log")
        self.debugger.install_hooks()
        self.bus = EventBus(self.debugger)
        self.search_service = SearchService(self.debugger, self.policy)
        self.thumbs = ThumbnailCache(self.cache_dir / "thumbnails", self.debugger)
        self.debugger.info("backend", f"download folder {self.dir_status}", path=str(self.download_dir))
        self.workers = WorkerPool(self.download_dir, self.debugger, self.bus, self.policy, workers,
                                  fmt=fmt, quality=quality)
        self._thumb_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="nexus-thumb")
        if yt_dlp:
            self.debugger.info("backend", f"yt-dlp {yt_dlp.version.__version__}", ffmpeg=bool(shutil.which("ffmpeg")))
        else:
            self.debugger.warn("backend", "yt-dlp not installed")

    # search ------------------------------------------------------------------
    def search(self, query: str, limit: int = MAX_RESULTS, prefetch_thumbnails: bool = True) -> list[VideoMeta]:
        results = self.search_service.search(query, limit)
        if prefetch_thumbnails:
            self.prefetch_thumbnails(results)
        return results

    def search_async(self, query: str, limit: int = MAX_RESULTS) -> threading.Thread:
        """Non-blocking: emits 'search.done' (query, results) or 'search.error' (query, error)."""
        def run():
            try:
                self.bus.emit("search.done", query=query, results=self.search(query, limit))
            except Exception as exc:
                self.debugger.error("backend", f"search failed: {query}", exc=exc)
                self.bus.emit("search.error", query=query, error=clean_message(exc))
        t = threading.Thread(target=run, name="nexus-search", daemon=True)
        t.start()
        return t

    def details(self, meta: VideoMeta) -> VideoMeta:
        self.search_service.details(meta)
        self.thumbs.get(meta)
        return meta

    def prefetch_thumbnails(self, metas: list[VideoMeta]):
        def one(m: VideoMeta):
            try:
                path = self.thumbs.get(m)
                if path:
                    self.bus.emit("thumbnail.ready", id=m.id, path=str(path), meta=m)
            except Exception as exc:
                self.debugger.warn("thumb", f"{m.id}: {clean_message(exc)}")
        for m in metas:
            self._thumb_pool.submit(one, m)

    # downloads ---------------------------------------------------------------
    def download(self, target: "VideoMeta | str", fmt: Optional[str] = None,
                 quality: Optional[int] = None) -> Job:
        """fmt: mp4/mkv/webm/mp3/m4a/opus/flac/wav (or 'video'/'audio'); default = current setting."""
        if isinstance(target, VideoMeta):
            return self.workers.submit(target.url, target.title, fmt, quality)
        return self.workers.submit(target, "", fmt, quality)

    def set_dir(self, path: "Path | str") -> tuple[Path, str]:
        """Switch download folder (created if missing). Thumbnails/log stay in the startup folder."""
        new, status = prepare_dir(path)
        self.download_dir = self.workers.outdir = new
        self.debugger.info("backend", f"download folder {status}", path=str(new))
        return new, status

    def set_format(self, fmt: str, quality: Optional[int] = None):
        self.workers.fmt, self.workers.quality = normalize_format(fmt), quality

    def jobs(self) -> list[Job]: return self.workers.jobs()
    def cancel(self, job_id: int) -> bool: return self.workers.cancel(job_id)
    def retry(self, job_id: int) -> bool: return self.workers.retry(job_id)
    def subscribe(self, cb): return self.bus.subscribe(cb)

    def close(self):
        self.workers.close()
        self._thumb_pool.shutdown(wait=False, cancel_futures=True)
        self.debugger.close()


# ════════════════════════════════════════════════════════════════════════════
# Terminal look-alike shell
# ════════════════════════════════════════════════════════════════════════════
BANNER = [
    "███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗",
    "████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝",
    "██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗",
    "██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║",
    "██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║",
    "╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
]

HELP = """\
  search <query> [-n N]    search YouTube, 100 results by default (max 100)
  info <n>                 full details for result n (date, likes, description)
  get <n|url> [fmt] [q]    send to yt-dlp workers. fmt: mp4 mkv webm mp3 m4a opus flac wav
                           q: max height (video) or kbps (audio) or 'best'.  e.g. get 3 mp3 320
  format [fmt] [q]         show / set default format        e.g. format mp4 720
  jobs                     list downloads         cancel <id> | retry <id>
  debug on|off             live debugger echo     debug level <DEBUG|INFO|WARN|ERROR>
  debug tail [n]           last log lines         debug trace   last error + traceback
  debug stats|dump [file]  counters / write the log buffer to a file
  rainbow on|off           rainbow result titles
  dir [path]               show / change download folder (created if missing)
  clear | help | quit"""


class NexusShell:
    def __init__(self, backend: Backend, animate: bool = True):
        self.be = backend
        self.animate = animate and sys.stdout.isatty() and Style.enabled
        self.results: list[VideoMeta] = []
        self.rainbow_titles = True
        self._seen_state: dict[int, str] = {}
        self.be.subscribe(self._on_event)

    # -- chrome ---------------------------------------------------------------
    def _chrome(self):
        dots = f"{red('●')} {yellow('●')} {green('●')}"
        print(f" {dots}   {grey('nexus@backend: ~/yt  —  80x24')}")
        print(grey(" " + "─" * 70))

    def banner(self):
        if Style.enabled and sys.stdout.isatty():
            os.system("") if os.name == "nt" else None  # enables ANSI on Windows consoles
        self._chrome()
        frames = 36 if self.animate else 1
        for f in range(frames):
            if f:
                sys.stdout.write(f"\x1b[{len(BANNER)}A")
            for row, line in enumerate(BANNER):
                print("  " + rainbow(line, offset=f * 0.03 + row * 0.05, spread=0.018))
            if self.animate:
                sys.stdout.flush()
                time.sleep(0.03)
        print("  " + rainbow("  C O N N E C T   •   E X P L O R E   •   B U I L D", 0.1, 0.02))
        print(dim("  backend shell · type 'help' for commands"))
        tag = green("✓ existing") if self.be.dir_status == "existing" else yellow("+ created")
        wp = self.be.workers
        qual = "" if wp.quality is None else f" @ {'best' if wp.quality == 0 else wp.quality}"
        print(f"  {grey('folder')} {self.be.download_dir}  {tag}   {grey('format')} {wp.fmt}{qual}\n")
        if yt_dlp is None:
            print(red("  ! yt-dlp not installed — run: pip install -U yt-dlp\n"))

    def prompt(self) -> str:
        active = sum(1 for j in self.be.jobs() if j.state in ACTIVE)
        text = (f"{green('nexus')}{grey('@')}{cyan('yt')}{grey(':')}{Style.paint('~', '34')}"
                f"{yellow(f' [{active}]') if active else ''}{grey('$')} ")
        if HAS_READLINE:  # tell readline the escape codes have zero width
            text = re.sub(r"(\x1b\[[0-9;]*m)", "\x01\\1\x02", text)
        return text

    # -- events ---------------------------------------------------------------
    def _on_event(self, name: str, data: dict):
        if name == "job.updated":
            job: Job = data["job"]
            key = f"{job.state.value}:{job.attempts}"
            if self._seen_state.get(job.id) == key:
                return
            self._seen_state[job.id] = key
            colour = {"done": green, "failed": red, "retrying": yellow, "cancelled": grey}.get(job.state.value, cyan)
            extra = ""
            if job.state == JobState.RETRYING:
                extra = f" — {job.error} (retry in {max(0, (job.retry_at or 0) - time.time()):.0f}s)"
            elif job.state == JobState.FAILED:
                extra = f" — {job.error}"
            elif job.state == JobState.DONE:
                extra = f" → {job.filepath}"
            self._async_print(colour(f"[job {job.id}] {job.state.value}{extra}"))

    @staticmethod
    def _async_print(line: str):
        sys.stdout.write("\r\x1b[2K" + line + "\n")
        sys.stdout.flush()

    # -- command loop ---------------------------------------------------------
    def run(self):
        self.banner()
        while True:
            try:
                raw = input(self.prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                print(red(f"parse error: {exc}"))
                continue
            cmd, args = parts[0].lower(), parts[1:]
            if cmd in ("quit", "exit", "q"):
                break
            handler = getattr(self, f"cmd_{cmd}", None)
            if not handler:
                print(red(f"unknown command: {cmd}") + dim("  (try 'help')"))
                continue
            try:
                handler(args)
            except FetchError as exc:
                print(red(f"✗ {exc}") + dim(f"  [{exc.kind}, {exc.attempts} attempt(s)]"))
            except MissingDependency as exc:
                print(red(f"✗ {exc}"))
            except (ValueError, IndexError) as exc:
                print(red(f"✗ {exc}"))
            except Exception as exc:
                self.be.debugger.error("shell", f"command '{cmd}' crashed", exc=exc)
                print(red(f"✗ internal error: {clean_message(exc)}") + dim("  (debug trace)"))
        print(dim("closing workers…"))
        self.be.close()

    # -- commands -------------------------------------------------------------
    def cmd_help(self, _): print(HELP)

    def cmd_clear(self, _):
        os.system("cls" if os.name == "nt" else "clear")
        self.banner()

    def cmd_search(self, args):
        limit = MAX_RESULTS
        if "-n" in args:
            i = args.index("-n")
            limit = int(args[i + 1])
            args = args[:i] + args[i + 2:]
        query = " ".join(args)
        t0 = time.time()
        print(dim(f"searching '{query}' …"))
        self.results = self.be.search(query, limit)
        print(dim(f"{len(self.results)} results in {time.time() - t0:.1f}s\n"))
        for i, m in enumerate(self.results, 1):
            title = rainbow(m.title, i * 0.07) if self.rainbow_titles else bold(m.title)
            print(f" {cyan(f'{i:>2}')}  {title}")
            print(f"     {grey(' · '.join([m.channel or '?', m.duration_str, fmt_count(m.views) + ' views', fmt_date(m.upload_date)]))}")
            print(f"     {dim(m.url)}")

    def _resolve(self, token: str) -> VideoMeta:
        if not self.results:
            raise ValueError("no results yet — run 'search' first")
        n = int(token)
        if not 1 <= n <= len(self.results):
            raise ValueError(f"no result #{n} (have 1-{len(self.results)})")
        return self.results[n - 1]

    def cmd_info(self, args):
        if not args:
            raise ValueError("usage: info <n>")
        m = self.be.details(self._resolve(args[0]))
        print(" " + (rainbow(m.title) if self.rainbow_titles else bold(m.title)))
        rows = [("channel", m.channel), ("duration", m.duration_str), ("views", fmt_count(m.views)),
                ("likes", fmt_count(m.like_count)), ("uploaded", fmt_date(m.upload_date)),
                ("url", m.url), ("thumbnail", m.thumbnail_path or m.thumbnail)]
        for k, v in rows:
            print(f"  {grey(f'{k:<10}')} {v}")
        if m.description:
            print("\n  " + dim(m.description[:400].replace("\n", "\n  ") + ("…" if len(m.description) > 400 else "")))

    def cmd_get(self, args):
        if not args:
            raise ValueError("usage: get <n|url> [format] [quality]")
        fmt, quality = None, None
        for a in args[1:]:
            if a.isdigit() or a.lower() == "best":
                quality = parse_quality(a)
            else:
                fmt = a
        target = args[0] if YT_URL_RE.match(args[0]) else self._resolve(args[0])
        job = self.be.download(target, fmt, quality)
        q = "" if job.quality is None else f" @ {'best' if job.quality == 0 else job.quality}"
        print(cyan(f"queued job {job.id} ({job.fmt}{q})") + dim(f"  {job.title}"))

    def cmd_format(self, args):
        wp = self.be.workers
        if args:
            fmt = args[0]
            quality = parse_quality(args[1]) if len(args) > 1 else None
            self.be.set_format(fmt, quality)
        q = "" if wp.quality is None else f" @ {'best' if wp.quality == 0 else wp.quality}"
        print(f"default format: {wp.fmt}{q}" + dim(f"   (video: {', '.join(VIDEO_FORMATS)} | audio: {', '.join(AUDIO_FORMATS)})"))

    def cmd_jobs(self, _):
        jobs = self.be.jobs()
        if not jobs:
            print(dim("no jobs"))
            return
        for j in jobs:
            bar_w = 20
            filled = int(j.progress * bar_w)
            bar = rainbow("█" * filled, 0.0, 0.05) + grey("░" * (bar_w - filled))
            colour = {"done": green, "failed": red, "retrying": yellow, "cancelled": grey}.get(j.state.value, cyan)
            speed = f" {fmt_size(j.speed)}/s" if j.speed else ""
            eta = f" eta {j.eta}s" if j.eta else ""
            print(f" {cyan(f'{j.id:>2}')} {colour(f'{j.state.value:<9}')} {dim(f'{j.fmt:<4}')} {bar} {j.progress * 100:5.1f}%{speed}{eta}"
                  f"  {dim(f'try {j.attempts}/{j.max_attempts}')}  {j.title[:40]}")
            if j.error and j.state in (JobState.FAILED, JobState.RETRYING):
                print(f"      {red(j.error[:100])}")

    def cmd_cancel(self, args):
        print(green("cancelling") if self.be.cancel(int(args[0])) else yellow("nothing to cancel"))

    def cmd_retry(self, args):
        print(green("re-queued") if self.be.retry(int(args[0])) else yellow("job is not failed/cancelled"))

    def cmd_rainbow(self, args):
        self.rainbow_titles = not args or args[0] != "off"
        print(rainbow("rainbow titles on") if self.rainbow_titles else dim("rainbow titles off"))

    def cmd_dir(self, args):
        if args:
            path, status = self.be.set_dir(args[0])
            print((green("✓ existing: ") if status == "existing" else yellow("+ created: ")) + str(path))
        else:
            print(self.be.download_dir)

    def cmd_debug(self, args):
        dbg = self.be.debugger
        sub = args[0].lower() if args else "tail"
        if sub in ("on", "off"):
            dbg.live = sub == "on"
            print(f"live debug {'on' if dbg.live else 'off'} (level {dbg.live_level})")
        elif sub == "level":
            lvl = args[1].upper()
            if lvl not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            dbg.live_level = lvl
            print(f"live level = {lvl}")
        elif sub == "tail":
            for rec in dbg.tail(int(args[1]) if len(args) > 1 else 25):
                print(rec.format())
        elif sub == "trace":
            rec = dbg.last_error
            print(rec.format() + "\n" + (rec.tb or "(no traceback captured)") if rec else dim("no errors recorded"))
        elif sub == "stats":
            print("  ".join(f"{k}={dbg.counts.get(k, 0)}" for k in LEVELS))
        elif sub == "dump":
            print(dbg.dump(Path(args[1] if len(args) > 1 else "nexus-debug-dump.txt")))
        else:
            raise ValueError("debug on|off|level|tail|trace|stats|dump")


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="NEXUS backend: YouTube search + yt-dlp workers")
    ap.add_argument("-d", "--dir", type=Path, metavar="DIR",
                    help="download folder; used if it exists, created if not (default ~/Downloads/YouRip)")
    ap.add_argument("-f", "--format", default="mp4", choices=list(ALL_FORMATS) + ["video", "audio"],
                    help="output format (default mp4)")
    ap.add_argument("-q", "--quality", metavar="Q",
                    help="video: max height (2160/1440/1080/720/480/360) | audio: kbps (128/192/320) | best")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--retries", type=int, default=4, help="max attempts per URL")
    ap.add_argument("--debug", action="store_true", help="live debugger echo on stderr")
    ap.add_argument("--no-anim", action="store_true", help="skip the banner animation")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--search", metavar="QUERY", help="non-interactive: print results as JSON and exit")
    ap.add_argument("-n", type=int, default=MAX_RESULTS, help="result count for --search (max 100)")
    ns = ap.parse_args(argv)

    if ns.no_color:
        Style.enabled = False
    try:
        be = Backend(download_dir=ns.dir, workers=ns.workers, policy=RetryPolicy(max_attempts=ns.retries),
                     debug=ns.debug, fmt=ns.format, quality=parse_quality(ns.quality))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if ns.search:
            print(json.dumps([m.to_dict() for m in be.search(ns.search, ns.n)], indent=2, ensure_ascii=False))
            return 0
        NexusShell(be, animate=not ns.no_anim).run()
        return 0
    except (FetchError, MissingDependency) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        be.close()


if __name__ == "__main__":
    sys.exit(main())
