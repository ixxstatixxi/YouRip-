#!/usr/bin/env python3
"""
NEXUS ghost window
==================
A frameless, translucent, always-on-top floating player. It fades to a "ghost" when the mouse
is away and solidifies when you hover it.

It normally runs as a child process of backend.py (GhostEngine), which sends one JSON object
per line on stdin and reads state events from stdout:

  in : {"cmd":"load","url":...,"title":...,"thumbnail":path|null,"kind":"video|audio","duration":sec}
       {"cmd":"art","thumbnail":path}   (cover arrived after load)
       {"cmd":"pause"} {"cmd":"resume"} {"cmd":"toggle"} {"cmd":"stop"} {"cmd":"quit"}
       {"cmd":"seek","seconds":90,"relative":false}   {"cmd":"volume","value":0-100}
       {"cmd":"window","opacity":0.35,"clickthrough":true,"visible":true,"ontop":true,
                       "move":[x,y],"size":[w,h]}
  out: {"event":"state","state":"loading|playing|paused|stopped|ended|error",...}
       {"event":"position","position":sec,"duration":sec}   {"event":"closed"}

Standalone test (no backend needed):   python ghost_window.py --url <direct media URL>

Mouse: drag anywhere to move | double-click = play/pause | wheel = volume |
       click the bar to seek | right-click = menu (opacity, click-through, always on top, close)

Requires: pip install PySide6   (QtMultimedia is included)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def send(**event):
    print(json.dumps(event), flush=True)


try:
    from PySide6.QtCore import QPropertyAnimation, QRectF, Qt, QThread, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor, QCursor, QFontMetrics, QLinearGradient, QPainter, QPen, QPixmap
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QApplication, QLabel, QMenu, QStackedLayout, QVBoxLayout, QWidget
except ImportError as exc:  # the backend relays this to the shell
    send(event="fatal", error=f"PySide6 with QtMultimedia is required (pip install PySide6): {exc}")
    sys.exit(2)

TEXT, MUTED = "#eaf4ff", "#8294aa"
IDLE_OPACITY, HOVER_OPACITY = 0.35, 0.96


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class StdinReader(QThread):
    """Blocking reader for backend commands; hands each parsed line to the GUI thread."""
    message = Signal(object)

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                self.message.emit(json.loads(line))
            except ValueError:
                send(event="error", error=f"bad JSON from backend: {line[:120]}")
        self.message.emit({"cmd": "quit"})  # backend went away -> close the window


class ProgressBar(QWidget):
    seekRequested = Signal(float)  # 0..1

    def __init__(self):
        super().__init__()
        self.fraction = 0.0
        self.setFixedHeight(12)
        self.setCursor(Qt.PointingHandCursor)

    def set_fraction(self, value: float):
        self.fraction = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0, self.height() / 2 - 2, self.width(), 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 45))
        p.drawRoundedRect(track, 2, 2)
        rainbow = QLinearGradient(track.left(), 0, track.right(), 0)
        for i in range(7):
            rainbow.setColorAt(i / 6, QColor.fromHsvF(i / 7 * 0.85, 0.75, 1.0))
        p.setBrush(rainbow)
        p.drawRoundedRect(QRectF(track.x(), track.y(), track.width() * self.fraction, track.height()), 2, 2)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.width() > 0:
            self.seekRequested.emit(event.position().x() / self.width())


class Ghost(QWidget):
    def __init__(self, size=(400, 262)):
        super().__init__()
        self.setWindowTitle("NEXUS Ghost")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.resize(*size)

        self.idle_opacity, self.hover_opacity = IDLE_OPACITY, HOVER_OPACITY
        self._hover, self._drag, self._closing = False, None, False
        self._title, self._kind, self._duration_hint = "NEXUS ghost", "video", 0.0
        self._art: QPixmap | None = None
        self._last_pos_emit = 0.0

        # --- media -----------------------------------------------------------
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.player.setVideoOutput(self.video)

        # --- layout: [video | cover art] / title / progress / time -------------
        self.art = QLabel()
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.stack = QStackedLayout()
        self.stack.addWidget(self.video)  # 0
        self.stack.addWidget(self.art)    # 1
        stage = QWidget()
        stage.setLayout(self.stack)
        stage.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.title_label = QLabel(self._title)
        self.title_label.setStyleSheet(f"color:{TEXT}; font-size:12px; font-weight:600; background:transparent;")
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setStyleSheet(f"color:{MUTED}; font-size:10px; background:transparent;")
        for w in (self.title_label, self.time_label):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.bar = ProgressBar()
        self.bar.seekRequested.connect(self._seek_fraction)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(5)
        layout.addWidget(stage, 1)
        layout.addWidget(self.title_label)
        layout.addWidget(self.bar)
        layout.addWidget(self.time_label)

        # --- player signals ----------------------------------------------------
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda _ms: self._on_position(self.player.position()))
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

        # --- ghost fade: poll the cursor (works even when click-through) ---------
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setDuration(180)
        self.setWindowOpacity(self.idle_opacity)
        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self._poll_hover)
        self.hover_timer.start(120)

    # ------------------------------------------------------------------ helpers
    def _state_event(self, state: str, **extra):
        send(event="state", state=state, position=self.player.position() / 1000,
             duration=self._duration(), title=self._title, **extra)

    def _duration(self) -> float:
        return (self.player.duration() / 1000) or self._duration_hint

    def _fade_to(self, value: float):
        self.fade.stop()
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(value)
        self.fade.start()

    def _poll_hover(self):
        inside = self.isVisible() and self.frameGeometry().contains(QCursor.pos())
        if inside != self._hover:
            self._hover = inside
            self._fade_to(self.hover_opacity if inside else self.idle_opacity)

    def _set_flag(self, flag, on: bool):
        visible = self.isVisible()
        self.setWindowFlag(flag, on)  # changing flags hides the window
        if visible:
            self.show()

    def _update_art(self):
        if self._art and not self._art.isNull():
            self.art.setPixmap(self._art.scaled(self.art.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.art.clear()

    # ------------------------------------------------------------ player events
    def _on_position(self, ms: int):
        dur = self._duration()
        self.bar.set_fraction((ms / 1000) / dur if dur else 0.0)
        self.time_label.setText(f"{fmt_time(ms / 1000)} / {fmt_time(dur)}")
        now = time.time()
        if now - self._last_pos_emit >= 0.5:
            self._last_pos_emit = now
            send(event="position", position=ms / 1000, duration=dur)

    def _on_state(self, state):
        S = QMediaPlayer.PlaybackState
        name = {S.PlayingState: "playing", S.PausedState: "paused", S.StoppedState: "stopped"}.get(state, "stopped")
        self._state_event(name)

    def _on_status(self, status):
        M = QMediaPlayer.MediaStatus
        if status == M.EndOfMedia:
            self._state_event("ended")
        elif status == M.LoadingMedia:
            self._state_event("loading")
        elif status == M.InvalidMedia:
            self._state_event("error", error="invalid or unsupported media")

    def _on_error(self, _error, message=""):
        self._state_event("error", error=message or "playback error")

    # ----------------------------------------------------------------- commands
    def load(self, msg: dict):
        self.player.stop()
        self._title = msg.get("title") or msg.get("url", "")
        self._kind = msg.get("kind", "video")
        self._duration_hint = float(msg.get("duration") or 0)
        fm = QFontMetrics(self.title_label.font())
        self.title_label.setText(fm.elidedText(self._title, Qt.ElideRight, max(120, self.width() - 30)))
        self.set_art(msg.get("thumbnail"))
        if not (self._kind == "audio" and self._art):
            self.stack.setCurrentIndex(0)
        self.player.setSource(QUrl(msg["url"]))
        if msg.get("autoplay", True):
            self.player.play()
        if not self.isVisible():
            self.show()
        self._state_event("loading")

    def set_art(self, path):
        self._art = QPixmap(path) if path and Path(path).exists() else None
        self._update_art()
        if self._kind == "audio" and self._art:
            self.stack.setCurrentIndex(1)

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, seconds: float, relative: bool):
        target = self.player.position() / 1000 + seconds if relative else seconds
        dur = self._duration()
        if dur:
            target = min(target, dur)
        self.player.setPosition(int(max(0.0, target) * 1000))

    def _seek_fraction(self, fraction: float):
        dur = self._duration()
        if dur:
            self.seek(max(0.0, min(1.0, fraction)) * dur, relative=False)

    def window_opts(self, o: dict):
        if "opacity" in o:
            self.idle_opacity = max(0.05, min(1.0, float(o["opacity"])))
            self.hover_opacity = max(self.idle_opacity, HOVER_OPACITY)
            self._fade_to(self.hover_opacity if self._hover else self.idle_opacity)
        if "clickthrough" in o:
            self._set_flag(Qt.WindowTransparentForInput, bool(o["clickthrough"]))
        if "ontop" in o:
            self._set_flag(Qt.WindowStaysOnTopHint, bool(o["ontop"]))
        if "size" in o:
            self.resize(int(o["size"][0]), int(o["size"][1]))
        if "move" in o:
            self.move(int(o["move"][0]), int(o["move"][1]))
        if "visible" in o:
            self.show() if o["visible"] else self.hide()

    def handle(self, msg: object):
        cmd = None
        try:
            if not isinstance(msg, dict):
                raise TypeError(f"expected a JSON object, got {type(msg).__name__}")
            cmd = msg.get("cmd")
            if cmd == "load":
                self.load(msg)
            elif cmd == "pause":
                self.player.pause()
            elif cmd == "resume":
                self.player.play()
            elif cmd == "toggle":
                self.toggle()
            elif cmd == "stop":
                self.player.stop()
            elif cmd == "seek":
                self.seek(float(msg["seconds"]), bool(msg.get("relative")))
            elif cmd == "volume":
                self.audio.setVolume(max(0.0, min(100.0, float(msg["value"]))) / 100.0)
            elif cmd == "art":
                self.set_art(msg.get("thumbnail"))
            elif cmd == "window":
                self.window_opts(msg)
            elif cmd == "quit":
                self.quit()
            else:
                send(event="error", error=f"unknown command: {cmd}")
        except Exception as exc:  # never let one bad message kill the window
            send(event="error", error=f"{cmd}: {exc}")

    def quit(self):
        if self._closing:
            return
        self._closing = True
        self.player.stop()
        send(event="closed")
        QApplication.quit()

    # ------------------------------------------------------------------- window
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(8, 13, 22, 215))
        p.setPen(QPen(QColor(66, 217, 255, 120), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_art()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _event):
        self._drag = None

    def mouseDoubleClickEvent(self, _event):
        self.toggle()

    def wheelEvent(self, event):
        step = 5 if event.angleDelta().y() > 0 else -5
        self.audio.setVolume(max(0.0, min(1.0, self.audio.volume() + step / 100)))

    def contextMenuEvent(self, event):
        menu = QMenu(self)

        def add(text, fn, checked=None):
            action = menu.addAction(text)
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(checked)
            action.triggered.connect(lambda *_: fn())

        add("Play / pause", self.toggle)
        for pct in (20, 35, 60, 100):
            add(f"Ghost opacity {pct}%", lambda v=pct: self.window_opts({"opacity": v / 100}))
        add("Always on top", lambda: self._set_flag(Qt.WindowStaysOnTopHint,
                                                    not bool(self.windowFlags() & Qt.WindowStaysOnTopHint)),
            checked=bool(self.windowFlags() & Qt.WindowStaysOnTopHint))
        add("Click-through (undo from shell: ghost click off)",
            lambda: self.window_opts({"clickthrough": True}))
        menu.addSeparator()
        add("Hide", self.hide)
        add("Close", self.quit)
        menu.exec(event.globalPos())

    def closeEvent(self, event):
        self.quit()
        event.accept()


def main() -> int:
    ap = argparse.ArgumentParser(description="NEXUS ghost window")
    ap.add_argument("--url", help="play this media URL immediately (standalone test)")
    ap.add_argument("--title", default="")
    ns = ap.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # hiding the window must not end the process
    ghost = Ghost()
    reader = StdinReader()
    reader.message.connect(ghost.handle)
    reader.start()
    send(event="ready")
    if ns.url:
        ghost.handle({"cmd": "load", "url": ns.url, "title": ns.title or ns.url})
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
