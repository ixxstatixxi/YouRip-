"""
NEXUS application entry point.

  python main.py          startup animation -> desktop GUI (placeholder homepage)
  python main.py --shell  open the backend terminal shell (same as `python backend.py`)

Any other arguments are passed to the backend (e.g. `python main.py --shell -d ~/Music`).
"""
import sys


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication
    from interface import IntroWindow, MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("NEXUS")
    app.setOrganizationName("NEXUS")

    window = MainWindow()

    def show_homepage():
        window.show()
        window.raise_()
        window.activateWindow()

    intro = IntroWindow(on_finished=show_homepage)
    intro.show()
    return app.exec()


def run_backend(argv: list[str]) -> int:
    import backend
    return backend.main(argv)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--shell":
        return run_backend(sys.argv[2:])
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
