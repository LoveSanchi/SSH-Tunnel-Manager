#!/usr/bin/env bash
# Build dist/tunnel-keeper_<version>_all.deb from this checkout (no debhelper needed).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import tunnel_keeper; print(tunnel_keeper.__version__)")"
STAGE="$ROOT/build/tunnel-keeper_${VERSION}_all"
PKG="$ROOT/packaging"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/usr/lib/tunnel-keeper" "$STAGE/usr/bin" \
  "$STAGE/usr/lib/systemd/user" "$STAGE/etc/xdg/autostart" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/doc/tunnel-keeper"

sed "s/@VERSION@/$VERSION/" "$PKG/debian/control" > "$STAGE/DEBIAN/control"
install -m 755 "$PKG/debian/postinst" "$PKG/debian/prerm" "$STAGE/DEBIAN/"

( cd "$ROOT" && find tunnel_keeper -type f \( -name '*.py' -o -name '*.svg' -o -name '*.sh' \) -print0 ) |
  while IFS= read -r -d '' f; do
    mode=644; [[ "$f" == *.sh ]] && mode=755
    install -D -m "$mode" "$ROOT/$f" "$STAGE/usr/lib/tunnel-keeper/$f"
  done

install -m 755 "$PKG/bin/tunnel-keeperd" "$STAGE/usr/lib/tunnel-keeper/tunnel-keeperd"
install -m 755 "$PKG/bin/tunnelctl" "$PKG/bin/tunnel-keeper-indicator" "$STAGE/usr/bin/"
install -m 644 "$PKG/tunnel-keeper.service" "$STAGE/usr/lib/systemd/user/"
install -m 644 "$PKG/tunnel-keeper-indicator.desktop" "$STAGE/etc/xdg/autostart/"
install -m 644 "$PKG/tunnel-keeper-indicator.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/usr/share/doc/tunnel-keeper/"

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/tunnel-keeper_${VERSION}_all.deb"
