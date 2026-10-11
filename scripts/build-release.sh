#!/usr/bin/env bash
# Build .AppImage and .deb for Cove Video Editor.
#
# Requires:
#   - python3 (with pip)
#   - ar, tar, xz, curl
# Downloads static ffmpeg+ffprobe and yt-dlp builds automatically.
#
# Env vars:
#   VERSION=2.1.0     # version tag used in artifact filenames
#   SKIP_DEB=1        # build only the AppImage (iteration shortcut)
#
# Output lands in release/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="cove-video-editor"
DISPLAY_NAME="Cove Video Editor"
VERSION="${VERSION:-1.0.0}"
ARCH="x86_64"
DEB_ARCH="amd64"
RELEASE_DIR="$ROOT/release"
DIST_DIR="$ROOT/dist"
APPDIR="$ROOT/build/AppDir"
DEB_BUILD="$ROOT/build/deb"
BUILD_ENV="$ROOT/.buildenv"
ICON_SRC="$ROOT/cove_icon.png"

LOCAL_BIN="${HOME}/.local/bin"
APPIMAGETOOL="${LOCAL_BIN}/appimagetool"

mkdir -p "$RELEASE_DIR" "$LOCAL_BIN"
rm -rf "$DIST_DIR" "$ROOT/build"
mkdir -p "$ROOT/build"

# ----------------------------------------------------------------------
# 0. Build venv
# ----------------------------------------------------------------------
echo "==> Creating build venv"
rm -rf "$BUILD_ENV"
python3 -m venv "$BUILD_ENV"
"$BUILD_ENV/bin/pip" install --quiet --upgrade pip
"$BUILD_ENV/bin/pip" install --quiet -r requirements.txt pyinstaller

# ----------------------------------------------------------------------
# 1. Download ffmpeg + ffprobe static build and yt-dlp
# ----------------------------------------------------------------------
echo "==> Fetching ffmpeg static build"
FF_TMP="$ROOT/build/ff"
mkdir -p "$FF_TMP"
curl -fL --retry 3 --silent --show-error \
    -o "$FF_TMP/ffmpeg.tar.xz" \
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
(cd "$FF_TMP" && tar -xf ffmpeg.tar.xz)
FFMPEG_BIN="$(find "$FF_TMP" -type f -name ffmpeg | head -1)"
FFPROBE_BIN="$(find "$FF_TMP" -type f -name ffprobe | head -1)"
[ -n "$FFMPEG_BIN" ]  || { echo "ffmpeg not found after extract";  exit 1; }
[ -n "$FFPROBE_BIN" ] || { echo "ffprobe not found after extract"; exit 1; }

echo "==> Fetching standalone yt-dlp"
YTDLP_BIN="$ROOT/build/yt-dlp"
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
fetch_yt_dlp "linux-x86_64" "$YTDLP_BIN" || exit 1

# ----------------------------------------------------------------------
# 2. PyInstaller
# ----------------------------------------------------------------------
echo "==> Running PyInstaller"
"$BUILD_ENV/bin/pyinstaller" \
    --noconfirm --clean --log-level WARN \
    --windowed \
    --name "$APP_NAME" \
    --paths src \
    --add-data "src/cove_video_editor/assets/cove_icon.png:cove_video_editor/assets" \
    --hidden-import PySide6.QtMultimedia \
    --hidden-import PySide6.QtMultimediaWidgets \
    --exclude-module PySide6.QtWebEngineCore \
    --exclude-module PySide6.QtWebEngineWidgets \
    --exclude-module PySide6.QtQml \
    --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.QtPdf \
    --exclude-module PySide6.Qt3DCore \
    --exclude-module PySide6.QtCharts \
    --exclude-module PySide6.QtDataVisualization \
    --exclude-module tkinter \
    --add-binary "${FFMPEG_BIN}:." \
    --add-binary "${FFPROBE_BIN}:." \
    --add-binary "${YTDLP_BIN}:." \
    packaging/launcher.py

BUNDLE="$DIST_DIR/$APP_NAME"
[ -d "$BUNDLE" ] || { echo "PyInstaller bundle not found at $BUNDLE"; exit 1; }

# ----------------------------------------------------------------------
# 3. AppImage
# ----------------------------------------------------------------------
echo "==> Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/$APP_NAME" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -r "$BUNDLE"/. "$APPDIR/usr/lib/$APP_NAME/"
chmod +x "$APPDIR/usr/lib/$APP_NAME/_internal/yt-dlp"
cp "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
cp "$ICON_SRC" "$APPDIR/$APP_NAME.png"
cp "$ICON_SRC" "$APPDIR/.DirIcon" 2>/dev/null || true

cat > "$APPDIR/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
GenericName=Video Editor
Comment=Trim, cut, merge, crop, caption, and export video clips
Exec=$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;Utility;
Keywords=video;editor;trim;cut;merge;crop;subtitles;caption;ffmpeg;
StartupNotify=true
EOF
cp "$APPDIR/$APP_NAME.desktop" "$APPDIR/usr/share/applications/$APP_NAME.desktop"

