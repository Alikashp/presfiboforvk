"""Клиент на записанных ответах: прогон без сети и без ключа.

Нужен по двум причинам. Первая — e2e-тест обязан идти в CI на каждом коммите,
а ходить оттуда в платный endpoint нельзя. Вторая — воспроизводимость: на
записанных ответах сквозной прогон даёт побайтово тот же результат, и любое
расхождение означает изменение в коде, а не настроение модели.

Ответы лежат в `tests/fixtures/recorded/<шаг>.json`. Ключ — имя шага, а не хэш
запроса: на этом уровне детализации записи переживают правку формулировки
промпта, а их подмена при смене смысла шага — осознанное действие, а не
случайность.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from deckwright.llm.base import StructuredError

T = TypeVar("T", bound=BaseModel)


class RecordedClient:
    """Отдаёт заранее записанные ответы вместо обращения к модели."""

    mocked = True

    def __init__(self, recordings_dir: str | Path) -> None:
        self._dir = Path(recordings_dir)
        self.calls = 0
        # Счётчики живого клиента. Записям они всегда нулевые, но читает их
        # диагностика пробы, и без них она падает на попытке отчитаться.
        self.retries = 0
        self.thinking_blocks = 0
        self.rate_limit_hits = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.dropped_params: set[str] = set()
        self.last_raw = ""

    def complete(
        self,
        step: str,
        prompt: str,  # записи выбираются по шагу, а не по тексту запроса
        schema: type[T],
        images: list[bytes] | None = None,  # записям картинки не нужны
    ) -> T:
        path = self._dir / f"{step}.json"
        if not path.exists():
            raise StructuredError(
                f"нет записанного ответа для шага {step!r}: ожидался {path}. "
                "Запишите его или запустите с живой моделью."
            )
        self.calls += 1
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise StructuredError(
                f"записанный ответ {path} не соответствует схеме {schema.__name__}: "
                "запись устарела после изменения контракта"
            ) from exc
