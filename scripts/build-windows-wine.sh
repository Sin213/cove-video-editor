#!/usr/bin/env bash
# Cross-build Windows Setup.exe + Portable.exe from Linux via Wine.
#
# Prereqs (all set up by the companion block in the README/overnight run):
#   - wine in $PATH
#   - a dedicated WINEPREFIX at $HOME/.wine-covebuild with Python 3.12,
#     PySide6, Pillow, and PyInstaller already installed
#   - Inno Setup 6 installed into the same wineprefix (this script installs
#     it if absent)
#
# Env vars:
#   VERSION=2.1.0    # artifact version
#
# Output lands in release/ alongside the AppImage.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${VERSION:-2.2.0}"
APP="cove-video-editor"
RELEASE_DIR="$ROOT/release"
mkdir -p "$RELEASE_DIR"

export WINEPREFIX="$HOME/.wine-covebuild"
export WINEARCH=win64
export WINEDEBUG=-all

# Python path inside wine prefix.
WIN_PY="C:\\users\\sin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
PY_UNIX="$WINEPREFIX/drive_c/users/sin/AppData/Local/Programs/Python/Python312/python.exe"
[ -x "$PY_UNIX" ] || { echo "Wine Python not found at $PY_UNIX"; exit 1; }

# ---------------------------------------------------------------- 1. ffmpeg + yt-dlp
FF_DIR="$ROOT/build/ff-win"
if [ ! -f "$FF_DIR/bin/ffmpeg.exe" ]; then
  echo "==> Downloading Windows ffmpeg (gyan.dev release-essentials)"
  rm -rf "$FF_DIR"
  mkdir -p "$FF_DIR"
  FF_TMP=$(mktemp -d)
  curl -fL --retry 3 --silent --show-error \
    -o "$FF_TMP/ff.zip" \
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
  (cd "$FF_TMP" && unzip -q ff.zip)
  SRC=$(find "$FF_TMP" -maxdepth 2 -type d -name 'ffmpeg-*' | head -1)
  [ -n "$SRC" ] || { echo "ffmpeg extract failed"; exit 1; }
  cp -r "$SRC"/. "$FF_DIR/"
  rm -rf "$FF_TMP"
fi
FFMPEG_EXE="$FF_DIR/bin/ffmpeg.exe"
FFPROBE_EXE="$FF_DIR/bin/ffprobe.exe"
FFMPEG_LICENSE="$FF_DIR/LICENSE"
[ -f "$FFMPEG_EXE" ] || { echo "ffmpeg.exe missing after extract"; exit 1; }

YTDLP_EXE="$ROOT/build/yt-dlp.exe"
YTDLP_PINS="$ROOT/packaging/yt-dlp-pins.json"
# BEGIN yt-dlp pin
fetch_yt_dlp() {
    local platform="$1" dest="$2" fields tag asset expected actual tmp
    fields=$(python3 -I -c '
import json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        pin = json.load(stream)
    assert type(pin["schema"]) is int and pin["schema"] == 1, "schema must be 1"
    tag = pin["tag"]
    asset = pin["assets"][sys.argv[2]]
    name, digest = asset["name"], asset["sha256"]
    assert isinstance(tag, str) and re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}", tag), "invalid tag"
    assert isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9._-]+", name), "invalid asset name"
    assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "invalid sha256"
    print(tag, name, digest, sep="\t")
except Exception as exc:
    print("yt-dlp pin manifest invalid or unavailable: " + str(exc), file=sys.stderr)
    sys.exit(1)
' "$YTDLP_PINS" "$platform") || { rm -f -- "$dest" || return 1; return 1; }
    IFS=$'\t' read -r tag asset expected <<< "$fields" || return 1
    if [ -f "$dest" ]; then
        actual=$(sha256sum < "$dest") || { rm -f -- "$dest" || return 1; return 1; }
        actual=${actual%% *}
        if [ "$actual" = "$expected" ]; then
            if [ "$platform" = "linux-x86_64" ]; then
                chmod 0755 -- "$dest" || return 1
            else
                chmod 0644 -- "$dest" || return 1
            fi
            echo "cached yt-dlp verified" || return 1
            echo "yt-dlp $tag $asset sha256 $actual verified" || return 1
            return 0
        fi
        echo "yt-dlp cache mismatch: expected $expected actual $actual; replacing" >&2 || return 1
    fi
    rm -f -- "$dest" || return 1
    tmp=$(mktemp "${dest}.tmp.XXXXXX") || { echo "yt-dlp temporary file could not be created" >&2 || return 1; return 1; }
    if ! curl --fail --location --retry 3 --max-time 300 --silent --show-error \
        -o "$tmp" "https://github.com/yt-dlp/yt-dlp/releases/download/$tag/$asset"; then
        actual=$(sha256sum < "$tmp") || actual="unavailable"
        rm -f -- "$tmp" || return 1
        echo "yt-dlp download failed: expected $expected actual ${actual%% *}" >&2 || return 1
        return 1
    fi
    actual=$(sha256sum < "$tmp") || {
        rm -f -- "$tmp" || return 1
        echo "yt-dlp checksum failed: expected $expected actual unavailable" >&2 || return 1
        return 1
    }
    actual=${actual%% *}
    if [ "$actual" != "$expected" ]; then
        rm -f -- "$tmp" || return 1
        echo "yt-dlp checksum mismatch: expected $expected actual $actual" >&2 || return 1
        return 1
    fi
    if [ "$platform" = "linux-x86_64" ]; then
        chmod 0755 -- "$tmp" || { rm -f -- "$tmp" || return 1; return 1; }
    else
        chmod 0644 -- "$tmp" || { rm -f -- "$tmp" || return 1; return 1; }
    fi
    mv -f -- "$tmp" "$dest" || { rm -f -- "$tmp" || return 1; return 1; }
    echo "yt-dlp $tag $asset sha256 $actual verified" || return 1
}
# END yt-dlp pin
fetch_yt_dlp "windows-x86_64" "$YTDLP_EXE" || exit 1

