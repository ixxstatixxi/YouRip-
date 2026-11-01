"""Small floating media player controlled by newline-delimited JSON on stdin."""
from __future__ import annotations

import json
import sys

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


class StdinReader(QThread):
    command = Signal(object)

    def run(self):
        for line in sys.stdin:
            try:
                self.command.emit(json.loads(line))
            except (ValueError, TypeError):
                print(json.dumps({"event": "error", "error": "Invalid command JSON."}), flush=True)
        self.command.emit({"cmd": "quit"})


class GhostWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NEXUS Ghost")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(420, 290)
        self._drag_offset = None

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.player.setVideoOutput(self.video)
        self.title = QLabel("NEXUS Ghost")
        self.title.setStyleSheet("color:#eaf4ff; padding:6px; background:#101a29;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.title)
        self.player.playbackStateChanged.connect(self._state_changed)

    def _state_changed(self, state):
        playback = QMediaPlayer.PlaybackState
        name = "playing" if state == playback.PlayingState else "paused" if state == playback.PausedState else "stopped"
        print(json.dumps({"event": "state", "state": name}), flush=True)

    def handle(self, command):
        if not isinstance(command, dict):
            return
        action = command.get("cmd")
        if action == "load":
            self.title.setText(command.get("title") or "NEXUS Ghost")
            self.player.setSource(QUrl(command["url"]))
            self.show()
            self.player.play()
        elif action == "toggle":
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
        elif action == "pause":
            self.player.pause()
        elif action == "resume":
            self.player.play()
        elif action == "stop":
            self.player.stop()
        elif action == "volume":
            self.audio.setVolume(max(0.0, min(100.0, float(command["value"]))) / 100)
        elif action == "quit":
            self.player.stop()
            QApplication.quit()
        elif action == "window":
            if "opacity" in command:
                self.setWindowOpacity(max(0.05, min(1.0, float(command["opacity"]))))
            if "size" in command:
                self.resize(*map(int, command["size"]))
            if "move" in command:
                self.move(*map(int, command["move"]))
            if "visible" in command:
                self.setVisible(bool(command["visible"]))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setBrush(QColor(8, 13, 22, 225))
        painter.setPen(QPen(QColor(66, 217, 255, 150), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == Qt.RightButton:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _event):
        self._drag_offset = None

    def wheelEvent(self, event):
        delta = 0.05 if event.angleDelta().y() > 0 else -0.05
        self.audio.setVolume(max(0.0, min(1.0, self.audio.volume() + delta)))


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = GhostWindow()
    reader = StdinReader()
    reader.command.connect(window.handle)
    reader.start()
    print(json.dumps({"event": "ready"}), flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
