#!/usr/bin/env bash
# Smoke test for an installed LogLens package, run in the Fedora, openSUSE
# and Arch containers by verify.sh and after the AUR build by aur.sh. The
# repository is mounted at /src.
#
#   1. /usr/bin/loglens --version
#   2. the TUI on a real log file in a pty: it renders and quits on "q"
#   3. the repository's own test suite (tests/, including the Textual pilot
#      tests of the app) against the installed copy and the distribution's
#      textual, watchdog and click
set -euo pipefail

# readlink: on Fedora and Arch /usr/sbin is a symlink to /usr/bin and comes
# first on root's PATH.
test "$(readlink -f "$(command -v loglens)")" = /usr/bin/loglens
loglens --version | tee /tmp/loglens-version.txt
grep -Eqx 'loglens, version [0-9]+\.[0-9]+\.[0-9]+' /tmp/loglens-version.txt

printf '%s\n' '2026-01-01T00:00:00Z INFO service started' \
  '2026-01-01T00:00:01Z ERROR smoke-test-marker failed' > /tmp/smoke.log
python3 - <<'PY'
import os, pty, select, sys, time

pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm-256color", LINES="40", COLUMNS="140")
    os.execv("/usr/bin/loglens", ["loglens", "/tmp/smoke.log"])
out = b""
def pump(seconds):
    global out
    end = time.time() + seconds
    while time.time() < end:
        if select.select([fd], [], [], 0.1)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                return
pump(6)
os.write(fd, b"q")
pump(3)
done, status = os.waitpid(pid, os.WNOHANG)
if not done:
    os.kill(pid, 9)
    sys.exit("FAIL: the TUI did not quit on 'q'")
text = out.decode("utf-8", "replace")
if "Traceback" in text:
    print(text)
    sys.exit("FAIL: traceback in the TUI")
for marker in ("LogLens", "smoke-test-marker"):
    if marker not in text:
        print(text[-3000:])
        sys.exit(f"FAIL: the TUI never showed {marker!r}")
code = os.waitstatus_to_exitcode(status)
if code != 0:
    sys.exit(f"FAIL: the TUI exited with {code}")
print("TUI rendered the log file and quit cleanly")
PY

# Test-only tools, from the distribution.
if command -v dnf > /dev/null; then
  dnf -y -q install python3-pytest python3-pytest-asyncio
elif command -v zypper > /dev/null; then
  zypper -n -q install python3-pytest python3-pytest-asyncio
else
  pacman -S --noconfirm --needed -q python-pytest python-pytest-asyncio
fi
rm -rf /tmp/suite
mkdir -p /tmp/suite
cp -r /src/tests /src/scripts /src/pyproject.toml /tmp/suite/
cd /tmp/suite
# The .rpm and the Arch package keep the code in /usr/share/loglens (the
# launcher adds it to sys.path); the AUR package installs into site-packages.
if [ -d /usr/share/loglens/loglens ]; then
  export PYTHONPATH=/usr/share/loglens
  expect=/usr/share/loglens/loglens/
else
  expect="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/loglens/"
fi
python3 - "$expect" <<'PY'
import sys
from importlib.metadata import version
import click, textual, watchdog, loglens
print("loglens", loglens.__version__, loglens.__file__)
print("python", sys.version.split()[0], *(f"{m} {version(m)}" for m in ("textual", "watchdog", "click")))
assert loglens.__file__.startswith(sys.argv[1]), (loglens.__file__, sys.argv[1])
PY
# The tests read a widget's text through Static.content, the name textual 6.0
# gave Static.renderable. Fedora 44 ships textual 4.0 (the app itself only
# calls Static.update and runs fine there), so on textual < 6 the suite gets
# an alias that returns the same stored value; textual's own code is unchanged.
cat > /tmp/suite/textual_compat.py <<'PY'
from textual.widgets import Static

if not hasattr(Static, "content"):
    Static.content = property(lambda self: self.renderable)
PY
PYTHONPATH="/tmp/suite${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m pytest -q -p no:cacheprovider -p textual_compat tests
echo "loglens smoke test passed"