# Wine-side paths (win-style with drive letter)
FFMPEG_WIN=$(winepath -w "$FFMPEG_EXE")
FFPROBE_WIN=$(winepath -w "$FFPROBE_EXE")

# ---------------------------------------------------------------- 2. Icon
echo "==> Generating cove_icon.ico"
wine "$WIN_PY" -c "
from PIL import Image
Image.open('cove_icon.png').save(
    'cove_icon.ico',
    sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)],
)
" >/dev/null 2>&1

# ---------------------------------------------------------------- 3. Clean
rm -rf "$ROOT/build/win-onedir" "$ROOT/build/win-onefile" "$ROOT/dist"
mkdir -p "$ROOT/build/win-onedir" "$ROOT/build/win-onefile"

ASSET_DATA="src\\cove_video_editor\\assets\\cove_icon.png;cove_video_editor\\assets"

COMMON_ARGS=(
  --noconfirm --clean --log-level WARN
  --windowed
  --icon cove_icon.ico
  --paths src
  --add-data "$ASSET_DATA"
  --hidden-import PySide6.QtMultimedia
  --hidden-import PySide6.QtMultimediaWidgets
  --exclude-module PySide6.QtWebEngineCore
  --exclude-module PySide6.QtWebEngineWidgets
  --exclude-module PySide6.QtQml
  --exclude-module PySide6.QtQuick
  --exclude-module PySide6.QtPdf
  --exclude-module PySide6.Qt3DCore
  --exclude-module PySide6.QtCharts
  --exclude-module PySide6.QtDataVisualization
  --exclude-module tkinter
  --add-binary "${FFMPEG_EXE};."
  --add-binary "${FFPROBE_EXE};."
  --add-binary "${YTDLP_EXE};."
  packaging/launcher.py
)

# ---------------------------------------------------------------- 4. onedir
echo "==> PyInstaller (one-dir for installer)"
wine "$WIN_PY" -m PyInstaller \
  --name "$APP" \
  --distpath "$ROOT/build/win-onedir/dist" \
  --workpath "$ROOT/build/win-onedir/work" \
  "${COMMON_ARGS[@]}"

ONEDIR="$ROOT/build/win-onedir/dist/$APP"
[ -d "$ONEDIR" ] || { echo "onedir output missing: $ONEDIR"; exit 1; }
cp -f cove_icon.png "$ONEDIR/"
[ -f README.md ]          && cp -f README.md          "$ONEDIR/"
[ -f LICENSE ]            && cp -f LICENSE            "$ONEDIR/"
[ -f "$FFMPEG_LICENSE" ]  && cp -f "$FFMPEG_LICENSE"  "$ONEDIR/FFMPEG-LICENSE.txt"

# ---------------------------------------------------------------- 5. onefile
echo "==> PyInstaller (one-file portable)"
wine "$WIN_PY" -m PyInstaller \
  --name "$APP-portable" \
  --onefile \
  --distpath "$ROOT/build/win-onefile/dist" \
  --workpath "$ROOT/build/win-onefile/work" \
  "${COMMON_ARGS[@]}"

PORT_SRC="$ROOT/build/win-onefile/dist/$APP-portable.exe"
[ -f "$PORT_SRC" ] || { echo "portable output missing: $PORT_SRC"; exit 1; }

# ---------------------------------------------------------------- 6. ISCC
ISCC_UNIX="$WINEPREFIX/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe"
if [ ! -x "$ISCC_UNIX" ]; then
  echo "==> Installing Inno Setup 6 under wine"
  IS_TMP="$ROOT/build/innosetup.exe"
  curl -fL --retry 3 --silent --show-error \
    -o "$IS_TMP" "https://github.com/jrsoftware/issrc/releases/download/is-6_7_1/innosetup-6.7.1.exe"
  wine "$IS_TMP" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- 2>&1 | tail -3 || true
  rm -f "$IS_TMP"
fi
[ -x "$ISCC_UNIX" ] || { echo "ISCC.exe missing at $ISCC_UNIX"; exit 1; }

echo "==> Building Setup.exe with Inno Setup"
SRC_WIN=$(winepath -w "$ONEDIR")
OUT_WIN=$(winepath -w "$RELEASE_DIR")
ICON_WIN=$(winepath -w "$ROOT/cove_icon.ico")

wine "$ISCC_UNIX" \
  "/DAppVersion=$VERSION" \
  "/DSourceDir=$SRC_WIN" \
  "/DOutputDir=$OUT_WIN" \
  "/DIconFile=$ICON_WIN" \
  packaging/installer.iss

# ---------------------------------------------------------------- 7. stage
PORT_DEST="$RELEASE_DIR/${APP}-${VERSION}-Portable.exe"
rm -f "$PORT_DEST"
cp -f "$PORT_SRC" "$PORT_DEST"

echo ""
echo "Done:"
ls -lh "$RELEASE_DIR"/*.exe 2>/dev/null || true
