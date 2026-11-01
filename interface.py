"""NEXUS desktop interface wired to the media backend and floating player."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QPushButton, QProgressBar, QSplitter, QVBoxLayout, QWidget,
)

from backend import Backend, DownloadJob, JobState, VideoMeta


BG, PANEL, CYAN, MUTED, TEXT = "#080d16", "#101a29", "#42d9ff", "#8294aa", "#eaf4ff"


class LogoAnimation(QWidget):
    def __init__(self):
        super().__init__()
        self.phase = 0.0
        self.setMinimumHeight(220)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.timer.start(16)

    def _advance(self):
        self.phase += 0.025
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = self.rect().center()
        radius = min(self.width(), self.height()) * 0.25
        painter.save()
        painter.translate(center)
        painter.rotate(self.phase * 35)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(66, 217, 255, 100), 2))
        painter.drawEllipse(-radius * 1.5, -radius * 0.5, radius * 3, radius)
        painter.rotate(-self.phase * 70)
        painter.setPen(QPen(QColor(66, 217, 255, 65), 2))
        painter.drawEllipse(-radius * 1.4, -radius * 0.45, radius * 2.8, radius * 0.9)
        painter.rotate(self.phase * 35)
        painter.setPen(QPen(QColor(66, 217, 255, 190), 2))
        painter.drawRect(-radius * 0.48, -radius * 0.48, radius * 0.96, radius * 0.96)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(CYAN))
        painter.drawEllipse(-4, -4, 8, 8)
        painter.restore()
        painter.end()


class IntroWindow(QWidget):
    """Three-second animated startup screen."""

    def __init__(self, on_finished=None):
        super().__init__()
        self.on_finished = on_finished
        self.setWindowTitle("NEXUS")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setFixedSize(760, 520)
        self.setStyleSheet(f"background:{BG}; color:{TEXT};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(35, 25, 35, 25)
        layout.addWidget(QLabel("N E X U S   /   SYSTEM INITIALIZATION"), alignment=Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(LogoAnimation(), 1)
        title = QLabel("NEXUS")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Segoe UI", 27, QFont.Bold))
        title.setStyleSheet(f"color:{TEXT}; letter-spacing:8px;")
        layout.addWidget(title)
        subtitle = QLabel("CONNECT  •  EXPLORE  •  BUILD")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(f"color:{CYAN}; font-size:10px; letter-spacing:3px;")
        layout.addWidget(subtitle)
        layout.addStretch(1)
        QTimer.singleShot(3000, self.finish_intro)

    def finish_intro(self):
        self.close()
        if callable(self.on_finished):
            self.on_finished()


class EventBridge(QObject):
    received = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NEXUS | YouRip")
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        self.backend = Backend()
        self.events = EventBridge(self)
        self.backend.subscribe(self.events.received.emit)
        self.events.received.connect(self._on_backend_event)
        self._player = QProcess(self)
        self._player.setProcessChannelMode(QProcess.SeparateChannels)
        self._player.readyReadStandardOutput.connect(self._read_player_events)
        self._pending_play: dict | None = None
        self._metas: list[VideoMeta] = []
        self._jobs: dict[int, QListWidgetItem] = {}
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; }}
            QLineEdit, QListWidget {{ background:{PANEL}; border:1px solid #1b3045;
                border-radius:6px; padding:8px; }}
            QPushButton {{ background:#142337; border:1px solid #25415c; border-radius:6px;
                padding:8px 14px; color:{TEXT}; }}
            QPushButton:hover {{ border-color:{CYAN}; }}
            QProgressBar {{ border:0; background:#1b2a3d; height:5px; }}
            QProgressBar::chunk {{ background:{CYAN}; }}
        """)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(26, 22, 26, 20)
        header = QHBoxLayout()
        brand = QLabel("NEXUS")
        brand.setFont(QFont("Segoe UI", 22, QFont.Bold))
        brand.setStyleSheet(f"color:{CYAN}; letter-spacing:4px;")
        header.addWidget(brand)
        header.addWidget(QLabel("YOUTUBE MEDIA WORKSPACE"))
        header.addStretch(1)
        self.status = QLabel("Ready")
        self.status.setStyleSheet(f"color:{MUTED};")
        header.addWidget(self.status)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search YouTube or paste a video URL")
        self.query.returnPressed.connect(self._search)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.addWidget(QLabel("SEARCH RESULTS"))
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _item: self._play())
        left_layout.addWidget(self.results, 1)
        actions = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._play)
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self._download)
        actions.addWidget(self.play_button)
        actions.addWidget(self.download_button)
        left_layout.addLayout(actions)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(QLabel("DOWNLOAD QUEUE"))
        self.jobs = QListWidget()
        right_layout.addWidget(self.jobs, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        right_layout.addWidget(self.progress)
        self.cancel_button = QPushButton("Cancel selected download")
        self.cancel_button.clicked.connect(self._cancel)
        right_layout.addWidget(self.cancel_button)
        splitter.addWidget(right)
        splitter.setSizes([700, 400])
        layout.addWidget(splitter, 1)
        footer = QLabel(f"Downloads: {self.backend.download_dir}")
        footer.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        layout.addWidget(footer)
        self.setCentralWidget(root)

    def _search(self):
        query = self.query.text().strip()
        if not query:
            self.status.setText("Enter a search term or URL.")
            return
        self.search_button.setEnabled(False)
        self.status.setText("Searching…")
        self.backend.search_async(query)

    def _selected_meta(self) -> VideoMeta | None:
        row = self.results.currentRow()
        return self._metas[row] if 0 <= row < len(self._metas) else None

    def _download(self):
        meta = self._selected_meta()
        if not meta:
            self.status.setText("Select a search result first.")
            return
        try:
            job = self.backend.download(meta)
        except Exception as exc:
            self.status.setText(str(exc))
            return
        item = QListWidgetItem(f"#{job.id}  {meta.title} — queued")
        item.setData(Qt.UserRole, job.id)
        self.jobs.addItem(item)
        self._jobs[job.id] = item

    def _play(self):
        meta = self._selected_meta()
        if not meta:
            self.status.setText("Select a search result first.")
            return
        self.status.setText("Preparing playback…")
        self.backend.play_async(meta)

    def _start_player(self, details: dict):
        self._pending_play = details
        if self._player.state() == QProcess.NotRunning:
            script = str(Path(__file__).with_name("ghost_window.py"))
            self._player.start(sys.executable, [script])
        elif self._player.state() == QProcess.Running:
            self._send_player({"cmd": "load", **details})

    def _send_player(self, command: dict):
        if self._player.state() == QProcess.Running:
            self._player.write((json.dumps(command) + "\n").encode("utf-8"))

    def _read_player_events(self):
        for raw in bytes(self._player.readAllStandardOutput()).splitlines():
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if event.get("event") == "ready" and self._pending_play:
                self._send_player({"cmd": "load", **self._pending_play})
                self._pending_play = None
            elif event.get("event") == "error":
                self.status.setText(event.get("error", "Player error"))
            elif event.get("event") == "state":
                self.status.setText(f"Player: {event.get('state', 'unknown')}")

    def _on_backend_event(self, name: str, data: dict):
        if name == "search.done":
            self._metas = data["results"]
            self.results.clear()
            for meta in self._metas:
                line = f"{meta.title}\n{meta.channel or 'Unknown channel'}  ·  {meta.duration_str}"
                self.results.addItem(QListWidgetItem(line))
            self.status.setText(f"{len(self._metas)} result(s)")
            self.search_button.setEnabled(True)
        elif name == "search.error":
            self.status.setText(data["error"])
            self.search_button.setEnabled(True)
        elif name == "playback.ready":
            self._start_player({key: data[key] for key in ("url", "title", "kind")})
        elif name == "playback.error":
            self.status.setText(data["error"])
        elif name in ("job.updated", "job.progress"):
            self._update_job(data["job"])

    def _update_job(self, job: DownloadJob):
        item = self._jobs.get(job.id)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, job.id)
            self.jobs.addItem(item)
            self._jobs[job.id] = item
        item.setText(f"#{job.id}  {job.meta.title} — {job.state.value} ({job.progress:.0%})")
        if job.state == JobState.FAILED:
            item.setToolTip(job.error or "Download failed")
        elif job.state == JobState.DONE:
            item.setToolTip(job.filepath or "")
        if job.state in (JobState.RUNNING, JobState.QUEUED):
            self.progress.setValue(round(job.progress * 100))

    def _cancel(self):
        item = self.jobs.currentItem()
        if item and self.backend.cancel(int(item.data(Qt.UserRole))):
            self.status.setText("Cancellation requested.")

    def closeEvent(self, event):
        if self._player.state() == QProcess.Running:
            self._send_player({"cmd": "quit"})
            self._player.waitForFinished(1000)
        self.backend.close()
        event.accept()
