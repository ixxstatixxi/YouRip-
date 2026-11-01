"""
NEXUS desktop interface.

Contains:
- A 3-second animated startup screen (custom-painted 3D-style logo)
- A placeholder-only homepage
- No backend or feature functionality yet
"""
import math
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QApplication, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy, QGraphicsOpacityEffect
)


BG = "#080d16"
PANEL = "#101a29"
PANEL_ALT = "#0c1421"
CYAN = "#42d9ff"
MUTED = "#8294aa"
TEXT = "#eaf4ff"


class LogoAnimation(QWidget):
    """Animated wireframe cube and orbital rings, drawn without external assets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.start(16)
        self.setMinimumSize(360, 300)

    def advance(self):
        self.phase += 0.025
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(BG))

        cx = self.width() / 2
        cy = self.height() / 2 - 8
        scale = min(self.width(), self.height()) * 0.26
        angle = self.phase

        # Soft radial glow behind the mark.
        glow = QRadialGradientCompat(QPointF(cx, cy), scale * 2.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QPointF(cx, cy), scale * 2.5, scale * 2.5)

        # Three rotating elliptical orbital rings.
        painter.save()
        painter.translate(cx, cy)
        for index, tilt in enumerate((0.25, 1.05, 2.0)):
            painter.save()
            painter.rotate(math.degrees(angle * (1 if index != 1 else -0.7) + tilt))
            pen = QPen(QColor(66, 217, 255, 115 if index == 0 else 65), 1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            rect = QRectF(-scale * 1.65, -scale * 0.52, scale * 3.3, scale * 1.04)
            painter.drawEllipse(rect)
            painter.restore()
        painter.restore()

        # Rotating 3D-style cube.
        vertices = []
        for z in (-1, 1):
            for y in (-1, 1):
                for x in (-1, 1):
                    px = x * math.cos(angle) - z * math.sin(angle)
                    pz = x * math.sin(angle) + z * math.cos(angle)
                    py = y * math.cos(angle * 0.72) - pz * math.sin(angle * 0.72)
                    pz2 = y * math.sin(angle * 0.72) + pz * math.cos(angle * 0.72)
                    depth = 3.8 / (3.8 + pz2 * 0.35)
                    vertices.append(QPointF(cx + px * scale * depth,
                                            cy + py * scale * depth))

        edges = [(0,1),(0,2),(0,4),(1,3),(1,5),(2,3),
                 (2,6),(3,7),(4,5),(4,6),(5,7),(6,7)]
        for a, b in edges:
            alpha = 175 if (a + b) % 2 == 0 else 105
            painter.setPen(QPen(QColor(66, 217, 255, alpha), 2.0))
            painter.drawLine(vertices[a], vertices[b])

        # Bright central core.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(CYAN))
        painter.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        painter.end()


class QRadialGradientCompat:
    """Small helper returning a radial gradient with a restrained cyan glow."""
    def __new__(cls, center, radius):
        from PySide6.QtGui import QRadialGradient
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, QColor(35, 150, 205, 48))
        gradient.setColorAt(0.55, QColor(20, 85, 125, 18))
        gradient.setColorAt(1.0, QColor(8, 13, 22, 0))
        return gradient


class IntroWindow(QWidget):
    """Frameless startup intro; calls on_finished after approximately 3 seconds."""

    def __init__(self, on_finished=None):
        super().__init__()
        self.on_finished = on_finished
        self.setWindowTitle("NEXUS")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setFixedSize(760, 520)
        self.setStyleSheet(f"background:{BG}; color:{TEXT};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(35, 25, 35, 25)
        layout.setSpacing(4)

        top = QLabel("N E X U S   /   SYSTEM INITIALIZATION")
        top.setAlignment(Qt.AlignCenter)
        top.setStyleSheet(f"color:{MUTED}; font-size:10px; letter-spacing:3px;")
        layout.addWidget(top)

        layout.addStretch(1)
        self.logo = LogoAnimation()
        layout.addWidget(self.logo, 1)

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

        footer = QLabel("INITIALIZING INTERFACE")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet(f"color:{MUTED}; font-size:9px; letter-spacing:2px;")
        layout.addWidget(footer)

        QTimer.singleShot(3000, self.finish_intro)

    def finish_intro(self):
        self.close()
        if callable(self.on_finished):
            self.on_finished()


class PlaceholderCard(QFrame):
    def __init__(self, heading, description="MODULE PLACEHOLDER"):
        super().__init__()
        self.setObjectName("placeholderCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel(heading)
        title.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:600;")
        detail = QLabel(description)
        detail.setStyleSheet(f"color:{MUTED}; font-size:10px; letter-spacing:1px;")
        detail.setWordWrap(True)

        marker = QFrame()
        marker.setFixedSize(34, 3)
        marker.setStyleSheet(f"background:{CYAN}; border:0;")
        layout.addWidget(title)
        layout.addWidget(marker)
        layout.addWidget(detail)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NEXUS | Desktop")
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; }}
            QFrame#sidebar {{ background:{PANEL_ALT}; border-right:1px solid #1b2a3d; }}
            QFrame#placeholderCard {{
                background:{PANEL}; border:1px solid #1b3045; border-radius:10px;
            }}
            QLabel#navItem {{
                color:{MUTED}; padding:11px 13px; border-radius:6px;
                font-size:12px;
            }}
            QLabel#navItem:hover {{ color:{TEXT}; background:#142337; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 25, 18, 20)
        side.setSpacing(9)

        brand = QLabel("NEXUS")
        brand.setFont(QFont("Segoe UI", 19, QFont.Bold))
        brand.setStyleSheet(f"color:{CYAN}; letter-spacing:4px;")
        side.addWidget(brand)

        tagline = QLabel("DESKTOP ENVIRONMENT")
        tagline.setStyleSheet(f"color:{MUTED}; font-size:8px; letter-spacing:1.5px;")
        side.addWidget(tagline)
        side.addSpacing(28)

        for item in ("OVERVIEW", "WORKSPACE", "TOOLS", "SETTINGS"):
            label = QLabel(item)
            label.setObjectName("navItem")
            side.addWidget(label)

        side.addStretch(1)
        version = QLabel("NEXUS  /  BUILD 001")
        version.setStyleSheet(f"color:{MUTED}; font-size:9px;")
        side.addWidget(version)
        outer.addWidget(sidebar)

        content = QWidget()
        page = QVBoxLayout(content)
        page.setContentsMargins(34, 29, 34, 28)
        page.setSpacing(22)

        header_row = QHBoxLayout()
        header_text = QVBoxLayout()
        heading = QLabel("Welcome")
        heading.setFont(QFont("Segoe UI", 25, QFont.DemiBold))
        heading.setStyleSheet(f"color:{TEXT};")
        subheading = QLabel("Your workspace is ready to take shape.")
        subheading.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        header_text.addWidget(heading)
        header_text.addWidget(subheading)
        header_row.addLayout(header_text)
        header_row.addStretch(1)

        status = QLabel("●  SYSTEM READY")
        status.setStyleSheet(f"color:{CYAN}; font-size:10px; letter-spacing:1px;")
        header_row.addWidget(status, alignment=Qt.AlignVCenter)
        page.addLayout(header_row)

        hero = QFrame()
        hero.setStyleSheet(f"""
            QFrame {{ background:#0d1b2b; border:1px solid #1d4057;
                      border-radius:12px; }}
        """)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(25, 23, 25, 23)
        hero_title = QLabel("YOUR NEXT PROJECT STARTS HERE")
        hero_title.setStyleSheet(f"color:{CYAN}; font-size:10px; letter-spacing:2px;")
        hero_body = QLabel("A clean foundation. Room to build.")
        hero_body.setFont(QFont("Segoe UI", 19, QFont.DemiBold))
        hero_body.setStyleSheet(f"color:{TEXT};")
        hero_note = QLabel("Homepage content is intentionally left as placeholders.")
        hero_note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        hero_layout.addWidget(hero_title)
        hero_layout.addSpacing(5)
        hero_layout.addWidget(hero_body)
        hero_layout.addWidget(hero_note)
        page.addWidget(hero)

        section = QLabel("WORKSPACE MODULES")
        section.setStyleSheet(f"color:{MUTED}; font-size:10px; letter-spacing:2px;")
        page.addWidget(section)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        for title in ("Module One", "Module Two", "Module Three"):
            card = PlaceholderCard(title)
            card.setMinimumHeight(155)
            cards.addWidget(card)
        page.addLayout(cards)

        lower = QHBoxLayout()
        lower.setSpacing(14)
        for title in ("Recent Activity", "Quick Access"):
            card = PlaceholderCard(title, "CONTENT WILL BE ADDED LATER")
            card.setMinimumHeight(170)
            lower.addWidget(card)
        page.addLayout(lower)
        page.addStretch(1)

        footer = QLabel("NEXUS DESKTOP  •  MODULAR FOUNDATION")
        footer.setStyleSheet(f"color:#52657b; font-size:9px; letter-spacing:1px;")
        page.addWidget(footer)

        outer.addWidget(content, 1)
