# LogLens

A terminal UI for tailing and analyzing multiple log files live, with format auto-detection, filtering, and error tracking.

[![CI](https://github.com/Dhiva-Labs/loglens/actions/workflows/ci.yml/badge.svg)](https://github.com/Dhiva-Labs/loglens/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

<!-- demo.gif: terminal recording goes here -->

```
 LogLens v0.2.0  [app.log ]  [api.log ]  lines: 8421

 [app.log ] 14:22:01 INFO  request completed in 42ms
 [api.log ] 14:22:01 WARN  retrying after backoff of 2s
 [app.log ] 14:22:02 ERROR unhandled exception in request handler
 [app.log ] 14:22:02 Traceback (most recent call last):
 [app.log ] 14:22:02   File "app/core/handler.py", line 88, in process_request
 [app.log ] 14:22:02 ValueError: invalid item state
 [api.log ] 14:22:03 INFO  cache miss for key user:4821

 errors/min · 15m · peak 4        ▁▂▃▅█▃▂▁▁▂▃▄▂▁▁

 q quit   / filter   s search   e errors   p pause   t histogram   c clusters   w export
```

## Features

- **Merged live tail** across multiple files at once, each source rendered with its own color badge so you can tell them apart at a glance
- **Format auto-detection** per file — JSON lines, syslog, nginx (access + error), or a generic fallback — no flags to set
- **Level highlighting** (DEBUG/INFO/WARN/ERROR) colored inline as lines arrive
- **Live regex filter** (`/`) that narrows the view as you type
- **Errors-only view** (`e`) that keeps a traceback attached to the error line that caused it, instead of hiding the continuation lines
- **Pause with free scrollback** (`p`) — freeze the tail and scroll through what's on screen without new lines pushing it around
- **Buffer search** (`s`) with `n`/`N` navigation across the whole buffer, not just what's currently rendered
- **Error-frequency sparkline** (`t`) — a per-minute histogram of recent errors
- **Error clustering view** (`c`) — groups similar errors and stack traces over the whole buffer into a ranked list with `[×N]` counts; `Enter` jumps to the newest occurrence
- **Export** (`w`) — writes the currently filtered view (the whole buffer under any active filter, raw lines) to a file, never overwriting an existing one
- **Log-rotation handling** — truncation and rename+recreate (logrotate-style) are both detected and the tail picks back up cleanly
- **100k-line capped ring buffer** per session, oldest lines evicted first

## Install

```
pip install loglens
```

From source:

```
git clone https://github.com/Dhiva-Labs/loglens.git
cd loglens
pip install -e .
```

Requires Python 3.10+. Runs in any Linux/macOS terminal.

## Usage

```
loglens app.log api.log
```

Point it at one or more existing log files; each becomes its own colored source in the merged view.

No log files handy? Generate some demo traffic:

```
python scripts/genlog.py demo.log --rate 20
loglens demo.log
```

## Keybindings

| Key | Action |
| --- | --- |
| `/` | Open the regex filter. Filters live as you type; `Enter` keeps it and closes the box, `Esc` clears it. An invalid pattern is flagged in the status bar and input border — the last valid filter stays applied until you fix or clear it. |
| `e` | Toggle errors-only view. Continuation lines (tracebacks) stay attached to the error that produced them. |
| `p` | Toggle pause. While paused, the tail stops advancing and you get free scrollback; resuming clears any active search. |
| `s` | Open buffer search. `Enter` jumps to the newest match and auto-pauses if not already paused. |
| `n` | Jump to the next older match, wrapping to the newest. |
| `N` | Jump to the next newer match, wrapping to the oldest. |
| `t` | Toggle the error-frequency histogram. |
| `c` | Toggle the error-clustering panel — a modal listing similar errors and stack traces grouped over the whole buffer, ranked by count. `Enter` on a row jumps to that cluster's newest occurrence and auto-pauses; `c` or `Esc` closes the panel. |
| `w` | Open export. Prefilled with a timestamped filename; `Enter` writes the currently filtered view (whole buffer under the active filter/errors-only, raw lines) to that path. Existing files are never overwritten; `Esc` cancels. |
| `q` | Quit. |
| `↑` `↓` `PgUp` `PgDn` | Scroll the log view. |
| `Esc` | Context-sensitive, checked in this order: closes the cluster panel if it's open, otherwise closes the export box if it's open, otherwise closes the search box if it's open, otherwise clears an active search if one has been run, otherwise clears the regex filter. |

## Format support

| Format | Detected by | Extracted |
| --- | --- | --- |
| JSON lines | Line parses as a JSON object | Level from `level` / `lvl` / `severity` / `log.level` (first present); timestamp from `ts` / `time` / `timestamp` / `@timestamp` (ISO-8601 or epoch seconds/milliseconds); message from `msg` / `message` / `event`; every other key kept as a field |
| Syslog (RFC 3164) | `Mon DD HH:MM:SS host proc[pid]: message` header | Host, process, and PID as fields; level inferred from keywords in the message body (`error`/`fail`/`failure` → ERROR, `warn`/`warning` → WARN, `debug` → DEBUG; no match leaves the level unset) |
| nginx access (combined) | `ip - - [time] "METHOD path HTTP/x.x" status bytes "referer" "agent"` | Method, path, status, bytes, IP as fields; level from HTTP status — 5xx → ERROR, 4xx → WARN, else INFO |
| nginx error log | `yyyy/mm/dd hh:mm:ss [level] pid#tid: message` | PID, connection ID (if present) as fields; level taken directly from the bracketed level token |
| Generic (fallback) | A leading timestamp or a level word found anywhere in the line | Leading timestamp if present, level token if present; lines that are indented or start with `Traceback (most recent call last)` or `at ` attach as continuations to the previous record and inherit its level |

Detection samples the first 8KB of each file independently, so a mixed set of files on the command line can each get a different parser.

## How it works

A background thread tails every file on a single loop: it stats each path, reads only the new bytes, and detects rotation via inode/device changes or the file shrinking underneath it (truncation). It sleeps on an event with a 250ms timeout so it never busy-spins, and a watchdog filesystem observer wakes it early when something changes. The only thing that crosses from that thread to the UI is a thread-safe ring buffer (`LineBuffer`); the app polls it every 100ms and renders whatever's new.

## Development

```
git clone https://github.com/Dhiva-Labs/loglens.git
cd loglens
pip install -e ".[dev]"
pytest
ruff check .
```

The test suite covers the parsers, the tailer, the ring buffer, and the app.

## License

MIT — see [LICENSE](LICENSE).
