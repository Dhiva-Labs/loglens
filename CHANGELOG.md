# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-10

### Added

- Error clustering engine: groups similar errors and stack traces by frame-structure signature, with volatile-value masking and a deterministic merge pass
- Error clustering view (`c`) — a ranked panel of clusters with `[×N]` counts and jump-to-occurrence
- Export filtered view to file (`w`), with exclusive-create safety so an existing file is never overwritten

## [0.1.0] - 2026-08-10

### Added

- Merged live tail across multiple log files, each source rendered with its own color badge
- Per-file format auto-detection: JSON lines, syslog (RFC 3164), nginx (access + error), and a generic fallback
- Level highlighting (DEBUG/INFO/WARN/ERROR) as lines arrive
- Live regex filter (`/`) that narrows the view as you type
- Errors-only view (`e`) that keeps a traceback attached to the error line that produced it
- Pause with free scrollback (`p`)
- Buffer search (`s`) with `n`/`N` match navigation
- Error-frequency histogram (`t`)
- Log-rotation and truncation handling for tailed files
- Capped 100k-line ring buffer
- Sample log generator script (`scripts/genlog.py`) for demo traffic

[0.2.0]: https://github.com/Dhiva-Labs/loglens/releases/tag/v0.2.0
[0.1.0]: https://github.com/Dhiva-Labs/loglens/releases/tag/v0.1.0
