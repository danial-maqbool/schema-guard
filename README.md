# schema-guard

Language models produce almost-JSON constantly: fenced in markdown, wrapped in
a friendly sentence, with a trailing comma, with Python's `True` instead of
`true`, or simply cut off when the token budget ran out.

Every one of those is a deterministic text problem with a deterministic fix.
Spending another model call on them is slow, expensive and non-reproducible.

**8% of malformed responses parse as-is. 96% after the repair ladder.**

```bash
pip install -r requirements.txt
python run.py      # regenerates every number below
pytest -q          # 38 tests
```

No API keys, no network, no model. The corruptions reproduce how model output
actually fails, so the recovery rate is measured rather than estimated.

---

## Recovery rate by failure mode

40 synthetic extractions per mode, validated against a Pydantic schema:

| failure mode | parses raw | after repair | validates |
| --- | --- | --- | --- |
| clean | 100% | 100% | 100% |
| markdown fence | 0% | 100% | 100% |
| fence without language | 0% | 100% | 100% |
| wrapped in prose | 0% | 100% | 100% |
| trailing comma | 0% | 100% | 100% |
| python literals (`True`/`None`) | 0% | 100% | 100% |
| single quotes | 0% | 100% | 100% |
| unquoted keys | 0% | 100% | 100% |
| smart quotes | 0% | 100% | 100% |
| raw newline inside a string | 0% | 100% | 100% |
| fence + trailing comma | 0% | 100% | 100% |
| prose + python literals | 0% | 100% | 100% |
| **truncated** | 0% | 57% | **42%** |

Truncation is the honest limit and it is in the table rather than cropped out.
Brackets that were never emitted can be closed; a value cut in half is gone,
and inventing it is exactly the behaviour this library exists to prevent.

## Two rules

**1. Cheapest repair first, stop as soon as it parses.** Each rung is more
invasive than the last, so the ladder stops at the least aggressive fix that
works. Repairs apply *cumulatively*, because these failures co-occur — a fenced
response with a trailing comma needs both fixes, not the better one.

```python
from schema_guard.repair import repair_json

r = repair_json('```json\n{"qty": 3, "ok": True,}\n```')
r.data      # {'qty': 3, 'ok': True}
r.repairs   # ['strip markdown fence', 'remove trailing commas',
            #  'python literals to json']
```

**2. Structure is repaired. Meaning is not.**

| case | outcome |
| --- | --- |
| `"value": "5.1"` where a float belongs | **accepted**, coerced, and recorded |
| `"value": "five point one"` | **rejected** |
| `patient_ref` missing entirely | **rejected**, never defaulted |

A library that guesses here is worse than no library, because the guess is
invisible downstream.

## What the coercion layer actually adds

Worth stating precisely, because it would be easy to overstate: **Pydantic's
default lax mode already turns `"3"` into `3`.** Switching this layer off does
not make that payload fail.

What you lose is the knowledge that it happened.

```python
guard(payload, Item, allow_coercion=True).coercions
# [{'field': 'qty', 'from': 'str', 'to': 'int', 'before': '3', 'after': 3}]

guard(payload, Item, allow_coercion=False).coercions
# []   -- pydantic still coerced it, silently
```

The deliverable is the audit trail, not the conversion. A silent type change is
exactly what you want in a log when an extraction pipeline starts behaving
oddly three weeks later. There is a test pinning this distinction.

## Failures are typed, so a caller knows whether to retry

```python
g = guard(response, LabResult)
g.stage     # 'unparseable' | 'schema' | 'valid'
g.issues    # [{'field': 'patient_ref', 'type': 'missing',
            #   'message': 'Field required', 'recoverable': False}]
```

`unparseable` is usually worth retrying. A `missing` required field usually is
not — the model did not have the information. `recoverable` marks the
difference so a pipeline does not burn calls on responses that cannot improve.

When a retry *is* worth making, `retry_prompt` names the offending field:

```
Your JSON did not match the required schema. Fix these and reply with the
corrected object only:
- field `patient_ref`: Field required
```

Handing the model its own error works far better than asking it to try again,
and it costs nothing. When a retry cannot help, `retry_prompt` returns `None`.

## Layout

```
schema_guard/
  repair.py     the ladder: fences, prose, commas, literals, quotes, truncation
  validate.py   pydantic validation, audited coercion, typed issues, retry prompts
  corrupt.py    13 reproducible failure modes for the benchmark
tests/          38 tests
run.py          regenerates results/
```

## Details worth knowing

- **Bracket slicing is balance-aware.** `slice_to_outermost` tracks string
  state, so `{"a": "} not the end {"}` is not truncated at the brace inside the
  string. There is a test.
- **A dangling key is dropped, not filled.** `{"a": 1, "b":` recovers as
  `{"a": 1}` — `b` is absent rather than `null`, because the model never said
  what it was.
- **A repair never raises.** A failing rung returns a typed failure; an
  exception escaping a repair function would be worse than the malformed input.
- **Newline escaping is string-aware.** Newlines *between* tokens are left
  alone; only those inside a string token are escaped.

## Limitations

The corruptions are modelled on real failure modes but they are synthetic, and
a specific model's quirks will differ. `corrupt.py` is deliberately the smallest
file in the repo so a new mode is a five-line addition, and `run.py` will
measure it automatically.

Nested-object coercion is out of scope on purpose. It is where a helpful
library starts silently reshaping data, and the whole design here is that you
can see what it did.

## Licence

MIT.
