"""`RunManifest` — паспорт прогона. То, что делает результат воспроизводимым.

Критерий A19: в манифест пишутся версии промптов, модель, параметры, хэш
шаблона и seed. Сюда же идут замеры времени по этапам (A18) и всё, о чём
пайплайн вынужден был умолчать: подстановки шрифтов, пропущенные проверки,
отброшенные параметры запроса.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PromptVersion(BaseModel):
    """Версия одного промпта или конфига агента.

    Промпты лежат отдельными файлами в `prompts/` и `agents/` (требование ТЗ),
    а их хэш здесь — гарантия, что прогон воспроизводится именно тем текстом.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(llm|vlm|t2i)$")
    model: str
    base_url: str = ""
    steps: dict[str, dict[str, object]] = Field(default_factory=dict)
    # Параметры, которые endpoint не принял и клиент отбросил. Не ошибка, но
    # факт, влияющий на воспроизводимость на другом провайдере.
    dropped_params: list[str] = Field(default_factory=list)
    calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    # Повторы из-за ответа, не прошедшего валидацию по схеме. Каждый повтор —
    # это лишние секунды в бюджете и лишние токены в счёте, поэтому цифра
    # попадает в манифест, а не остаётся в логе.
    retries: int = Field(default=0, ge=0)
    rate_limit_hits: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    # Ответы, в которых пришёл блок рассуждений. Модели семейства Qwen3 умеют
    # его выдавать, и тогда ответ перестаёт быть чистым JSON.
    thinking_blocks: int = Field(default=0, ge=0)
    # Записанные ответы вместо живой модели: e2e-прогон в CI идёт так.
    mocked: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class StageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    seconds: float = Field(ge=0)


class FixIteration(BaseModel):
    """Один оборот цикла «аудит → исправление → пересборка → аудит».

    Цикл имеет право менять колоду, а значит обязан отчитываться: что было
    применено, что переписано моделью и сколько находок осталось. Без этого
    «стало лучше» проверяется только на глаз.
    """

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    # Ключи применённых находок (`slide:check_id:element`).
    applied: list[str] = Field(default_factory=list)
    # Что применить не удалось и почему: {ключ находки: причина}.
    skipped: dict[str, str] = Field(default_factory=dict)
    # Слайды, текст которых переписан моделью по явному выбору.
    rewritten_slides: list[int] = Field(default_factory=list)
    # Слайды, пересобранные и потому переспрошенные заново.
    rechecked_slides: list[int] = Field(default_factory=list)
    issues_before: int = Field(default=0, ge=0)
    issues_after: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0)


class FontSubstitution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested: str
    used: str
    reason: str


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_name: str
    seed: int = 0
    variants: list[str] = Field(default_factory=list)

    prompts: list[PromptVersion] = Field(default_factory=list)
    models: list[ModelUsage] = Field(default_factory=list)

    timings: list[StageTiming] = Field(default_factory=list)
    # Как прогон распорядился находками аудита. Пустой список означает, что
    # цикл исправления не запускался: режим `review` или `off`.
    fix_mode: str = Field(default="off", pattern=r"^(off|review|auto|selected)$")
    fix_iterations: list[FixIteration] = Field(default_factory=list)
    # Находки, оставшиеся после предела итераций. Оставить и назвать честнее,
    # чем чинить дальше вслепую.
    unresolved: list[str] = Field(default_factory=list)
    font_substitutions: list[FontSubstitution] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    artifacts: dict[str, str] = Field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return round(sum(t.seconds for t in self.timings), 3)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(m.cost_usd for m in self.models), 6)

    def within_budget(self, budget_seconds: int) -> bool:
        """Уложился ли прогон в бюджет времени из конфига (A18)."""
        return self.total_seconds <= budget_seconds
