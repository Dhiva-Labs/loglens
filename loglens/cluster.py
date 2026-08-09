"""Error and stack-trace clustering.

collect_error_records() walks buffer entries in seq order and stitches each
ERROR line together with the continuation lines that belong to it.  Records are
tracked per source, so tailing several files at once - where lines from
different sources interleave freely - still yields intact records.

cluster_errors() then groups records that describe the same logical failure.
Identity is built in two layers:

* Volatile text is masked before anything is compared.  Timestamps, uuids,
  hex/pointer literals, numeric and float values, long id-ish tokens and quoted
  payloads all collapse to typed placeholders (``<ts>``, ``<uuid>``, ``<hex>``,
  ``<n>``, ``<str>``).  Titles keep those typed placeholders because they read
  well; the signature collapses every placeholder to a single ``<*>`` slot so
  that a value which happens to be all digits in one occurrence and hex in the
  next cannot split a cluster.
* Records that carry a stack trace are identified by their frame structure -
  the ordered (file, function) pairs, with line numbers dropped and paths cut
  down to their trailing segments - plus the exception type and the masked
  exception message.  Records without frames fall back to the masked head
  message.

A final conservative merge pass folds message-only clusters whose token sets
are near-identical.  It compares a new cluster only against a bounded number of
existing cluster representatives found through an inverted token index, never
all-pairs over records, and it walks clusters in first-appearance order so the
outcome does not depend on dict or set iteration order.

Nothing here raises on malformed input: unparseable records degrade to their
own cluster rather than failing the caller.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache

from loglens.parsers import Level, LogLine

__all__ = ["Cluster", "ErrorRecord", "cluster_errors", "collect_error_records"]


@dataclass(slots=True)
class ErrorRecord:
    """An ERROR line together with the continuation lines that belong to it."""

    seq: int
    head: LogLine
    trace: list[LogLine] = field(default_factory=list)


@dataclass(slots=True)
class Cluster:
    """A group of error records that describe the same logical failure."""

    signature: str
    title: str
    count: int
    first_seq: int
    last_seq: int
    sources: frozenset[str]
    example: ErrorRecord


def collect_error_records(
    entries: Iterable[tuple[int, LogLine, Level | None]],
) -> list[ErrorRecord]:
    """Assemble error records from buffer entries, in seq order.

    A non-continuation line whose effective level is ERROR opens a record.
    Continuation lines from the same source with effective level ERROR attach
    to it; lines from other sources interleave without closing it.  The next
    non-continuation line from that source closes the record.  Continuations
    with no open record for their source are skipped.
    """
    records: list[ErrorRecord] = []
    open_records: dict[str, ErrorRecord] = {}

    for seq, line, effective_level in entries:
        source = line.source
        if line.continuation:
            record = open_records.get(source)
            if record is not None and effective_level == Level.ERROR:
                record.trace.append(line)
            continue

        # A real line always ends whatever record was open for its source,
        # whether or not it is an error itself.
        open_records.pop(source, None)
        if effective_level == Level.ERROR:
            record = ErrorRecord(seq=seq, head=line, trace=[])
            open_records[source] = record
            records.append(record)

    return records


def cluster_errors(records: Iterable[ErrorRecord]) -> list[Cluster]:
    """Group similar records, most frequent first, ties broken by newest seq."""
    groups: dict[str, _Group] = {}

    for record in records:
        try:
            seq = int(record.seq)
            source = str(record.head.source)
        except Exception:
            continue
        try:
            analysis = _analyze(record)
        except Exception:
            analysis = _UNPARSED

        group = groups.get(analysis.signature)
        if group is None:
            groups[analysis.signature] = _Group(
                signature=analysis.signature,
                title=analysis.title,
                count=1,
                first_seq=seq,
                last_seq=seq,
                sources={source},
                example=record,
                tokens=analysis.tokens,
                size=analysis.size,
                mergeable=analysis.mergeable,
            )
            continue

        group.count += 1
        group.sources.add(source)
        if seq < group.first_seq:
            group.first_seq = seq
        if seq >= group.last_seq:
            group.last_seq = seq
            group.example = record
            group.title = analysis.title

    merged = _merge_similar(list(groups.values()))
    merged.sort(key=lambda group: (-group.count, -group.last_seq))
    return [
        Cluster(
            signature=group.signature,
            title=group.title,
            count=group.count,
            first_seq=group.first_seq,
            last_seq=group.last_seq,
            sources=frozenset(group.sources),
            example=group.example,
        )
        for group in merged
    ]


# -- masking ------------------------------------------------------------------

_MAX_TITLE = 120
_MAX_SKELETON = 400
_MAX_FRAMES = 20
_FRAME_EDGE = 8

# One pass over the text does every substitution: alternation order encodes
# precedence, so a full timestamp wins over the bare numbers inside it.
_MASK_RE = re.compile(
    r"""
      (?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)
    | (?P<clf>\d{1,2}/[A-Za-z]{3}/\d{4}(?::\d{2}){3}(?:\s[+-]\d{4})?)
    | (?P<date>\d{4}-\d{2}-\d{2})
    | (?P<clock>\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)
    | (?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})
    | (?P<dquote>"[^"\n]*")
    | (?P<squote>(?<![0-9A-Za-z])'[^'\n]*')
    | (?P<hexlit>0[xX][0-9a-fA-F]+)
    | (?P<hexid>(?<![0-9A-Za-z])[0-9a-fA-F]{4,}(?![0-9A-Za-z]))
    | (?P<num>\d+(?:\.\d+)*)
    """,
    re.VERBOSE,
)

_MASK_TOKENS = {
    "ts": "<ts>",
    "clf": "<ts>",
    "date": "<ts>",
    "clock": "<ts>",
    "uuid": "<uuid>",
    "dquote": "<str>",
    "squote": "<str>",
    "hexlit": "<hex>",
    "num": "<n>",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_PLACEHOLDER_RE = re.compile(r"<(?:ts|uuid|hex|n|str)>")

_LEVEL_WORDS = "DEBUG|TRACE|INFO|NOTICE|WARNING|WARN|CRITICAL|CRIT|FATAL|ALERT|EMERG|ERROR|ERR"
_PREFIX_NOISE_RE = re.compile(
    rf"^(?:<ts>[\s,]*)*(?:[\[(]?\b(?:{_LEVEL_WORDS})\b[\])]?[\s:-]*)?",
    re.IGNORECASE,
)


def _mask_sub(match: re.Match[str]) -> str:
    name = match.lastgroup
    if name == "hexid":
        text = match.group()
        if not any(char.isdigit() for char in text):
            # All-letter runs like "cafe" or "deadbeef" are words, not ids.
            return text
        return "<hex>" if any(char.isalpha() for char in text) else "<n>"
    return _MASK_TOKENS.get(name or "", "<n>")


def _clean(text: str) -> str:
    """Strip escape sequences and control characters, collapse whitespace."""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    if "\x1b" in text:
        text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub(" ", text)
    return " ".join(text.split())


def _normalize(text: str) -> str:
    """Mask volatile values in a single line, keeping typed placeholders."""
    cleaned = _clean(text)
    if not cleaned:
        return ""
    masked = _MASK_RE.sub(_mask_sub, cleaned)
    trimmed = _PREFIX_NOISE_RE.sub("", masked, count=1).strip()
    # A line that was nothing but a timestamp and a level keeps its own text
    # rather than collapsing to an empty identity shared with every other one.
    return trimmed or masked


def _skeleton(normalized: str) -> str:
    """Reduce a normalized line to a case-folded, slot-collapsed identity."""
    if not normalized:
        return ""
    return _PLACEHOLDER_RE.sub("<*>", normalized).lower()[:_MAX_SKELETON]


def _truncate(text: str, limit: int = _MAX_TITLE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# -- frame and exception extraction -------------------------------------------

_PY_FRAME_RE = re.compile(r'File "(?P<file>[^"]*)", line \d+(?:,\s+in\s+(?P<func>.+?))?\s*$')
_CALL_FRAME_RE = re.compile(r"^at\s+(?P<sym>[^\s(]+)\s*\((?P<loc>[^)]*)\)")
_EXC_RE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*:\s*(?P<msg>.*)$"
)
_BARE_EXC_RE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"(?:Error|Exception|Interrupt|Warning|Exit|Timeout|Failure|Fault|Abort))$"
)
_CAUSE_PREFIX_RE = re.compile(
    r"^(?:Caused by|Suppressed)\s*:\s*|^Exception in thread\s+(?:\"[^\"]*\"|\S+)\s+"
)


@lru_cache(maxsize=8192)
def _reduce_path(path: str) -> str:
    """Cut a source path down to its trailing segments and mask volatile bits."""
    text = path.strip().replace("\\", "/")
    if not text:
        return "?"
    parts = [part for part in text.split("/") if part and part != "."]
    if not parts:
        return "?"
    return _MASK_RE.sub(_mask_sub, "/".join(parts[-2:]))


def _parse_frame(text: str, stripped: str) -> str | None:
    """Return a `path#symbol` frame id for a stack frame line, else None."""
    if 'File "' in stripped:
        match = _PY_FRAME_RE.search(stripped)
        if match is not None:
            func = (match.group("func") or "?").strip() or "?"
            return f"{_reduce_path(match.group('file'))}#{func}"
    if stripped.startswith("at "):
        # Covers Java (`at com.foo.Bar.baz(Bar.java:42)`) and the JavaScript
        # shape (`at Object.run (/app/x.js:10:5)`) alike.
        match = _CALL_FRAME_RE.match(stripped)
        if match is not None:
            location = match.group("loc").strip()
            file_part = location.split(":")[0].strip() or "?"
            return f"{_reduce_path(file_part)}#{match.group('sym')}"
    return None


def _exception_on(stripped: str) -> tuple[str, str] | None:
    """Extract (exception type, raw message) from a trailing exception line."""
    if stripped.startswith("Traceback"):
        return None
    candidate = _CAUSE_PREFIX_RE.sub("", stripped, count=1).strip()
    if not candidate:
        return None
    match = _EXC_RE.match(candidate)
    if match is not None:
        return match.group("type"), match.group("msg")
    bare = _BARE_EXC_RE.match(candidate)
    if bare is not None:
        return bare.group("type"), ""
    return None


def _record_lines(record: ErrorRecord) -> tuple[str, list[str]]:
    """Split a record into its head line and every following line of text.

    Handles the case where a parser kept a whole traceback inside one physical
    line (JSON logs carry embedded newlines in the message), so those records
    cluster by frame structure just like line-per-line ones.
    """
    head_text = record.head.message or record.head.raw or ""
    if not isinstance(head_text, str):
        head_text = str(head_text)
    head_line, _, remainder = head_text.partition("\n")
    extra: list[str] = remainder.split("\n") if remainder else []

    for line in record.trace:
        text = line.message or line.raw or ""
        if not isinstance(text, str):
            text = str(text)
        if "\n" in text:
            extra.extend(text.split("\n"))
        else:
            extra.append(text)
    return head_line, extra


def _collapse_frames(frames: list[str]) -> list[str]:
    """Drop consecutive repeats (recursion) and bound the frame chain length."""
    collapsed: list[str] = []
    for frame in frames:
        if not collapsed or collapsed[-1] != frame:
            collapsed.append(frame)
    if len(collapsed) > _MAX_FRAMES:
        collapsed = collapsed[:_FRAME_EDGE] + ["..."] + collapsed[-_FRAME_EDGE:]
    return collapsed


@dataclass(slots=True)
class _Analysis:
    signature: str
    title: str
    tokens: frozenset[str]
    size: int
    mergeable: bool


_UNPARSED = _Analysis(
    signature="M|<unparsed>",
    title="(unparsed error record)",
    tokens=frozenset(),
    size=0,
    mergeable=False,
)

_MIN_MERGE_TOKENS = 6


def _analyze(record: ErrorRecord) -> _Analysis:
    head_line, extra = _record_lines(record)

    frames: list[str] = []
    exc_type: str | None = None
    exc_msg = ""
    for text in extra:
        stripped = text.strip()
        if not stripped:
            continue
        frame = _parse_frame(text, stripped)
        if frame is not None:
            frames.append(frame)
            continue
        if text[:1].isspace():
            # Indented non-frame lines are echoed source, never the summary.
            continue
        found = _exception_on(stripped)
        if found is not None:
            # The last one wins: Python puts the raised exception last, Java
            # puts the root cause last.
            exc_type, exc_msg = found

    if frames or exc_type is not None:
        norm_msg = _normalize(exc_msg) if exc_msg else ""
        if exc_type is not None:
            title = f"{exc_type}: {norm_msg}" if norm_msg else exc_type
            if frames:
                # Frames plus the exception type are the identity.  Head wording
                # varies between occurrences of one failure and is deliberately
                # left out so it cannot split the cluster.
                chain = ";".join(_collapse_frames(frames))
                signature = f"T|{exc_type}|{chain}|{_skeleton(norm_msg)}"
            else:
                # With no frames the head message has to take part, or unrelated
                # errors that share a trailing "Foo: bar" line would collapse
                # into one.
                head_skeleton = _skeleton(_normalize(head_line))
                signature = f"E|{exc_type}|{_skeleton(norm_msg)}|{head_skeleton}"
        else:
            # Frames but no summary line: some parsers only mark indented lines
            # as continuations, so the trailing "SomeError: ..." never reaches
            # the record and the head message is the only thing separating
            # unrelated failures that share a frame chain.
            norm_head = _normalize(head_line)
            chain = ";".join(_collapse_frames(frames))
            signature = f"T|?|{chain}||{_skeleton(norm_head)}"
            title = norm_head or "(stack trace)"
        return _Analysis(
            signature=signature,
            title=_truncate(title),
            tokens=frozenset(),
            size=0,
            mergeable=False,
        )

    normalized = _normalize(head_line)
    skeleton = _skeleton(normalized)
    tokens = skeleton.split()
    return _Analysis(
        signature=f"M|{skeleton}",
        title=_truncate(normalized or _clean(head_line) or "(empty)"),
        tokens=frozenset(tokens),
        size=len(tokens),
        mergeable=len(tokens) >= _MIN_MERGE_TOKENS,
    )


# -- grouping -----------------------------------------------------------------


@dataclass(slots=True)
class _Group:
    signature: str
    title: str
    count: int
    first_seq: int
    last_seq: int
    sources: set[str]
    example: ErrorRecord
    tokens: frozenset[str]
    size: int
    mergeable: bool


_MERGE_THRESHOLD = 0.9
_MAX_SIZE_GAP = 2
_MAX_CANDIDATES = 16
_MAX_INDEX_TOKENS = 8
_MAX_POSTINGS = 32


def _anchor_tokens(tokens: frozenset[str]) -> list[str]:
    """Word-bearing tokens, sorted so indexing does not depend on set order."""
    return sorted(token for token in tokens if any(char.isalpha() for char in token))


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    shared = len(left & right)
    if not shared:
        return 0.0
    return shared / len(left | right)


def _absorb(target: _Group, other: _Group) -> None:
    target.count += other.count
    target.sources |= other.sources
    if other.first_seq < target.first_seq:
        target.first_seq = other.first_seq
    if other.last_seq > target.last_seq:
        target.last_seq = other.last_seq
        target.example = other.example
        target.title = other.title


def _merge_similar(groups: list[_Group]) -> list[_Group]:
    """Fold message-only groups into near-identical earlier representatives.

    Groups are visited in first-appearance order and each one is compared only
    against representatives reachable through an inverted index of their word
    tokens, capped at _MAX_CANDIDATES comparisons, so the pass stays linear in
    the number of groups.
    """
    survivors: list[_Group] = []
    index: dict[str, list[int]] = {}

    for group in groups:
        if not group.mergeable:
            survivors.append(group)
            continue

        anchors = _anchor_tokens(group.tokens)
        candidates: dict[int, None] = {}
        for token in anchors[:_MAX_INDEX_TOKENS]:
            for position in index.get(token, ()):
                candidates[position] = None
                if len(candidates) >= _MAX_CANDIDATES:
                    break
            if len(candidates) >= _MAX_CANDIDATES:
                break

        best_position = -1
        best_score = 0.0
        for position in candidates:
            other = survivors[position]
            if abs(other.size - group.size) > _MAX_SIZE_GAP:
                continue
            score = _similarity(other.tokens, group.tokens)
            # Strictly greater, so an exact tie keeps the earlier candidate -
            # candidates are visited in insertion order.
            if score >= _MERGE_THRESHOLD and score > best_score:
                best_score = score
                best_position = position

        if best_position >= 0:
            _absorb(survivors[best_position], group)
            continue

        position = len(survivors)
        survivors.append(group)
        for token in anchors[:_MAX_INDEX_TOKENS]:
            postings = index.setdefault(token, [])
            if len(postings) < _MAX_POSTINGS:
                postings.append(position)

    return survivors
