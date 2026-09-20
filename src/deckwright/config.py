"""Загрузка конфигурации прогона.

Единственная точка, где `configs/config.yaml` превращается в типизированный
объект. Секретов в YAML нет — там ссылки вида ``${LLM_API_KEY}``, которые
подставляются из окружения здесь.

Отсутствующая переменная окружения не является ошибкой загрузки: разбор
шаблона работает без доступа к модели, а проверка «ключ есть» делается тем
слоем, которому ключ реально нужен.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class StepParams(BaseModel):
    """Параметры одного шага пайплайна при обращении к модели.

    ``enable_thinking`` и ``reasoning_effort`` поддерживаются не всеми
    endpoint'ами. Незаданное поле в запрос не уходит вовсе, а заданное, но
    отвергнутое endpoint'ом, отбрасывается клиентом и прогон не роняет.
    """

    temperature: float = 0.0
    max_tokens: int = Field(default=2000, gt=0)
    enable_thinking: bool = False
    reasoning_effort: str | None = None


class ModelConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = Field(default=120, gt=0)
    max_retries: int = Field(default=3, ge=0)
    # Цены провайдера за миллион токенов. Нужны, чтобы прогон сам считал
    # стоимость, а не оставлял это умножению в уме.
    price_per_1m_input: float = Field(default=0.0, ge=0)
    price_per_1m_output: float = Field(default=0.0, ge=0)
    # Сколько запросов к этой модели идёт одновременно. Контекстный аудит
    # делает по вызову на слайд, и последовательно это не влезает в бюджет.
    # Значение консервативное: провайдеры ограничивают частоту запросов, и
    # упереться в 429 дороже, чем идти на восьми потоках.
    max_concurrent_calls: int = Field(default=8, gt=0)
    steps: dict[str, StepParams] = Field(default_factory=dict)

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Стоимость по ценам из конфига. Без цен — ноль, а не выдумка."""
        return (
            prompt_tokens * self.price_per_1m_input
            + completion_tokens * self.price_per_1m_output
        ) / 1_000_000

    @property
    def has_prices(self) -> bool:
        return self.price_per_1m_input > 0 or self.price_per_1m_output > 0

    @property
    def configured(self) -> bool:
        """Есть ли всё, чтобы вообще пойти в модель."""
        return bool(self.base_url and self.api_key and self.model)

    def step(self, name: str) -> StepParams:
        """Параметры шага; для неописанного шага — значения по умолчанию."""
        return self.steps.get(name, StepParams())


class ImageProviderConfig(ModelConfig):
    enabled: bool = False


class RunConfig(BaseModel):
    seed: int = 0
    output_dir: Path = Path("outputs")
    time_budget_seconds: int = Field(default=300, gt=0)
    slide_workers: int = Field(default=4, gt=0)
    max_fix_iterations: int = Field(default=2, ge=0)


class TemplateConfig(BaseModel):
    cache_dir: Path = Path(".cache/templates")


class DeckConfig(BaseModel):
    slide_count: int | None = None
    min_slides: int = Field(default=10, gt=0)
    max_slides: int = Field(default=15, gt=0)
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    purpose: str = "product"

    @model_validator(mode="after")
    def _check_bounds(self) -> DeckConfig:
        if self.min_slides > self.max_slides:
            raise ValueError("min_slides не может быть больше max_slides")
        if self.slide_count is not None and not (
            self.min_slides <= self.slide_count <= self.max_slides
        ):
            raise ValueError(
                f"slide_count={self.slide_count} вне границ "
                f"{self.min_slides}..{self.max_slides}"
            )
        return self


class FontsConfig(BaseModel):
    extract_dir: Path = Path(".cache/fonts")
    allow_substitution: bool = True


class RenderConfig(BaseModel):
    soffice_binary: str = "soffice"
    soffice_timeout_seconds: int = Field(default=180, gt=0)
    png_dpi: int = Field(default=96, gt=0)


class AuditConfig(BaseModel):
    contrast_min_ratio: float = Field(default=4.5, gt=0)
    max_bullets_per_slide: int = Field(default=6, gt=0)
    max_words_per_bullet: int = Field(default=15, gt=0)
    max_table_rows: int = Field(default=7, gt=0)
    max_table_cols: int = Field(default=5, gt=0)
    max_chart_series: int = Field(default=5, gt=0)
    min_fill_ratio: float = Field(default=0.25, ge=0, le=1)
    max_fill_ratio: float = Field(default=0.75, ge=0, le=1)
    contextual_enabled: bool = True

    @model_validator(mode="after")
    def _check_fill(self) -> AuditConfig:
        if self.min_fill_ratio >= self.max_fill_ratio:
            raise ValueError("min_fill_ratio должен быть меньше max_fill_ratio")
        return self


class LayoutStrategy(BaseModel):
    """Ось различий между тремя вариантами вёрстки.

    Не три ветки кода, а один параметр: пайплайн один, пресеты разные
    (``configs/variants/*.yaml``).
    """

    slot_fill_target: float = Field(default=0.7, gt=0, le=1)
    type_scale_bias: str = Field(default="mid", pattern=r"^(smaller|mid|larger)$")
    data_viz_mode: str = Field(default="auto", pattern=r"^(table|chart|factoid|auto)$")
    blocks_per_slide: int = Field(default=2, gt=0)
    pattern_preference: list[str] = Field(default_factory=list)


class Variant(BaseModel):
    name: str
    description: str = ""
    strategy: LayoutStrategy = Field(default_factory=LayoutStrategy)


class Config(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    template: TemplateConfig = Field(default_factory=TemplateConfig)
    deck: DeckConfig = Field(default_factory=DeckConfig)
    variants: list[Variant] = Field(default_factory=list)
    llm: ModelConfig = Field(default_factory=ModelConfig)
    vlm: ModelConfig = Field(default_factory=ModelConfig)
    image_provider: ImageProviderConfig = Field(default_factory=ImageProviderConfig)
    fonts: FontsConfig = Field(default_factory=FontsConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    def variant(self, name: str) -> Variant:
        for v in self.variants:
            if v.name == name:
                return v
        known = ", ".join(v.name for v in self.variants) or "—"
        raise KeyError(f"вариант {name!r} не найден; известны: {known}")


def _expand_env(value: Any) -> Any:
    """Подставляет ``${VAR}`` из окружения. Незаданная переменная → пустая строка."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: ожидался словарь на верхнем уровне, получено {type(data).__name__}"
        )
    return data


def load_config(path: str | Path) -> Config:
    """Читает config.yaml, подставляет окружение, подтягивает пресеты вариантов.

    ``variants`` в YAML — список путей к файлам пресетов; пути разрешаются
    относительно каталога самого config.yaml, чтобы конфиг не зависел от того,
    из какого каталога запущен процесс.
    """
    path = Path(path)
    raw = _expand_env(_read_yaml(path))

    variant_refs = raw.pop("variants", []) or []
    variants: list[dict[str, Any]] = []
    for ref in variant_refs:
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            # Сначала рядом с config.yaml, затем от текущего каталога.
            candidate = path.parent / ref_path.name
            ref_path = candidate if candidate.exists() else ref_path
        variants.append(_expand_env(_read_yaml(ref_path)))
    raw["variants"] = variants

    return Config.model_validate(raw)
