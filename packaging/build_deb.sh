#!/usr/bin/env bash
# Build dist/dupefinder_<version>_<arch>.deb: a PyInstaller executable plus a desktop launcher.
# Needs PyInstaller and the app's requirements in $PYTHON (default: python3).
set -euo pipefail

VERSION=2.7.0
ARCH=$(dpkg --print-architecture)
PYTHON=${PYTHON:-python3}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/dupefinder_${VERSION}_${ARCH}"

cd "$ROOT"
"$PYTHON" -m PyInstaller --noconfirm --onefile --name dupefinder --collect-all tkinterdnd2 \
  --add-data "$ROOT/locale:locale" \
  --distpath "$WORK/exe" --workpath "$WORK/build" --specpath "$WORK" --exclude-module cupy --log-level WARN gui.py
"$PYTHON" -m PyInstaller --noconfirm --onefile --name dupefinder-cli \
  --distpath "$WORK/exe" --workpath "$WORK/build" --specpath "$WORK" --exclude-module cupy --log-level WARN cli.py

install -Dm755 "$WORK/exe/dupefinder" "$PKG/usr/bin/dupefinder"
install -Dm755 "$WORK/exe/dupefinder-cli" "$PKG/usr/bin/dupefinder-cli"
install -Dm644 packaging/dupefinder.desktop "$PKG/usr/share/applications/dupefinder.desktop"
install -Dm644 packaging/copyright "$PKG/usr/share/doc/dupefinder/copyright"
install -Dm644 packaging/dupefinder.nemo_action "$PKG/usr/share/nemo/actions/dupefinder.nemo_action"
gzip -9n -c packaging/changelog > "$WORK/changelog.gz"
install -Dm644 "$WORK/changelog.gz" "$PKG/usr/share/doc/dupefinder/changelog.gz"

mkdir -p "$PKG/DEBIAN"
SIZE_KB=$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: dupefinder
Version: $VERSION
Architecture: $ARCH
Maintainer: matthew <firefoxx.ms@gmail.com>
Installed-Size: $SIZE_KB
Depends: libc6
Recommends: ffmpeg
Section: utils
Priority: optional
Description: Find and remove duplicate files
 Finds exact duplicate files, visually similar images, and copies across two
 folders, with a preview pane for images, text, PDF and audio files.
EOF

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$PKG" "$ROOT/dist/" >/dev/null
echo "Built $ROOT/dist/dupefinder_${VERSION}_${ARCH}.deb"
