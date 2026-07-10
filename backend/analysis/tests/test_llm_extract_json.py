"""extract_json: коректно тягне JSON, зокрема об'єкт із внутрішнім масивом."""
from analysis.services.llm import extract_json


def test_object_with_single_inner_array_returns_object():
    # РЕГРЕСІЯ: об'єкт, де остання структура — один масив; bracket-hunting
    # (["…"] перед {…}) раніше повертав внутрішній СПИСОК замість об'єкта
    raw = '{"relevant": true, "region": "Бурятія", "tags": {"economy_social": ["ЖКГ", "фестиваль"]}}'
    out = extract_json(raw)
    assert isinstance(out, dict)
    assert out["region"] == "Бурятія"
    assert out["tags"]["economy_social"] == ["ЖКГ", "фестиваль"]


def test_object_with_multiple_arrays_still_object():
    raw = '{"a": ["x"], "b": ["y", "z"]}'
    assert extract_json(raw) == {"a": ["x"], "b": ["y", "z"]}


def test_plain_array_still_array():
    assert extract_json('[{"i": 0}, {"i": 1}]') == [{"i": 0}, {"i": 1}]


def test_json_in_code_fence():
    assert extract_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_object_wrapped_in_prose():
    assert extract_json('Ось відповідь: {"x": 1} — готово') == {"x": 1}


def test_empty_and_garbage():
    assert extract_json("") is None
    assert extract_json("нема жодного джейсону") is None
