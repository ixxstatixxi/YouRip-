"""NEXUS desktop application entry point. Run with: python main.py"""
import sys

from PySide6.QtWidgets import QApplication

from interface import IntroWindow, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NEXUS")
    app.setOrganizationName("NEXUS")
    window = MainWindow()
    intro = IntroWindow(on_finished=window.show)
    intro.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
