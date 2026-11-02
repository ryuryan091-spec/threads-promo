"""Transport-only simplification of the seven identical bible arc schemas."""
from __future__ import annotations

from copy import deepcopy

ARC_INSTRUCTION = (
    "[출력 형식 보정] 이번 요청의 arc는 e1~e7 객체가 아니라 배열입니다. "
    "episode가 정수 1,2,3,4,5,6,7인 항목을 각각 한 번씩, 순서대로 정확히 7개 작성하세요. "
    "기존 7회차의 내용·필수 필드와 모든 안전 규칙은 유지하세요."
)


def compact_arc(schema: dict) -> tuple[dict, bool]:
    """Only rewrite the known homogeneous e1..e7 form; never mutate the canon.

    Length seven is checked by B01 locally, not unsupported API minItems=7.
    Future heterogeneous/extra constraints must not be silently discarded.
    """
    props = schema.get("properties", {})
    arc = props.get("arc", {}) if isinstance(props, dict) else {}
    if not isinstance(arc, dict) or arc.get("type") != "object":
        return schema, False
    keys = [f"e{i}" for i in range(1, 8)]
    items = arc.get("properties", {})
    if (set(arc) != {"type", "additionalProperties", "required", "properties"}
            or arc.get("additionalProperties") is not False
            or not isinstance(items, dict) or set(items) != set(keys)
            or sorted(arc.get("required", [])) != keys):
        return schema, False
    first = items["e1"]
    if (not isinstance(first, dict) or first.get("type") != "object"
            or first.get("properties", {}).get("episode") != {"type": "integer"}
            or "episode" not in first.get("required", [])
            or any(items[k] != first for k in keys)):
        return schema, False
    out = deepcopy(schema)
    out["properties"]["arc"] = {"type": "array", "items": deepcopy(first)}
    return out, True


def valid_arc_shape(data: dict) -> bool:
    arc = data.get("arc")
    return (isinstance(arc, list) and all(isinstance(item, dict)
            and type(item.get("episode")) is int for item in arc))