cat > "$APPDIR/AppRun" <<EOF
#!/usr/bin/env bash
HERE="\$(dirname "\$(readlink -f "\${0}")")"
export PATH="\$HERE/usr/bin:\$PATH"
export LD_LIBRARY_PATH="\$HERE/usr/lib/$APP_NAME:\${LD_LIBRARY_PATH:-}"
exec "\$HERE/usr/lib/$APP_NAME/$APP_NAME" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/usr/bin/$APP_NAME" <<EOF
#!/usr/bin/env bash
HERE="\$(dirname "\$(readlink -f "\${0}")")/../lib/$APP_NAME"
exec "\$HERE/$APP_NAME" "\$@"
EOF
chmod +x "$APPDIR/usr/bin/$APP_NAME"

if [ ! -x "$APPIMAGETOOL" ]; then
    if command -v appimagetool >/dev/null 2>&1; then
        APPIMAGETOOL="$(command -v appimagetool)"
    else
        echo "==> Downloading appimagetool to $APPIMAGETOOL"
        curl -fL --retry 3 --silent --show-error -o "$APPIMAGETOOL" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
        chmod +x "$APPIMAGETOOL"
    fi
fi

echo "==> Building AppImage"
APPIMAGE_OUT="$RELEASE_DIR/${DISPLAY_NAME// /-}-${VERSION}-${ARCH}.AppImage"
ARCH=$ARCH "$APPIMAGETOOL" --no-appstream "$APPDIR" "$APPIMAGE_OUT"
chmod +x "$APPIMAGE_OUT"
echo "    -> $APPIMAGE_OUT"

# ----------------------------------------------------------------------
# 4. .deb (manual: ar + tar.xz, no dpkg-deb dependency)
# ----------------------------------------------------------------------
if [ "${SKIP_DEB:-0}" = "1" ]; then
    echo ""
    echo "==> SKIP_DEB=1 — skipping .deb build"
    echo "Release artifacts in $RELEASE_DIR:"
    ls -lh "$RELEASE_DIR"
    exit 0
fi

echo "==> Assembling .deb tree"
PKG_ROOT="$DEB_BUILD/${APP_NAME}_${VERSION}_${DEB_ARCH}"
rm -rf "$DEB_BUILD"
mkdir -p "$PKG_ROOT/DEBIAN" \
         "$PKG_ROOT/usr/bin" \
         "$PKG_ROOT/usr/lib/$APP_NAME" \
         "$PKG_ROOT/usr/share/applications" \
         "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$PKG_ROOT/usr/share/doc/$APP_NAME"

cp -r "$BUNDLE"/. "$PKG_ROOT/usr/lib/$APP_NAME/"
chmod +x "$PKG_ROOT/usr/lib/$APP_NAME/_internal/yt-dlp"
cp "$ICON_SRC" "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"

cat > "$PKG_ROOT/usr/bin/$APP_NAME" <<EOF
#!/usr/bin/env bash
exec /usr/lib/$APP_NAME/$APP_NAME "\$@"
EOF
chmod +x "$PKG_ROOT/usr/bin/$APP_NAME"

cat > "$PKG_ROOT/usr/share/applications/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
GenericName=Video Editor
Comment=Trim, cut, merge, crop, caption, and export video clips
Exec=$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;Utility;
Keywords=video;editor;trim;cut;merge;crop;subtitles;caption;ffmpeg;
StartupNotify=true
EOF

cp "$ROOT/LICENSE" "$PKG_ROOT/usr/share/doc/$APP_NAME/copyright"

INSTALLED_SIZE=$(du -sk "$PKG_ROOT/usr" | awk '{print $1}')

cat > "$PKG_ROOT/DEBIAN/control" <<EOF
Package: $APP_NAME
Version: $VERSION
Architecture: $DEB_ARCH
Maintainer: Cove <noreply@cove.local>
Installed-Size: $INSTALLED_SIZE
Section: video
Priority: optional
Homepage: https://github.com/Sin213/cove-video-editor
Description: Lightweight offline video editor
 Cove Video Editor is a focused desktop tool for trimming, cutting, merging,
 and cropping video clips, adding subtitles, replacing or mixing audio tracks,
 and exporting to a range of common formats (MP4, MKV, WebM, MOV, AVI, GIF,
 MP3, WAV). ffmpeg is bundled — no external dependencies needed at runtime.
EOF

echo "==> Building .deb archive"
DEB_OUT="$RELEASE_DIR/${APP_NAME}_${VERSION}_${DEB_ARCH}.deb"
WORK="$DEB_BUILD/work"
rm -rf "$WORK"
mkdir -p "$WORK"

(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/control.tar.xz" -C DEBIAN .)
(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/data.tar.xz" \
    --transform 's,^\./,,' \
    --exclude=./DEBIAN \
    .)
echo -n "2.0" > "$WORK/debian-binary"
echo "" >> "$WORK/debian-binary"

(cd "$WORK" && ar -rc "$DEB_OUT" debian-binary control.tar.xz data.tar.xz)

echo "    -> $DEB_OUT"

echo ""
echo "Release artifacts in $RELEASE_DIR:"
ls -lh "$RELEASE_DIR"
