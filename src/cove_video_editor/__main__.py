import faulthandler
import os
import sys

# PyInstaller `--windowed` builds detach from the console, which sets
# `sys.stderr` to None. `faulthandler.enable()` without args tries to
# register stderr's fd and raises `RuntimeError: sys.stderr is None`
# before the GUI even starts. Point it at a real file when there's no
# stderr, so crash tracebacks still land somewhere (a log next to the
# user data dir) instead of nuking the app on startup.
if sys.stderr is not None and hasattr(sys.stderr, "fileno"):
    try:
        faulthandler.enable()
    except (RuntimeError, OSError, ValueError):
        pass
else:
    try:
        from .portable import is_portable, portable_data_dir
        if is_portable():
            log_dir = portable_data_dir("cove-video-editor")
        elif sys.platform == "win32":
            log_dir = os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                "CoveVideoEditor",
            )
        else:
            log_dir = os.path.join(os.path.expanduser("~"), ".cove-video-editor")
        os.makedirs(log_dir, exist_ok=True)
        _fault_log = open(os.path.join(log_dir, "faulthandler.log"), "a", buffering=1)
        faulthandler.enable(file=_fault_log)
    except (OSError, RuntimeError, ValueError):
        pass

# Qt's default media backend on Linux is GStreamer, which crashes on AV1 /
# unusual codecs. PySide6 6.5+ ships an FFmpeg-based backend that covers
# everything we need — opt into it before QApplication touches the plugins.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtWidgets import QApplication  # noqa: E402

from . import theme  # noqa: E402
from .app import MainWindow, drain_surviving_media_threads  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Cove Video Editor")
    app.setOrganizationName("Cove")
    theme.apply_theme(app)
    win = MainWindow()
    win.show()
    code = app.exec()
    # A media thread the close could not join is parked, and released from
    # a queued `finished` - which needs an event loop that no longer
    # exists here. Join it before the interpreter destroys a QThread that
    # is still running.
    drain_surviving_media_threads()
    return code


if __name__ == "__main__":
    sys.exit(main())
