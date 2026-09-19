"""Контракт обращения к модели: один вызов — один типизированный объект.

Свободный текст наружу не выходит. Слой просит объект нужной Pydantic-схемы,
клиент отвечает им же или бросает исключение. Разбор ответа регулярками
запрещён постановкой, и здесь для него просто нет места.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredError(RuntimeError):
    """Модель не отдала валидный объект за отведённое число попыток."""


class StructuredClient(Protocol):
    """Единый интерфейс живого клиента и клиента на записанных ответах."""

    mocked: bool

    def complete(
        self,
        step: str,
        prompt: str,
        schema: type[T],
        images: list[bytes] | None = None,
    ) -> T:
        """Возвращает объект `schema`. Параметры шага берутся из конфига."""
        ...
