"""Живой клиент к OpenAI-совместимому endpoint'у.

Модель задаётся конфигом: на разработке `Qwen/Qwen3.6-27B` через SiliconFlow,
в финале `Qwen/Qwen3.8-27B` на инференсе VK. Переход — смена `base_url` и
`model`, кода это не касается.

Две особенности, ради которых клиент вообще написан своими руками.

**Ответ по схеме.** В запрос уходит JSON-схема Pydantic-модели, ответ ею же и
валидируется. Невалидный ответ даёт ограниченное число повторов с текстом
ошибки валидации в подсказке — и ни одной регулярки по свободному тексту.

**Параметры, которых endpoint не знает.** `enable_thinking` и
`reasoning_effort` поддерживаются не везде. Провайдер, который их не знает,
отвечает 400. Ронять из-за этого прогон нельзя, поэтому клиент отбрасывает
непонятый параметр, повторяет запрос один раз и запоминает отказ для этого
endpoint'а — дальше не посылает. Отброшенное уходит в манифест прогона, чтобы
расхождение между провайдерами было видно, а не пряталось.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TypeVar

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from deckwright.config import ModelConfig
from deckwright.llm.base import StructuredError

T = TypeVar("T", bound=BaseModel)

# Из текста ошибки 400 достаём имя параметра, который провайдер не принял.
_UNKNOWN_PARAM = re.compile(
    r"(?:unknown|unsupported|unrecognized|invalid)[^\"']*[\"']?(\w+)[\"']?", re.IGNORECASE
)

_SCHEMA_INSTRUCTION = (
    "Ответь строго одним объектом JSON по схеме ниже. "
    "Без пояснений, без markdown-ограждения.\n\nСхема:\n{schema}"
)


class LiveClient:
    """Обращение к модели с валидацией ответа по Pydantic-схеме."""

    mocked = False

    def __init__(self, cfg: ModelConfig) -> None:
        if not cfg.configured:
            raise StructuredError(
                "модель не настроена: нужны base_url, api_key и model. "
                "Заполните .env по образцу .env.example."
            )
        self._cfg = cfg
        self._client = OpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )
        self.dropped_params: set[str] = set()
        self.calls = 0

    def _extra_body(self, step: str) -> dict[str, object]:
        params = self._cfg.step(step)
        extra: dict[str, object] = {"enable_thinking": params.enable_thinking}
        # reasoning_effort уходит только если задан в конфиге явно.
        if params.reasoning_effort is not None:
            extra["reasoning_effort"] = params.reasoning_effort
        return {k: v for k, v in extra.items() if k not in self.dropped_params}

    def _message(self, prompt: str, schema: type[T], images: list[bytes] | None) -> list[dict]:
        text = prompt + "\n\n" + _SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        if not images:
            return [{"role": "user", "content": text}]
        content: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
        return [{"role": "user", "content": content}]

    def _ask(self, step: str, messages: list[dict]) -> str:
        params = self._cfg.step(step)
        try:
            response = self._client.chat.completions.create(
                model=self._cfg.model,
                messages=messages,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                response_format={"type": "json_object"},
                extra_body=self._extra_body(step),
            )
        except BadRequestError as exc:
            rejected = self._rejected_param(str(exc))
            if rejected is None:
                raise
            # Провайдер не знает параметр: забываем его и пробуем ещё раз.
            self.dropped_params.add(rejected)
            response = self._client.chat.completions.create(
                model=self._cfg.model,
                messages=messages,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                response_format={"type": "json_object"},
                extra_body=self._extra_body(step),
            )
        self.calls += 1
        return response.choices[0].message.content or ""

    def _rejected_param(self, message: str) -> str | None:
        match = _UNKNOWN_PARAM.search(message)
        if match is None:
            return None
        name = match.group(1)
        known = {"enable_thinking", "reasoning_effort"}
        return name if name in known and name not in self.dropped_params else None

    def complete(
        self,
        step: str,
        prompt: str,
        schema: type[T],
        images: list[bytes] | None = None,
    ) -> T:
        messages = self._message(prompt, schema, images)
        last_error = ""
        for _ in range(self._cfg.max_retries + 1):
            raw = self._ask(step, messages)
            try:
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Ответ не прошёл валидацию. Исправь и пришли только JSON.\n"
                            f"Ошибки:\n{last_error}"
                        ),
                    },
                ]
        raise StructuredError(
            f"шаг {step}: модель не отдала валидный {schema.__name__} "
            f"за {self._cfg.max_retries + 1} попыток. Последние ошибки:\n{last_error}"
        )
