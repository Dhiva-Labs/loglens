#!/usr/bin/env python3
"""Generate a sample log file for manually testing LogLens.

Stdlib-only so it can be run without installing the project.  Writes lines in
one of several common log formats (or a random mix of all of them), at a
configurable rate, with an adjustable proportion of error lines.  Some error
lines are followed by a Python-style traceback to exercise multi-line record
handling.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone

FORMATS = ["json", "syslog", "nginx", "generic"]

LOGGERS = ["app.core", "app.db", "app.api", "worker.tasks", "auth.session", "cache.redis"]
HOSTS = ["web01", "web02", "db01", "worker03", "api-gw"]
PROCS = ["nginx", "app", "sshd", "cron", "postgres"]
LEVELS_NORMAL = ["DEBUG", "INFO", "INFO", "INFO", "WARNING"]

METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]
PATHS = [
    "/",
    "/api/users",
    "/api/users/42",
    "/api/orders",
    "/static/app.js",
    "/health",
    "/login",
    "/api/products?category=books",
    "/favicon.ico",
]
STATUS_OK = [200, 201, 204, 301, 302, 304]
STATUS_ERR = [400, 401, 403, 404, 500, 502, 503]
REFERERS = ["-", "https://example.com/", "https://example.com/dashboard"]
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "curl/8.4.0",
    "python-requests/2.31.0",
]

TRACEBACK_MODULES = [
    ("app/core/handler.py", "process_request"),
    ("app/db/pool.py", "acquire"),
    ("app/api/routes.py", "dispatch"),
    ("worker/tasks.py", "run_task"),
]
EXCEPTIONS = ["ValueError", "KeyError", "TimeoutError", "ConnectionError", "RuntimeError"]
EXC_MESSAGES = [
    "invalid item state",
    "'user_id'",
    "connection timed out after 30s",
    "connection refused",
    "unexpected null in response",
]


def random_info_message(rng: random.Random) -> str:
    choice = rng.randrange(10)
    if choice == 0:
        return f"request completed in {rng.randint(5, 450)}ms"
    if choice == 1:
        return f"cache miss for key user:{rng.randint(1000, 9999)}"
    if choice == 2:
        return "connected to database"
    if choice == 3:
        return f"user user{rng.randint(1, 500)} logged in"
    if choice == 4:
        return f"processing batch of {rng.randint(1, 200)} items"
    if choice == 5:
        return f"retrying after backoff of {rng.choice([1, 2, 5, 10])}s"
    if choice == 6:
        return "config reloaded"
    if choice == 7:
        return "healthcheck ok"
    if choice == 8:
        return f"queue depth: {rng.randint(0, 500)}"
    return f"starting worker {rng.randint(1, 8)}"


def random_error_message(rng: random.Random) -> str:
    choice = rng.randrange(6)
    if choice == 0:
        return "failed to connect to database"
    if choice == 1:
        return "unhandled exception in request handler"
    if choice == 2:
        return "timeout waiting for upstream response"
    if choice == 3:
        return "invalid payload received"
    if choice == 4:
        path = rng.choice(["out.log", "err.log", "trace.log"])
        return f"permission denied writing to /var/log/app/{path}"
    return "connection reset by peer"


def random_traceback(rng: random.Random) -> list[str]:
    file1, func1 = rng.choice(TRACEBACK_MODULES)
    file2, func2 = rng.choice(TRACEBACK_MODULES)
    exc_type = rng.choice(EXCEPTIONS)
    exc_msg = rng.choice(EXC_MESSAGES)
    return [
        "Traceback (most recent call last):",
        f'  File "{file1}", line {rng.randint(10, 400)}, in {func1}',
        f"    result = {func2}(item)",
        f'  File "{file2}", line {rng.randint(10, 400)}, in {func2}',
        f'    raise {exc_type}("{exc_msg}")',
        f"{exc_type}: {exc_msg}",
    ]


def iso8601_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def gen_json_line(dt: datetime, rng: random.Random, error_rate: float) -> list[str]:
    logger = rng.choice(LOGGERS)
    if rng.random() < error_rate:
        level = rng.choice(["ERROR", "CRITICAL"])
        msg = random_error_message(rng)
        if rng.random() < 0.6:
            msg = msg + "\n" + "\n".join(random_traceback(rng))
    else:
        level = rng.choice(LEVELS_NORMAL)
        msg = random_info_message(rng)
    obj = {"ts": iso8601_ms(dt), "level": level, "logger": logger, "msg": msg}
    return [json.dumps(obj)]


def gen_syslog_lines(dt: datetime, rng: random.Random, error_rate: float) -> list[str]:
    host = rng.choice(HOSTS)
    proc = rng.choice(PROCS)
    pid = rng.randint(100, 65535)
    header = f"{dt.strftime('%b')} {dt.day:2d} {dt.strftime('%H:%M:%S')} {host} {proc}[{pid}]:"
    if rng.random() < error_rate:
        level = rng.choice(["ERROR", "CRITICAL"])
        msg = random_error_message(rng)
        lines = [f"{header} {level} {msg}"]
        if rng.random() < 0.6:
            lines.extend(random_traceback(rng))
        return lines
    level = rng.choice(LEVELS_NORMAL)
    msg = random_info_message(rng)
    return [f"{header} {level} {msg}"]


def gen_nginx_line(dt: datetime, rng: random.Random, error_rate: float) -> list[str]:
    ip = f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    method = rng.choice(METHODS)
    path = rng.choice(PATHS)
    status = rng.choice(STATUS_ERR) if rng.random() < error_rate else rng.choice(STATUS_OK)
    body_bytes = rng.randint(150, 8192)
    referer = rng.choice(REFERERS)
    agent = rng.choice(USER_AGENTS)
    time_local = dt.strftime("%d/%b/%Y:%H:%M:%S +0000")
    line = (
        f'{ip} - - [{time_local}] "{method} {path} HTTP/1.1" '
        f'{status} {body_bytes} "{referer}" "{agent}"'
    )
    return [line]


def gen_generic_lines(dt: datetime, rng: random.Random, error_rate: float) -> list[str]:
    name = rng.choice(LOGGERS)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S,") + f"{dt.microsecond // 1000:03d}"
    if rng.random() < error_rate:
        level = rng.choice(["ERROR", "CRITICAL"])
        msg = random_error_message(rng)
        lines = [f"{ts} {level} {name}: {msg}"]
        if rng.random() < 0.6:
            lines.extend(random_traceback(rng))
        return lines
    level = rng.choice(LEVELS_NORMAL)
    msg = random_info_message(rng)
    return [f"{ts} {level} {name}: {msg}"]


GENERATORS = {
    "json": gen_json_line,
    "syslog": gen_syslog_lines,
    "nginx": gen_nginx_line,
    "generic": gen_generic_lines,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="path to the log file to write")
    parser.add_argument("--format", choices=[*FORMATS, "mixed"], default="mixed")
    parser.add_argument(
        "--count", type=int, default=None, help="number of lines to generate (default: forever)"
    )
    parser.add_argument("--rate", type=float, default=10.0, help="lines per second (default: 10)")
    parser.add_argument(
        "--error-rate", type=float, default=0.1, help="fraction of lines that are errors, 0..1"
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="random seed for reproducible output"
    )
    parser.add_argument(
        "--rotate-every",
        type=int,
        default=None,
        help="truncate the file after this many lines, to simulate log rotation",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)
    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    lines_since_rotation = 0
    total = 0
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            while args.count is None or total < args.count:
                fmt = rng.choice(FORMATS) if args.format == "mixed" else args.format
                dt = datetime.now(timezone.utc)
                for line in GENERATORS[fmt](dt, rng, args.error_rate):
                    f.write(line + "\n")
                f.flush()
                total += 1
                lines_since_rotation += 1
                if args.rotate_every and lines_since_rotation >= args.rotate_every:
                    f.seek(0)
                    f.truncate()
                    lines_since_rotation = 0
                if interval:
                    time.sleep(interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
