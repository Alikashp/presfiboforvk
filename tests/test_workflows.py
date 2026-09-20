"""Живой ключ не должен тратиться на каждый коммит.

Проверка инварианта, а не поведения кода: обычный CI обязан оставаться
бесплатным и офлайновым, а всё, что ходит в платную модель, — запускаться
только руками. Разъехаться это может незаметно, одной строкой в YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

# Workflow'ы, которым разрешено обращаться к секретам. Всё остальное обязано
# работать без ключей — на записанных ответах.
PAID = {"llm-probe.yml"}


def _triggers(path: Path) -> list[str]:
    # В YAML голое `on` читается как булево True, а не как строка.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list((data.get("on") or data.get(True) or {}).keys())


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_paid_workflows_touch_secrets(path: Path):
    uses_secrets = "secrets." in path.read_text(encoding="utf-8")
    if path.name in PAID:
        assert uses_secrets, f"{path.name} числится платным, но секретов не использует"
    else:
        assert not uses_secrets, (
            f"{path.name} обращается к секретам, хотя запускается автоматически"
        )


@pytest.mark.parametrize("path", [p for p in WORKFLOWS if p.name in PAID], ids=lambda p: p.name)
def test_paid_workflows_run_only_by_hand(path: Path):
    assert _triggers(path) == ["workflow_dispatch"], (
        f"{path.name} запускается не только вручную: ключ будет тратиться сам по себе"
    )
