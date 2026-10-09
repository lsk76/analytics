"""Спільний розбір і виконання MCP-батчів: окремий savepoint на запис."""
import json

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction

from . import fmt
from .registry import TOOLS, ToolError

MAX_BATCH = 100


def _batch(spec: str, name: str, *, csv: bool = False) -> list:
    if not isinstance(spec, str):
        raise ToolError(f"{name}: очікується рядок із JSON-масивом")
    try:
        data = (_split_refs(spec) if csv and not spec.strip().startswith(("[", "{"))
                else json.loads(spec))
    except json.JSONDecodeError as e:
        raise ToolError(f"{name}: битий JSON ({e})") from e
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_BATCH:
        raise ToolError(f"{name}: очікується масив від 1 до {MAX_BATCH} записів")
    return data


def _split_refs(spec: str) -> list[str]:
    return [ref.strip() for ref in spec.split(",") if ref.strip()]


def _payload(item, operation: str) -> dict:
    if not isinstance(item, dict):
        raise ToolError("запис має бути JSON-обʼєктом")
    target = TOOLS[operation]
    unknown = set(item) - set(target.params)
    if unknown:
        raise ToolError(f"невідомі параметри: {', '.join(sorted(unknown))}")
    payload = {k: v for k, v in item.items() if v is not None}
    for key, param in target.sig.parameters.items():
        if key not in payload:
            if param.default is param.empty:
                raise ToolError(f"обовʼязковий параметр: {key}")
            continue
        value = payload[key]
        if type(value) is not param.annotation:
            raise ToolError(f"{key}: очікується {param.annotation.__name__}")
        if key in ("url", "ref") and not value.strip():
            raise ToolError(f"{key}: порожнє значення")
        if key == "subscribers" and not -1 <= value <= 2_147_483_647:
            raise ToolError("subscribers: очікується -1 або число від 0 до 2147483647")
    return payload


def _write_batch(items: str, operation: str, label: str = "Довідник", prepare=None) -> str:
    data = _batch(items, "items")
    results, errors = [], 0
    for index, item in enumerate(data, 1):
        try:
            payload = _payload(item, operation)
            if prepare is not None:
                payload = prepare(payload, len(data))
            # Savepoint на запис: ensure() може створити канал ще до перевірки
            # типу. Помилка має відкотити весь запис, а не лишити напівзапис.
            with transaction.atomic():
                result = TOOLS[operation](payload)
            results.append(fmt.section(f"Запис {index}: OK", result))
        except (ToolError, ValidationError, DataError, IntegrityError, ValueError) as e:
            errors += 1
            results.append(fmt.section(f"Запис {index}: ПОМИЛКА", str(e)))
    return fmt.section(
        f"{label}: успішно {len(data) - errors}, помилок {errors}, усього {len(data)}",
        "\n\n".join(results))
