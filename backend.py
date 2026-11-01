"""NEXUS media backend: YouTube search, downloads, and playback URL resolution."""
from __future__ import annotations

import concurrent.futures
import enum
import itertools
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def require_ytdlp():
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is required. Install project dependencies with: pip install -r requirements.txt")


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / "YouRip"


def prepare_dir(path: Path | str | None = None) -> Path:
    directory = Path(path).expanduser() if path else default_download_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        raise ValueError(f"{directory} is not a writable directory")
    return directory.resolve()


@dataclass
class VideoMeta:
    id: str
    title: str
    url: str
    channel: str = ""
    duration: Optional[int] = None
    views: Optional[int] = None
    thumbnail: str = ""

    @classmethod
    def from_ydl(cls, info: dict) -> Optional["VideoMeta"]:
        video_id = info.get("id")
        if not video_id:
            return None
        url = info.get("webpage_url") or info.get("url") or ""
        if not url.startswith(("http://", "https://")):
            url = f"https://www.youtube.com/watch?v={video_id}"
        return cls(
            id=str(video_id),
            title=info.get("title") or "(untitled)",
            url=url,
            channel=info.get("channel") or info.get("uploader") or "",
            duration=int(info["duration"]) if info.get("duration") else None,
            views=info.get("view_count"),
            thumbnail=info.get("thumbnail") or "",
        )

    @property
    def duration_str(self) -> str:
        if self.duration is None:
            return "--:--"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadJob:
    id: int
    meta: VideoMeta
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    filepath: Optional[str] = None
    error: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)


class Backend:
    """Thread-safe facade used by the desktop interface."""

    def __init__(self, download_dir: Path | str | None = None, workers: int = 2):
        self.download_dir = prepare_dir(download_dir)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers + 1), thread_name_prefix="nexus"
        )
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[str, dict], None]] = []
        self._jobs: dict[int, DownloadJob] = {}
        self._ids = itertools.count(1)

    def subscribe(self, callback: Callable[[str, dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def _emit(self, name: str, **data):
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(name, data)
            except Exception:
                logging.getLogger(__name__).exception("Backend event subscriber failed: %s", name)

    @staticmethod
    def _ydl_options(**options) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            **options,
        }

    def search(self, query: str, limit: int = 50) -> list[VideoMeta]:
        require_ytdlp()
        query = query.strip()
        if not query:
            raise ValueError("Enter a search term.")
        limit = max(1, min(int(limit), 100))
        target = query if query.startswith(("http://", "https://")) else f"ytsearch{limit}:{query}"
        with yt_dlp.YoutubeDL(self._ydl_options(extract_flat="in_playlist", skip_download=True)) as ydl:
            result = ydl.extract_info(target, download=False) or {}
        entries = result.get("entries") or [result]
        return [meta for entry in entries if entry
                for meta in [VideoMeta.from_ydl(entry)] if meta][:limit]

    def search_async(self, query: str, limit: int = 50):
        def run():
            try:
                self._emit("search.done", query=query, results=self.search(query, limit))
            except Exception as exc:
                self._emit("search.error", query=query, error=str(exc))
        return self._pool.submit(run)

    def download(self, meta: VideoMeta) -> DownloadJob:
        require_ytdlp()
        with self._lock:
            job = DownloadJob(next(self._ids), meta)
            self._jobs[job.id] = job
        self._pool.submit(self._download, job)
        return job

    def _download(self, job: DownloadJob):
        if job.cancel.is_set():
            job.state = JobState.CANCELLED
            self._emit("job.updated", job=job)
            return
        job.state = JobState.RUNNING
        self._emit("job.updated", job=job)

        def progress(data: dict):
            if job.cancel.is_set():
                raise InterruptedError("Download cancelled.")
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                if total:
                    job.progress = min(0.99, (data.get("downloaded_bytes") or 0) / total)
                self._emit("job.progress", job=job)

        options = self._ydl_options(
            outtmpl=str(self.download_dir / "%(title).150B [%(id)s].%(ext)s"),
            progress_hooks=[progress],
            continuedl=True,
        )
        if shutil.which("ffmpeg"):
            options.update(format="bv*+ba/b", merge_output_format="mp4")
        else:
            options["format"] = "b[ext=mp4]/b"
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(job.meta.url, download=True)
                downloads = (info or {}).get("requested_downloads") or []
                job.filepath = (downloads[0].get("filepath") if downloads else None) or ydl.prepare_filename(info)
            job.progress = 1.0
            job.state = JobState.DONE
        except InterruptedError:
            job.state = JobState.CANCELLED
        except Exception as exc:
            job.state, job.error = JobState.FAILED, str(exc)
        self._emit("job.updated", job=job)

    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or job.state not in (JobState.QUEUED, JobState.RUNNING):
            return False
        job.cancel.set()
        if job.state == JobState.QUEUED:
            job.state = JobState.CANCELLED
            self._emit("job.updated", job=job)
        return True

    def play_async(self, meta: VideoMeta):
        def run():
            try:
                require_ytdlp()
                with yt_dlp.YoutubeDL(self._ydl_options(skip_download=True)) as ydl:
                    info = ydl.extract_info(meta.url, download=False)
                formats = (info or {}).get("formats") or []
                playable = next(
                    (item for item in reversed(formats)
                     if item.get("url") and item.get("vcodec") != "none"
                     and item.get("acodec") != "none"),
                    None,
                )
                if not playable:
                    playable = next((item for item in formats if item.get("url")), None)
                url = (playable or {}).get("url") or (info or {}).get("url")
                if not url:
                    raise RuntimeError("No playable media stream was found.")
                kind = "audio" if (playable or {}).get("vcodec") == "none" else "video"
                self._emit("playback.ready", url=url, title=meta.title, kind=kind)
            except Exception as exc:
                self._emit("playback.error", error=str(exc))
        return self._pool.submit(run)

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
