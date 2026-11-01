#!/usr/bin/env bash
# Builds the installed file tree that pack.sh turns into the .rpm and the
# Arch package, from the wheel the "build" job made (there is no .deb).
#
# usage: build-tree.sh <wheel> <root>
#
# The wheel is unpacked into /usr/share/loglens (the package and its
# .dist-info) and /usr/bin/loglens is a small launcher that puts that
# directory first on sys.path. Installing into site-packages instead would
# tie the package to one Python version (/usr/lib/python3.X), and Fedora,
# openSUSE and Arch each ship a different one.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ $# -eq 2 ] || { echo "usage: $0 <wheel> <root>" >&2; exit 2; }
wheel="$1"
root="$2"
# shellcheck source=/dev/null
. "$here/distro.conf"

case "$(basename "$wheel")" in
  "$NAME"-*-py3-none-any.whl) ;;
  *) echo "not a pure-Python $NAME wheel: $wheel" >&2; exit 1 ;;
esac
mkdir -p "$root/usr/share/$NAME" "$root/usr/bin"
python3 -m zipfile -e "$wheel" "$root/usr/share/$NAME"
find "$root/usr/share/$NAME" -name '__pycache__' -prune -exec rm -rf {} +
test -f "$root/usr/share/$NAME/$NAME/cli.py"

cat > "$root/usr/bin/$NAME" <<'PY'
#!/usr/bin/python3
# LogLens launcher installed by the .rpm and Arch packages. The app lives in
# /usr/share/loglens and runs on the distribution's Python 3 with its own
# textual, watchdog and click packages.
import sys

# Bytecode would be written into /usr/share/loglens when run as root and
# left behind, unowned, on removal.
sys.dont_write_bytecode = True
sys.path.insert(0, "/usr/share/loglens")

from loglens.cli import main

sys.exit(main())
PY
chmod 0755 "$root/usr/bin/$NAME"
install -Dm644 -t "$root/usr/share/doc/$NAME" "$repo/README.md" "$repo/CHANGELOG.md"
