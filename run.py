# -*- coding: utf-8 -*-
"""Measure the repair rate per failure mode.

    python run.py

No API keys and no network. The corruptions in ``schema_guard.corrupt``
reproduce how model output actually fails, so the recovery rate can be measured
deterministically instead of estimated from a handful of anecdotes.
"""
import json
import os
from typing import List, Optional

from pydantic import BaseModel, Field

from schema_guard.corrupt import CORRUPTIONS, corrupt
from schema_guard.repair import repair_json
from schema_guard.validate import guard, retry_prompt

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
N_SAMPLES = 40


class LabResult(BaseModel):
    """A deliberately ordinary extraction target."""
    patient_ref: str = Field(min_length=3)
    test_code: str
    value: float
    unit: str
    abnormal: bool
    notes: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


def samples(n=N_SAMPLES):
    out = []
    for i in range(n):
        out.append({
            "patient_ref": "pat-%04d" % i,
            "test_code": ["2093-3", "718-7", "2345-7", "4548-4"][i % 4],
            "value": round(1.5 + i * 0.37, 2),
            "unit": ["mg/dL", "g/dL", "%", "mmol/L"][i % 4],
            "abnormal": bool(i % 3),
            "notes": "Collected fasting. Repeat in 3 months." if i % 5 == 0 else None,
            "tags": ["routine"] if i % 2 else ["routine", "flagged"],
        })
    return out


def main():
    os.makedirs(RESULTS, exist_ok=True)
    data = samples()
    report, metrics = [], {"n_samples": len(data)}

    report.append("# Results\n")
    report.append("%d synthetic extractions per failure mode, %d modes. "
                  "Everything deterministic and offline.\n"
                  % (len(data), len(CORRUPTIONS)))

    report.append("\n## Recovery rate by failure mode\n")
    report.append("| failure mode | parses raw | after repair | validates | repairs used |")
    report.append("| --- | --- | --- | --- | --- |")

    rows = {}
    for kind in CORRUPTIONS:
        raw_ok = repaired_ok = valid_ok = 0
        used = {}
        for i, obj in enumerate(data):
            text = corrupt(obj, kind, seed=i)
            if repair_json(text, max_rungs=1).ok:
                raw_ok += 1
            r = repair_json(text)
            if r.ok:
                repaired_ok += 1
                for name in r.repairs:
                    used[name] = used.get(name, 0) + 1
            g = guard(text, LabResult)
            if g.ok:
                valid_ok += 1
        n = len(data)
        rows[kind] = {"raw": raw_ok / n, "repaired": repaired_ok / n,
                      "valid": valid_ok / n, "repairs_used": used}
        top = sorted(used.items(), key=lambda kv: -kv[1])[:2]
        report.append("| %s | %.0f%% | %.0f%% | %.0f%% | %s |"
                      % (kind, 100 * raw_ok / n, 100 * repaired_ok / n,
                         100 * valid_ok / n,
                         ", ".join("`%s`" % k for k, _ in top) or "none"))
    metrics["by_mode"] = rows

    overall_raw = sum(r["raw"] for r in rows.values()) / len(rows)
    overall_valid = sum(r["valid"] for r in rows.values()) / len(rows)
    report.append("\nAveraged over every mode: **%.0f%%** of responses are "
                  "usable with no repair, **%.0f%%** after the ladder. That "
                  "difference is model calls not made.\n"
                  % (100 * overall_raw, 100 * overall_valid))

    unrecovered = {k: v for k, v in rows.items() if v["valid"] < 1.0}
    metrics["unrecovered"] = list(unrecovered)
    report.append("\n## What the ladder cannot fix\n")
    if unrecovered:
        for k, v in unrecovered.items():
            report.append("- **%s**: %.0f%% recovered" % (k, 100 * v["valid"]))
        report.append("\nTruncation is the honest limit. Brackets that were "
                      "never emitted can be closed, but a value cut in half is "
                      "gone, and inventing it is precisely the behaviour this "
                      "library exists to prevent. Those cases return a typed "
                      "failure and a correction prompt instead.\n")
    else:
        report.append("Nothing. Every mode recovered fully.\n")

    # ------------------------------------------------------- semantic failures
    report.append("\n## Structure is repaired, meaning is not\n")
    report.append("The line the library will not cross:\n")
    cases = [
        ("missing required field",
         '{"test_code": "2093-3", "value": 5.1, "unit": "mg/dL", "abnormal": false}'),
        ("wrong type, unambiguous",
         '{"patient_ref": "pat-1", "test_code": "x", "value": "5.1", '
         '"unit": "mg/dL", "abnormal": "true"}'),
        ("wrong type, ambiguous",
         '{"patient_ref": "pat-1", "test_code": "x", "value": "five point one", '
         '"unit": "mg/dL", "abnormal": false}'),
    ]
    report.append("\n| case | outcome | detail |")
    report.append("| --- | --- | --- |")
    sem = {}
    for label, text in cases:
        g = guard(text, LabResult)
        if g.ok:
            detail = "coerced " + ", ".join(
                "`%s` %s to %s" % (c["field"], c["from"], c["to"])
                for c in g.coercions)
            outcome = "accepted"
        else:
            first = g.issues[0]
            outcome = "**rejected**"
            detail = "`%s`: %s" % (first["field"], first["message"])
        sem[label] = {"ok": g.ok, "stage": g.stage,
                      "coercions": g.coercions, "issues": g.issues}
        report.append("| %s | %s | %s |" % (label, outcome, detail))
    metrics["semantic"] = sem

    report.append("\n`\"5.1\"` becomes `5.1` because that conversion is "
                  "lossless and unambiguous. `\"five point one\"` is rejected, "
                  "and a missing `patient_ref` is rejected rather than filled "
                  "with a default. A library that guesses here is worse than "
                  "no library, because the guess is invisible downstream.\n")

    report.append("\n## Correction prompts name the field\n")
    g = guard(cases[0][1], LabResult)
    report.append("```\n%s\n```\n" % retry_prompt(g, LabResult))
    report.append("Handing the model its own error is far more effective than "
                  "asking it to try again, and it costs nothing to name the "
                  "field. When a retry cannot help, `retry_prompt` returns "
                  "`None` rather than burning the call.\n")

    with open(os.path.join(RESULTS, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    with open(os.path.join(RESULTS, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, default=str)

    print("%-26s %8s %8s %8s" % ("failure mode", "raw", "repaired", "valid"))
    for k, v in rows.items():
        print("%-26s %7.0f%% %7.0f%% %7.0f%%"
              % (k, 100 * v["raw"], 100 * v["repaired"], 100 * v["valid"]))
    print("\noverall: %.0f%% raw -> %.0f%% after repair"
          % (100 * overall_raw, 100 * overall_valid))


if __name__ == "__main__":
    main()
