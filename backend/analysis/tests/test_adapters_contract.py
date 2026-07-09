"""Контракт адаптерів джерел: RawItem + реєстр ADAPTERS.

Параметризований тест проганяється по КОЖНОМУ зареєстрованому адаптеру —
новий адаптер (Phase 1-3) автоматично потрапляє під контракт.
"""
import dataclasses

import pytest

from analysis.services.infospace.adapters import (
    ADAPTERS, BaseSourceAdapter, RawItem, get_adapter, register,
)


def test_rawitem_contract_fields():
    names = {f.name for f in dataclasses.fields(RawItem)}
    assert {"external_id", "url", "title", "text",
            "posted_at", "author", "meta"} <= names


def test_rawitem_meta_not_shared_between_instances():
    a = RawItem(external_id="1", url="https://a.example/1")
    a.meta["x"] = 1
    assert RawItem(external_id="2", url="https://a.example/2").meta == {}


def test_register_rejects_duplicate_kind_and_blank():
    class Dup(BaseSourceAdapter):
        kind = "_test_dup"

    register(Dup)
    try:
        class Dup2(BaseSourceAdapter):
            kind = "_test_dup"

        with pytest.raises(ValueError):
            register(Dup2)

        class Blank(BaseSourceAdapter):
            kind = ""

        with pytest.raises(ValueError):
            register(Blank)
    finally:
        ADAPTERS.pop("_test_dup", None)


def test_register_returns_class_and_get_adapter_returns_instance():
    """Happy path реєстру: register повертає cls (інакше @register-класи
    стануть None), get_adapter повертає ІНСТАНС (не клас)."""
    class Dummy(BaseSourceAdapter):
        kind = "_test_ok"

    try:
        assert register(Dummy) is Dummy          # декоратор не губить клас
        got = get_adapter("_test_ok")
        assert isinstance(got, Dummy)             # інстанс, не клас
        assert not isinstance(got, type)
    finally:
        ADAPTERS.pop("_test_ok", None)


def test_get_adapter_unknown_kind_hints_registered():
    with pytest.raises(KeyError) as ei:
        get_adapter("no-such-kind")
    # підказка називає зареєстровані kind (не голий KeyError від dict[])
    assert "no-such-kind" in str(ei.value)
    assert "зареєстровані" in str(ei.value)


def test_config_limit_helpers():
    class _Src:  # мінімальний дубль Source для юніта без БД
        config = {"max_items": 7, "backfill_limit": 3}

    assert BaseSourceAdapter.max_items(_Src()) == 7
    assert BaseSourceAdapter.backfill_limit(_Src()) == 3
    _Src.config = {}
    assert BaseSourceAdapter.max_items(_Src()) == 100
    assert BaseSourceAdapter.backfill_limit(_Src()) == 20


@pytest.mark.parametrize(
    "kind",
    sorted(ADAPTERS) or [pytest.param(
        None, marks=pytest.mark.skip(reason="адаптери з'являться у Phase 1-3"))],
)
def test_registered_adapters_satisfy_contract(kind):
    cls = ADAPTERS[kind]
    assert issubclass(cls, BaseSourceAdapter)
    assert cls.kind == kind
    assert callable(cls.fetch)
