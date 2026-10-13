import argparse
import faulthandler
import os
from pathlib import Path
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
from .project_io import PROJECT_EXT  # noqa: E402
from .app import MainWindow, drain_surviving_media_threads  # noqa: E402


class _ProjectPathAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error("--project may be specified only once")
        setattr(namespace, self.dest, values)


def _parse_startup_args(
    argv: list[str],
) -> tuple[argparse.ArgumentParser, Path | None, list[str]]:
    """Parse Cove's option before Qt consumes its own command-line tokens."""
    launcher = os.path.basename(argv[0])
    prog = "python3 -m cove_video_editor" if launcher == "__main__.py" else launcher
    parser = argparse.ArgumentParser(
        prog=prog, allow_abbrev=False, add_help=True,
        description="Start Cove Video Editor, optionally opening a project.",
        epilog="Qt options such as -platform and -style are also accepted. "
               "The separate-token forms -platform, --platform, -style and --style "
               "require a following non-dash value. Other Qt options pass through "
               "without validation. "
               "A path starting with '-' must use --project=PATH.",
    )
    parser.add_argument(
        "--project", metavar="PATH", action=_ProjectPathAction,
        help="open an existing .coveproj after the window appears",
    )
    args, remainder = parser.parse_known_args(argv[1:])
    qt_value_options = {"-platform", "--platform", "-style", "--style"}
    for index, option in enumerate(remainder):
        if option in qt_value_options:
            if index + 1 >= len(remainder) or remainder[index + 1].startswith("-"):
                parser.error(f"{option} needs a value that does not start with '-'")
    path = None
    if args.project is not None:
        if not args.project:
            parser.error("--project PATH must not be empty")
        # abspath uses the launch directory, preserving symlinks and literal ~.
        path = Path(os.path.abspath(args.project))
        if not os.path.isfile(path):
            parser.error(f"--project must name an existing file: {path}")
        if path.suffix.lower() != PROJECT_EXT:
            parser.error(f"--project file must have the {PROJECT_EXT} extension: {path}")
    return parser, path, remainder


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser, project_path, remainder = _parse_startup_args(argv)
    app = QApplication([argv[0], *remainder])
    leftover = app.arguments()[1:]
    if leftover:
        parser.print_usage(file=sys.stderr)
        print(f"{parser.prog}: error: unsupported arguments: {' '.join(leftover)}",
              file=sys.stderr)
        print("Cove's own option is --project PATH; Qt options such as "
              "-platform/-style are also accepted.", file=sys.stderr)
        return 2
    app.setApplicationName("Cove Video Editor")
    app.setOrganizationName("Cove")
    theme.apply_theme(app)
    win = MainWindow()
    try:
        win.show()
        if project_path is not None:
            win.open_startup_project(project_path)
        code = app.exec()
    finally:
        # A parked media thread needs joining even after the event loop ends.
        drain_surviving_media_threads()
    return code


if __name__ == "__main__":
    sys.exit(main())
