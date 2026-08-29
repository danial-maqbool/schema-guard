# Results

40 synthetic extractions per failure mode, 13 modes. Everything deterministic and offline.


## Recovery rate by failure mode

| failure mode | parses raw | after repair | validates | repairs used |
| --- | --- | --- | --- | --- |
| clean | 100% | 100% | 100% | none |
| markdown fence | 0% | 100% | 100% | `strip markdown fence` |
| fence without language | 0% | 100% | 100% | `strip markdown fence` |
| wrapped in prose | 0% | 100% | 100% | `slice to outermost brackets` |
| trailing comma | 0% | 100% | 100% | `remove trailing commas` |
| python literals | 0% | 100% | 100% | `python literals to json` |
| single quotes | 0% | 100% | 100% | `single to double quotes` |
| unquoted keys | 0% | 100% | 100% | `quote bare keys` |
| smart quotes | 0% | 100% | 100% | `normalise smart quotes` |
| raw newline in string | 0% | 100% | 100% | `escape raw newlines in strings` |
| truncated | 0% | 58% | 42% | `close unterminated structures` |
| fence + trailing comma | 0% | 100% | 100% | `strip markdown fence`, `remove trailing commas` |
| prose + python literals | 0% | 100% | 100% | `slice to outermost brackets`, `python literals to json` |

Averaged over every mode: **8%** of responses are usable with no repair, **96%** after the ladder. That difference is model calls not made.


## What the ladder cannot fix

- **truncated**: 42% recovered

Truncation is the honest limit. Brackets that were never emitted can be closed, but a value cut in half is gone, and inventing it is precisely the behaviour this library exists to prevent. Those cases return a typed failure and a correction prompt instead.


## Structure is repaired, meaning is not

The line the library will not cross:


| case | outcome | detail |
| --- | --- | --- |
| missing required field | **rejected** | `patient_ref`: Field required |
| wrong type, unambiguous | accepted | coerced `value` str to float, `abnormal` str to bool |
| wrong type, ambiguous | **rejected** | `value`: Input should be a valid number, unable to parse string as a number |

`"5.1"` becomes `5.1` because that conversion is lossless and unambiguous. `"five point one"` is rejected, and a missing `patient_ref` is rejected rather than filled with a default. A library that guesses here is worse than no library, because the guess is invisible downstream.


## Correction prompts name the field

```
Your JSON did not match the required schema. Fix these and reply with the corrected object only:
- field `patient_ref`: Field required
```

Handing the model its own error is far more effective than asking it to try again, and it costs nothing to name the field. When a retry cannot help, `retry_prompt` returns `None` rather than burning the call.

