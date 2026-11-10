#!/usr/bin/env bash
# Build dist/dupefinder-<version>-x86_64.AppImage from the binaries in the .deb.
# Needs appimagetool: download it from the AppImage project, then run this with APPIMAGETOOL=/path/to/appimagetool.
# Not run in this project yet: appimagetool was not installed when this was written.
set -euo pipefail

VERSION=2.7.1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEB="$ROOT/dist/dupefinder_${VERSION}_$(dpkg --print-architecture).deb"
TOOL=${APPIMAGETOOL:-appimagetool}
command -v "$TOOL" >/dev/null || { echo "appimagetool not found; set APPIMAGETOOL" >&2; exit 1; }

APPDIR=$(mktemp -d)/Dupe.AppDir
trap 'rm -rf "$(dirname "$APPDIR")"' EXIT
dpkg-deb -x "$DEB" "$APPDIR"
mv "$APPDIR/usr" "$APPDIR/usr-tmp" && mkdir -p "$APPDIR/usr/bin" && mv "$APPDIR/usr-tmp/bin/"* "$APPDIR/usr/bin/" && rm -rf "$APPDIR/usr-tmp"
cp "$ROOT/packaging/dupefinder.desktop" "$APPDIR/dupefinder.desktop"

ICON=$(find /usr/share/icons -name 'edit-find.png' -path '*256*' 2>/dev/null | head -1)
[ -n "$ICON" ] || { echo "no 256px edit-find icon found in /usr/share/icons" >&2; exit 1; }
cp "$ICON" "$APPDIR/edit-find.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/bin/dupefinder" "$@"
EOF
chmod +x "$APPDIR/AppRun"

ARCH=x86_64 "$TOOL" "$APPDIR" "$ROOT/dist/dupefinder-${VERSION}-x86_64.AppImage"
echo "Built $ROOT/dist/dupefinder-${VERSION}-x86_64.AppImage"
