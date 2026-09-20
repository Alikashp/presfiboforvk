"""Приведение ответа модели к чистому JSON.

Модель может обернуть ответ в то, что к содержанию не относится: блок
рассуждений `<think>…</think>` или ограждение markdown. Здесь это снимается —
и только это. Разбор самого содержания делает Pydantic по схеме; регулярок по
смысловому тексту тут нет и быть не должно.

Почему обёртку всё же снимаем, хотя замер на Qwen3.6 через SiliconFlow при
`enable_thinking=false` не дал ни одного блока рассуждений: замер сделан на
одной связке модель-провайдер, а целевая другая. У Qwen3.8 режим рассуждений
включён по умолчанию, и поддерживает ли инференс VK параметр
`enable_thinking`, заранее неизвестно. Если не поддерживает, каждый ответ
придёт с блоком рассуждений и пайплайн ляжет целиком — на финале, там, где
чинить уже некогда. Снятие обёртки стоит двадцати строк и убирает этот риск.

Счётчик блоков остаётся: он показывает, работает ли отключение рассуждений у
конкретного провайдера, и сколько лишних токенов на это уходит.
"""

from __future__ import annotations

import re

# Блоки рассуждений разных моделей. Закрытые и незакрытые — модель, которой
# не хватило max_tokens, обрывает блок на полуслове.
_CLOSED_REASONING = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OPEN_REASONING = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)

# Ограждение markdown вокруг JSON.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

REASONING_MARKERS = ("<think>", "<thinking>", "<reasoning>")


def has_reasoning(raw: str) -> bool:
    """Пришёл ли в ответе блок рассуждений."""
    lowered = raw.lower()
    return any(marker in lowered for marker in REASONING_MARKERS)


def strip_wrapping(raw: str) -> str:
    """Снимает блоки рассуждений и ограждение markdown, оставляя объект JSON.

    Если после снятия обёрток в тексте всё ещё есть посторонние слова, берётся
    внешний объект `{…}` — от первой открывающей скобки до последней
    закрывающей. Это не разбор содержания: границы объекта ищутся по скобкам,
    а что внутри, решает валидация по схеме.
    """
    text = _CLOSED_REASONING.sub("", raw)

    # Незакрытый блок: всё от его начала до первой открывающей скобки объекта
    # — рассуждение, а не ответ.
    match = _OPEN_REASONING.search(text)
    if match:
        brace = text.find("{", match.end())
        text = text[brace:] if brace != -1 else text[: match.start()]

    text = _FENCE.sub("", text.strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()
