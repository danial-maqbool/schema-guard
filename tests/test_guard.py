# -*- coding: utf-8 -*-
from typing import List, Optional

import pytest
from pydantic import BaseModel, Field

from schema_guard.corrupt import CORRUPTIONS, corrupt
from schema_guard.repair import (close_unterminated, repair_json,
                                 slice_to_outermost, strip_fence)
from schema_guard.validate import guard, retry_prompt


class Item(BaseModel):
    name: str = Field(min_length=1)
    qty: int
    price: float
    active: bool
    note: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


GOOD = {"name": "widget", "qty": 3, "price": 9.99, "active": True,
        "note": None, "tags": ["a"]}


# ------------------------------------------------------------------ repairs

def test_clean_json_needs_no_repair():
    r = repair_json('{"a": 1}')
    assert r.ok and r.data == {"a": 1} and r.repairs == []


def test_markdown_fence_is_stripped():
    r = repair_json('```json\n{"a": 1}\n```')
    assert r.ok and r.data == {"a": 1}
    assert "strip markdown fence" in r.repairs


def test_surrounding_prose_is_discarded():
    r = repair_json('Sure! Here it is:\n\n{"a": 1}\n\nHope that helps.')
    assert r.ok and r.data == {"a": 1}


def test_trailing_comma_is_removed():
    assert repair_json('{"a": 1,}').data == {"a": 1}


def test_python_literals_become_json():
    assert repair_json('{"a": True, "b": None}').data == {"a": True, "b": None}


def test_single_quotes_are_converted():
    assert repair_json("{'a': 1}").data == {"a": 1}


def test_bare_keys_get_quoted():
    assert repair_json('{a: 1, b: 2}').data == {"a": 1, "b": 2}


def test_raw_newline_inside_a_string_is_escaped():
    r = repair_json('{"a": "one\ntwo"}')
    assert r.ok and r.data == {"a": "one\ntwo"}


def test_repairs_are_applied_cumulatively():
    """These failures co-occur; fixing only the best-matching one is not
    enough."""
    r = repair_json('```json\n{"a": 1,}\n```')
    assert r.ok and r.data == {"a": 1}
    assert len(r.repairs) >= 2


def test_a_truncated_object_is_closed():
    r = repair_json('{"a": 1, "b": 2')
    assert r.ok and r.data == {"a": 1, "b": 2}


def test_a_truncated_string_is_closed():
    r = repair_json('{"a": "hello')
    assert r.ok and r.data["a"] == "hello"


def test_a_dangling_key_is_dropped_not_invented():
    """The repair must not fabricate a value for a key that was cut off."""
    r = repair_json('{"a": 1, "b":')
    assert r.ok and r.data == {"a": 1}
    assert "b" not in r.data


def test_brackets_inside_strings_do_not_confuse_the_slicer():
    text = 'noise {"a": "} not the end {", "b": 2} more noise'
    assert repair_json(text).data == {"a": "} not the end {", "b": 2}


def test_empty_input_fails_cleanly():
    r = repair_json("")
    assert not r.ok and "empty" in r.error


def test_non_string_input_fails_cleanly():
    r = repair_json(None)
    assert not r.ok and "not str" in r.error


def test_hopeless_input_fails_without_raising():
    r = repair_json("this is just a sentence with no json in it")
    assert not r.ok and r.error


def test_arrays_are_handled_as_well_as_objects():
    assert repair_json('```json\n[1, 2, 3,]\n```').data == [1, 2, 3]


# --------------------------------------------------------------- validation

def test_a_valid_payload_passes():
    g = guard('{"name": "w", "qty": 1, "price": 2.0, "active": true}', Item)
    assert g.ok and g.model.qty == 1 and g.stage == "valid"


def test_unambiguous_string_numbers_are_coerced():
    g = guard('{"name": "w", "qty": "3", "price": "9.99", "active": "true"}', Item)
    assert g.ok
    assert g.model.qty == 3 and g.model.price == pytest.approx(9.99)
    assert {c["field"] for c in g.coercions} >= {"qty", "price"}


def test_ambiguous_values_are_rejected_not_guessed():
    """The line the library will not cross."""
    g = guard('{"name": "w", "qty": "a few", "price": 1.0, "active": true}', Item)
    assert not g.ok and g.stage == "schema"


def test_a_missing_required_field_is_never_invented():
    g = guard('{"qty": 1, "price": 2.0, "active": true}', Item)
    assert not g.ok
    assert any(i["field"] == "name" and not i["recoverable"] for i in g.issues)


def test_the_coercion_layer_makes_pydantics_silent_conversion_auditable():
    """Pydantic's default lax mode already turns "3" into 3, with or without
    our layer. What the layer adds is the *record* that it happened.

    That is the actual value: a silent type conversion is exactly the kind of
    thing you want in a log when an extraction pipeline starts behaving oddly
    three weeks later.
    """
    payload = '{"name": "w", "qty": "3", "price": 2.0, "active": true}'
    with_layer = guard(payload, Item, allow_coercion=True)
    without = guard(payload, Item, allow_coercion=False)

    assert with_layer.ok and without.ok            # pydantic accepts both
    assert with_layer.model.qty == without.model.qty == 3
    assert [c["field"] for c in with_layer.coercions] == ["qty"]
    assert without.coercions == []                 # the conversion is invisible


def test_unparseable_and_invalid_are_different_stages():
    """A caller needs to tell these apart: one is worth retrying, one is not."""
    assert guard("not json at all", Item).stage == "unparseable"
    assert guard('{"qty": 1}', Item).stage == "schema"


def test_retry_prompt_names_the_offending_field():
    g = guard('{"qty": 1, "price": 2.0, "active": true}', Item)
    prompt = retry_prompt(g, Item)
    assert "name" in prompt


def test_no_retry_prompt_for_a_successful_parse():
    g = guard('{"name": "w", "qty": 1, "price": 2.0, "active": true}', Item)
    assert retry_prompt(g, Item) is None


# --------------------------------------------------------------- end to end

@pytest.mark.parametrize("kind", [k for k in CORRUPTIONS if k != "truncated"])
def test_every_non_truncating_corruption_round_trips(kind):
    """The headline claim, asserted rather than only reported.

    Structural fields are checked rather than every value: the
    `raw newline in string` corruption deliberately inserts a newline *into* a
    string value, so recovering it faithfully means getting the newline back,
    not the original text. The repair is not supposed to undo that.
    """
    g = guard(corrupt(GOOD, kind, seed=0), Item)
    assert g.ok, "%s was not recovered" % kind
    assert g.model.qty == 3
    assert g.model.price == pytest.approx(9.99)
    assert g.model.active is True
    assert g.model.name.replace("\n", "") == "widget"


def test_truncation_is_allowed_to_fail_but_must_fail_safely():
    """Truncation is the honest limit. What must never happen is a
    half-recovered object being reported as valid."""
    for seed in range(20):
        g = guard(corrupt(GOOD, "truncated", seed=seed), Item)
        if g.ok:
            assert g.model.name == "widget"
            assert g.model.qty == 3
