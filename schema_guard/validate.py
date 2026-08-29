# -*- coding: utf-8 -*-
"""Schema validation with typed, actionable failures.

Parsing is only half the problem. Valid JSON that does not match the contract
is still unusable, and the difference between "the model returned a string
where an integer belongs" and "the model omitted a required field" matters:
the first is safely coercible, the second is not recoverable without asking
again.

Coercion here is deliberately narrow. ``"42"`` becomes ``42`` because the
information is unambiguous. ``"forty-two"`` does not, and neither does an
absent field become a default. Anything that would require guessing what the
model meant is reported instead.
"""
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from schema_guard.repair import repair_json

# only losslessly reversible conversions belong here
COERCIONS = {
    ("str", "int"): lambda v: int(v.strip()),
    ("str", "float"): lambda v: float(v.strip()),
    ("str", "bool"): lambda v: {"true": True, "false": False,
                                "yes": True, "no": False,
                                "1": True, "0": False}[v.strip().lower()],
    ("int", "float"): float,
    ("float", "int"): lambda v: int(v) if float(v).is_integer() else _fail(),
    ("int", "str"): str,
    ("float", "str"): str,
}


def _fail():
    raise ValueError("not losslessly convertible")


@dataclass
class GuardResult:
    ok: bool
    model: BaseModel = None
    repairs: list = field(default_factory=list)
    coercions: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    stage: str = "parsed"

    def __bool__(self):
        return self.ok


def _issue(err):
    """Flatten a pydantic error into something a caller can act on."""
    loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
    kind = err.get("type", "unknown")
    return {
        "field": loc,
        "type": kind,
        "message": err.get("msg", ""),
        # this is the flag that decides whether a retry could possibly help
        "recoverable": not kind.startswith("missing"),
    }


def _type_name(v):
    return type(v).__name__


def _expected_types(model_cls, field_name):
    f = model_cls.model_fields.get(field_name)
    if f is None or f.annotation is None:
        return []
    ann = f.annotation
    return [getattr(ann, "__name__", str(ann))]


def coerce_scalars(data, model_cls):
    """Fix unambiguous type mismatches at the top level, and record each one.

    Worth being precise about what this adds, because it is easy to overstate.
    Pydantic's default lax mode **already** turns ``"3"`` into ``3``; switching
    this layer off does not make that payload fail. What you lose is the
    knowledge that it happened.

    So the deliverable here is the audit trail, not the conversion. A silent
    type change is exactly the thing you want in a log when an extraction
    pipeline starts behaving oddly three weeks later, and ``GuardResult``
    carries the before and after for every field it touched.

    Only scalars, only where the conversion is lossless, and nested models are
    left alone: recursing invites precisely the kind of invisible
    transformation this module exists to surface.
    """
    if not isinstance(data, dict):
        return data, []
    out, applied = dict(data), []
    for name in model_cls.model_fields:
        if name not in out:
            continue
        value = out[name]
        got = _type_name(value)
        for want in _expected_types(model_cls, name):
            if got == want:
                break
            fn = COERCIONS.get((got, want))
            if fn is None:
                continue
            try:
                new = fn(value)
            except (ValueError, KeyError, TypeError):
                continue
            out[name] = new
            applied.append({"field": name, "from": got, "to": want,
                            "before": value, "after": new})
            break
    return out, applied


def guard(text, model_cls, allow_coercion=True):
    """Parse, repair, validate, and report exactly what happened.

    Returns a ``GuardResult`` whose ``stage`` says how far it got, so a caller
    can tell an unparseable response from a well-formed one that broke the
    contract. Those need different handling: the first is usually worth
    retrying, the second usually is not.
    """
    parsed = repair_json(text)
    if not parsed.ok:
        return GuardResult(False, repairs=parsed.repairs, stage="unparseable",
                           issues=[{"field": "<root>", "type": "json",
                                    "message": parsed.error,
                                    "recoverable": True}])

    data, coercions = (coerce_scalars(parsed.data, model_cls)
                       if allow_coercion else (parsed.data, []))
    try:
        model = model_cls.model_validate(data)
    except ValidationError as exc:
        return GuardResult(False, repairs=parsed.repairs, coercions=coercions,
                           stage="schema", issues=[_issue(e) for e in exc.errors()])

    return GuardResult(True, model=model, repairs=parsed.repairs,
                       coercions=coercions, stage="valid")


def retry_prompt(result, model_cls):
    """Build a correction message naming exactly what was wrong.

    Handing the model its own error is far more effective than asking it to
    "try again", and naming the field costs nothing. Returns None when a retry
    cannot help, so a caller does not burn a call on an unfixable response.
    """
    if result.ok:
        return None
    if result.stage == "unparseable":
        return ("Your previous reply was not valid JSON. Reply with a single "
                "JSON object matching this schema and nothing else, no prose "
                "and no markdown fence:\n%s"
                % model_cls.model_json_schema())

    lines = ["Your JSON did not match the required schema. Fix these and "
             "reply with the corrected object only:"]
    for i in result.issues:
        lines.append("- field `%s`: %s" % (i["field"], i["message"]))
    return "\n".join(lines)
