"""Снятие обёрток с ответа модели.

Замер на Qwen3.6 через SiliconFlow блоков рассуждений не дал, но целевая
связка другая: у Qwen3.8 рассуждения включены по умолчанию, а поддержку
`enable_thinking` инференсом VK заранее проверить негде. Эти случаи — то, на
чём пайплайн иначе ляжет целиком.
"""

from __future__ import annotations

import pytest

from deckwright.llm.response import has_reasoning, strip_wrapping

CLEAN = '{"title": "Тема", "n": 3}'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (CLEAN, CLEAN),
        (f"<think>Подумаю: нужно 10 слайдов.</think>\n{CLEAN}", CLEAN),
        (f"<thinking>рассуждение</thinking>{CLEAN}", CLEAN),
        (f"```json\n{CLEAN}\n```", CLEAN),
        (f"```\n{CLEAN}\n```", CLEAN),
        (f"<think>рассуждение</think>\n```json\n{CLEAN}\n```", CLEAN),
        (f"Вот план:\n{CLEAN}\nГотово.", CLEAN),
        # Незакрытый блок: модели не хватило max_tokens на закрывающий тег.
        (f"<think>рассуждение оборвалось {CLEAN}", CLEAN),
        # Скобки внутри строковых значений не должны сбивать границы объекта.
        ('{"t": "срок {Q1} — {Q2}"}', '{"t": "срок {Q1} — {Q2}"}'),
    ],
)
def test_strip_wrapping(raw, expected):
    assert strip_wrapping(raw) == expected


def test_reasoning_is_detected_for_the_counter():
    assert has_reasoning("<think>x</think>{}")
    assert not has_reasoning(CLEAN)


def test_response_without_json_is_returned_as_is():
    """Не находим объект — отдаём как есть: пусть валидация скажет, что не так."""
    assert strip_wrapping("<think>только рассуждение</think>") == ""
    assert strip_wrapping("совсем не json") == "совсем не json"
