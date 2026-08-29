# -*- coding: utf-8 -*-
"""Corruptions that reproduce how model output actually fails.

Every one of these is something a language model does regularly. Having them as
named, reproducible transformations is what turns "the repair ladder seems to
help" into a measured recovery rate per failure mode, which is the only way to
know whether a new rung is worth its complexity.
"""
import json
import random


def clean(obj):
    return json.dumps(obj, indent=2)


def fenced(obj, rng):
    return "```json\n%s\n```" % clean(obj)


def fenced_no_lang(obj, rng):
    return "```\n%s\n```" % clean(obj)


def prose_wrapped(obj, rng):
    intros = ["Sure! Here is the JSON you requested:",
              "Here's the extracted data:",
              "Based on the document, I found the following:"]
    outros = ["\n\nLet me know if you need anything else!",
              "\n\nHope this helps.", ""]
    return "%s\n\n%s%s" % (rng.choice(intros), clean(obj), rng.choice(outros))


def trailing_comma(obj, rng):
    s = clean(obj)
    idx = s.rfind("\n}")
    return s if idx < 0 else s[:idx] + ",\n}"


def python_literals(obj, rng):
    return (clean(obj).replace("true", "True").replace("false", "False")
            .replace("null", "None"))


def single_quotes(obj, rng):
    return clean(obj).replace('"', "'")


def bare_keys(obj, rng):
    import re
    return re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"(\s*:)', r"\1\2", clean(obj))


def smart_quotes(obj, rng):
    return clean(obj).replace('"', "“", 1).replace('"', "”", 1)


def truncated(obj, rng):
    """Cut the response off, as a token limit would."""
    s = clean(obj)
    return s[:int(len(s) * rng.uniform(0.55, 0.8))]


def raw_newline_in_string(obj, rng):
    """A multi-line value emitted with real newlines inside the quotes.

    Targets the first string *value* long enough to split, rather than a fixed
    field name. An earlier version looked only for ``notes``/``description``,
    which existed on 20% of the samples, so the mode silently scored 80% "no
    corruption needed" and the repair was barely exercised.
    """
    import re

    s = clean(obj)
    for m in re.finditer(r':\s*"([^"\n]{4,})"', s):
        start, end = m.span(1)
        mid = (start + end) // 2
        return s[:mid] + "\n" + s[mid:]
    return s


def fenced_and_trailing_comma(obj, rng):
    """Failures co-occur. This is why repairs are applied cumulatively."""
    return "```json\n%s\n```" % trailing_comma(obj, rng)


def prose_and_python_literals(obj, rng):
    return "Here you go:\n\n%s" % python_literals(obj, rng)


CORRUPTIONS = {
    "clean": lambda o, r: clean(o),
    "markdown fence": fenced,
    "fence without language": fenced_no_lang,
    "wrapped in prose": prose_wrapped,
    "trailing comma": trailing_comma,
    "python literals": python_literals,
    "single quotes": single_quotes,
    "unquoted keys": bare_keys,
    "smart quotes": smart_quotes,
    "raw newline in string": raw_newline_in_string,
    "truncated": truncated,
    "fence + trailing comma": fenced_and_trailing_comma,
    "prose + python literals": prose_and_python_literals,
}


def corrupt(obj, kind, seed=0):
    rng = random.Random(seed)
    return CORRUPTIONS[kind](obj, rng)
