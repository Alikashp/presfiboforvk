"""Контракт конфигурации: поставляемый config.yaml грузится и типизируется.

Воспроизводимый запуск одним конфиг-файлом — критерий A22 спеки, поэтому
поставляемый конфиг обязан грузиться без правок и без переменных окружения.
"""

from pathlib import Path

import pytest

from deckwright.config import load_config

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "config.yaml"


@pytest.fixture
def cfg(monkeypatch):
    # Прогон без окружения: секретов нет, конфиг всё равно обязан грузиться,
    # потому что разбор шаблона от доступа к модели не зависит.
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return load_config(CONFIG)


def test_shipped_config_loads(cfg):
    assert cfg.run.time_budget_seconds == 300
    assert cfg.deck.min_slides <= cfg.deck.max_slides


def test_three_variants_with_distinct_strategies(cfg):
    """Три варианта вёрстки — один пайплайн с разными пресетами (A12)."""
    names = [v.name for v in cfg.variants]
    assert names == ["dense", "balanced", "airy"]

    fills = [v.strategy.slot_fill_target for v in cfg.variants]
    assert len(set(fills)) == 3, "варианты обязаны различаться по оси плотности"

    viz = {v.strategy.data_viz_mode for v in cfg.variants}
    assert len(viz) > 1, "варианты обязаны различаться по способу визуализации данных"


def test_env_refs_expand(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret-from-env")
    cfg = load_config(CONFIG)
    assert cfg.llm.api_key == "secret-from-env"


def test_missing_env_is_not_fatal(cfg):
    """Без ключей конфиг грузится, но модель помечена как ненастроенная."""
    assert cfg.llm.api_key == ""
    assert cfg.llm.configured is False


def test_step_params_fall_back_for_unknown_step(cfg):
    assert cfg.llm.step("plan_deck").enable_thinking is False
    # reasoning_effort не задан => в запрос не уйдёт вовсе
    assert cfg.llm.step("plan_deck").reasoning_effort is None
    assert cfg.llm.step("нет-такого-шага").max_tokens > 0


def test_unknown_variant_reports_known_ones(cfg):
    with pytest.raises(KeyError, match="dense"):
        cfg.variant("no-such-variant")


def test_image_pass_covers_what_text_cannot(cfg):
    """Картиночный проход обязателен и непуст.

    Текст SlideIR показывает намерение, а не результат: обрезанный краем
    слайда текст, наложившиеся блоки и подставленный не тот элемент в нём не
    видны вовсе. ТЗ задаёт картинку слайда входом для валидации контента.
    """
    audit = cfg.audit
    by_image = audit.checks_by_mode("image")
    assert by_image, "ни одна контекстная проверка не идёт по картинке"
    assert "content.body_matches_title" in by_image
    assert "content.visuals_on_topic" in by_image
    assert "content.no_prompt_leftovers" in by_image


def test_moving_every_check_to_text_is_rejected():
    """Конфиг, отключающий картинку целиком, не должен грузиться."""
    from pydantic import ValidationError

    from deckwright.config import AuditConfig

    with pytest.raises(ValidationError, match="картиночный проход обязателен"):
        AuditConfig(contextual_checks={"content.no_typos": "text"})


def test_unknown_check_mode_is_rejected():
    from pydantic import ValidationError

    from deckwright.config import AuditConfig

    with pytest.raises(ValidationError, match="image или text"):
        AuditConfig(contextual_checks={"content.no_typos": "vlm"})


# ── Конфиг не имеет права обещать поведение, которого нет ────────────────────
#
# Это случилось четырежды подряд, и каждый раз выглядело одинаково: ключ в
# `config.yaml` есть, комментарий объясняет, что он делает, а в коде его никто
# не читает — либо читает под другим именем. Цена — впустую потраченное время
# на «почему настройка не действует».
#
#   audit_context / layout_semantics  — параметры шагов под именами, которых
#                                       код не зовёт (он зовёт audit_slide и
#                                       audit_deck);
#   ModelUsage.steps                  — поле манифеста, всегда пустое;
#   text_checks_once_per_deck         — флаг стоял true, текстовый проход шёл
#                                       на каждый вариант;
#   contrast_min_ratio                — порог в конфиге, а в проверке
#                                       захардкоженная константа.
#
# Лечится двумя правилами: незнакомый ключ роняет загрузку (extra="forbid"), а
# знакомый, но никем не читаемый, роняет этот тест.


def _config_fields(model, prefix: str = "") -> list[tuple[str, str]]:
    """Все поля конфига с путями: ('audit.contrast_min_ratio', 'contrast_min_ratio')."""
    from pydantic import BaseModel

    found: list[tuple[str, str]] = []
    for name, info in model.model_fields.items():
        found.append((f"{prefix}{name}", name))
        annotation = info.annotation
        for candidate in (annotation, *getattr(annotation, "__args__", ())):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                found.extend(_config_fields(candidate, f"{prefix}{name}."))
    return found


def test_every_config_key_is_read_by_someone():
    """Ключ, которого никто не читает, — это обещание, которого нет."""
    from deckwright.config import Config

    source = "\n".join(
        path.read_text("utf-8")
        for path in (Path(__file__).resolve().parents[1] / "src" / "deckwright").rglob("*.py")
    )
    unused = [
        path
        for path, field in _config_fields(Config)
        if f".{field}" not in source
        and f'"{field}"' not in source
        and f"'{field}'" not in source
    ]
    assert not unused, (
        f"эти ключи конфига не читает никто: {unused}. Либо подключите их, "
        "либо уберите: молчаливое умолчание выглядит как настройка"
    )


def test_unknown_config_key_is_a_loud_error():
    """Опечатка в имени ключа не должна тихо откатываться к умолчанию."""
    from deckwright.config import Config

    with pytest.raises(Exception, match="max_fix_iteraions"):
        Config.model_validate({"run": {"max_fix_iteraions": 3}})
