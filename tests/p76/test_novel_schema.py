"""Synthetic schema tests; no private prompt text or paid API calls."""
from copy import deepcopy

import pytest

from p76 import novel_fixtures, novel_llm, novel_rules, novel_schema, settings


def schema():
    item = {"type": "object", "additionalProperties": False,
            "required": ["episode", "hook"], "properties": {
                "episode": {"type": "integer"}, "hook": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["arc"],
            "properties": {"arc": {"type": "object", "additionalProperties": False,
                "required": [f"e{i}" for i in range(1, 8)],
                "properties": {f"e{i}": deepcopy(item) for i in range(1, 8)}}}}


def test_compaction_preserves_item_contract_and_canon():
    original = schema()
    before = deepcopy(original)
    compact, changed = novel_schema.compact_arc(original)
    assert changed and original == before
    assert compact["properties"]["arc"] == {
        "type": "array", "items": original["properties"]["arc"]["properties"]["e1"]}
    assert "minItems" not in compact["properties"]["arc"]
    assert novel_schema.compact_arc(compact) == (compact, False)


def test_heterogeneous_and_extra_constraints_not_dropped():
    for modify in (lambda a: a.update(description="keep"),
                   lambda a: a["properties"]["e7"].update(description="different"),
                   lambda a: a["required"].pop()):
        original = schema()
        modify(original["properties"]["arc"])
        assert novel_schema.compact_arc(original) == (original, False)
    assert novel_schema.compact_arc({"type": "object"}) == ({"type": "object"}, False)


@pytest.mark.parametrize("arc", [None, {}, [None], [{"episode": "1"}], [{"episode": True}]])
def test_bad_shapes_rejected(arc):
    assert not novel_schema.valid_arc_shape({"arc": arc})


@pytest.mark.parametrize("numbers", [[], [1], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 6],
                                     list(range(1, 9))])
def test_seven_episode_requirement_is_still_enforced(numbers):
    bible = novel_fixtures.bible()
    bible["arc"] = [{**bible["arc"][0], "episode": n} for n in numbers]
    assert "B01:arc_incomplete" in novel_rules.check_bible(bible)


def test_transport_and_partial_arc_requests_use_compact_schema():
    client = novel_llm.FixtureClient()
    result = novel_llm.call(client, label="test:write", model=settings.MODEL_WRITER,
        effort="medium", system="private canon", user_blocks=[{"text": "repair arc only"}],
        schema=schema(), max_tokens=16000, stream=True)
    assert result.ok
    sent = client.calls[0]
    assert sent["output_config"]["format"]["schema"]["properties"]["arc"]["type"] == "array"
    assert novel_schema.ARC_INSTRUCTION in sent["system"][0]["text"]
    assert len(client.calls) == 1
    assert "B01:arc_incomplete" not in novel_rules.check_bible(result.data)
