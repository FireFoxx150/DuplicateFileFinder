#!/usr/bin/env bash
# Build a Flatpak from the binaries in dist/dupefinder_<version>_<arch>.deb.
# Needs flatpak-builder and the freedesktop runtime (flatpak install flathub org.freedesktop.Platform//24.08).
# Not run in this project yet: flatpak-builder was not installed when this was written.
set -euo pipefail

VERSION=2.2.0
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEB="$ROOT/dist/dupefinder_${VERSION}_$(dpkg --print-architecture).deb"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

dpkg-deb -x "$DEB" "$STAGE/root"
mkdir -p "$STAGE/src"
cp "$STAGE/root/usr/bin/dupefinder" "$STAGE/root/usr/bin/dupefinder-cli" "$STAGE/src/"
cp "$ROOT/packaging/dupefinder.desktop" "$STAGE/src/org.dupefinder.App.desktop"

cat > "$STAGE/org.dupefinder.App.yml" <<EOF
app-id: org.dupefinder.App
runtime: org.freedesktop.Platform
runtime-version: '24.08'
sdk: org.freedesktop.Sdk
command: dupefinder
finish-args:
  - --share=ipc
  - --socket=x11
  - --socket=wayland
  - --filesystem=host
  - --talk-name=org.freedesktop.Notifications
modules:
  - name: dupefinder
    buildsystem: simple
    build-commands:
      - install -Dm755 dupefinder /app/bin/dupefinder
      - install -Dm755 dupefinder-cli /app/bin/dupefinder-cli
      - install -Dm644 org.dupefinder.App.desktop /app/share/applications/org.dupefinder.App.desktop
    sources:
      - type: dir
        path: src
EOF

cd "$STAGE"
flatpak-builder --force-clean --repo="$ROOT/dist/flatpak-repo" "$STAGE/build" org.dupefinder.App.yml
flatpak build-bundle "$ROOT/dist/flatpak-repo" "$ROOT/dist/dupefinder-${VERSION}.flatpak" org.dupefinder.App
echo "Built $ROOT/dist/dupefinder-${VERSION}.flatpak"
