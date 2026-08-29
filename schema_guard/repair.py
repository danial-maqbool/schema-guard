# -*- coding: utf-8 -*-
"""A repair ladder for model output that is nearly JSON.

Language models produce almost-JSON constantly: fenced in markdown, wrapped in
a sentence of explanation, with a trailing comma, with Python's ``True``
instead of ``true``, or simply cut off when the token budget ran out. Every one
of those is a deterministic text problem with a deterministic fix, and burning
another model call on them is slow, expensive and non-reproducible.

Two rules shape everything here:

1. **Cheapest repair first, and stop as soon as it parses.** Each rung is more
   invasive than the last, so the ladder stops at the least aggressive fix that
   works.
2. **Never guess semantics.** Structure is repaired; meaning is not. A missing
   required field is reported, never invented. That boundary is the whole
   reason this is safe to run unattended.

Every repair is recorded, so what reaches the caller is auditable rather than
"it worked, somehow".
"""
import json
import re
from dataclasses import dataclass, field

FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
TRAILING_COMMA = re.compile(r",(\s*[}\]])")
PY_LITERAL = re.compile(r"(?<![\"\w])(True|False|None)(?![\"\w])")
UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)")
SMART_QUOTES = str.maketrans({"“": '"', "”": '"',
                              "‘": "'", "’": "'"})


@dataclass
class RepairResult:
    ok: bool
    data: object = None
    repairs: list = field(default_factory=list)
    error: str = None

    def __bool__(self):
        return self.ok


def _try(text):
    try:
        return True, json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False, None


def strip_fence(text):
    """Pull the body out of a markdown code fence."""
    m = FENCE.search(text)
    return m.group(1).strip() if m else text


def slice_to_outermost(text):
    """Keep everything between the first opening and last matching bracket.

    Handles the extremely common "Here is the JSON you asked for: {...} Let me
    know if you need anything else." Scanning for balance rather than taking
    the first and last bracket avoids being fooled by a bracket inside a
    string.
    """
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return text

    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def close_unterminated(text):
    """Add the brackets a truncated response never got to emit.

    This is the one rung that genuinely invents characters, so it is last and
    it is conservative: an unterminated string is closed, then open brackets
    are closed in reverse order. It cannot recover a value that was cut in
    half, and it is not supposed to. What it recovers is the objects that were
    already complete before the truncation.
    """
    depth_stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()

    out = text
    if in_str:
        out += '"'
    # a dangling key or comma cannot be closed into valid JSON, so drop it
    out = re.sub(r",\s*$", "", out.rstrip())
    out = re.sub(r'"[^"]*"\s*:\s*$', "", out.rstrip()).rstrip().rstrip(",")
    for opener in reversed(depth_stack):
        out += "}" if opener == "{" else "]"
    return out


LADDER = [
    ("as-is", lambda t: t),
    ("normalise smart quotes", lambda t: t.translate(SMART_QUOTES)),
    ("strip markdown fence", strip_fence),
    ("slice to outermost brackets", slice_to_outermost),
    ("remove trailing commas", lambda t: TRAILING_COMMA.sub(r"\1", t)),
    ("python literals to json",
     lambda t: PY_LITERAL.sub(lambda m: {"True": "true", "False": "false",
                                         "None": "null"}[m.group(1)], t)),
    ("quote bare keys", lambda t: UNQUOTED_KEY.sub(r'\1"\2"\3', t)),
    ("single to double quotes", lambda t: t.replace("'", '"')),
    ("escape raw newlines in strings", lambda t: _escape_newlines(t)),
    ("close unterminated structures", close_unterminated),
]


def _escape_newlines(text):
    """Escape literal newlines that appear inside a string token.

    A model writing a multi-line description often emits real newlines inside
    the quotes, which JSON forbids. Newlines *between* tokens are untouched.
    """
    out, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            elif ch == "\n":
                out.append("\\n")
                continue
            elif ch == "\r":
                out.append("\\r")
                continue
            elif ch == "\t":
                out.append("\\t")
                continue
        elif ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def repair_json(text, max_rungs=None):
    """Climb the ladder until the text parses.

    Repairs are applied cumulatively: each rung operates on the output of the
    ones before it, because these failures co-occur. A fenced response with a
    trailing comma needs both fixes, not the better one.
    """
    if not isinstance(text, str):
        return RepairResult(False, error="input was %s, not str" % type(text).__name__)
    if not text.strip():
        return RepairResult(False, error="empty response")

    applied, current = [], text
    rungs = LADDER if max_rungs is None else LADDER[:max_rungs]
    for name, fn in rungs:
        try:
            candidate = fn(current)
        except Exception as exc:                      # a repair must never raise
            return RepairResult(False, repairs=applied,
                                error="repair %r failed: %s" % (name, exc))
        if candidate != current:
            applied.append(name)
            current = candidate
        ok, data = _try(current)
        if ok:
            return RepairResult(True, data=data, repairs=applied)

    return RepairResult(False, repairs=applied,
                        error="still unparseable after %d repair attempts"
                              % len(rungs))
