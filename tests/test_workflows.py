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


# ── Диагностический вызов ────────────────────────────────────────────────────
#
# Проба ходит в платную модель и запускается руками, поэтому её путь не
# закрыт обычным CI — и один раз это стоило живого вызова впустую. Фаза 5
# добавила планировщику третье возвращаемое значение, вызов в `cli.py`
# остался двухместным, и проба упала с `too many values to unpack` уже
# **после** ответа модели: деньги списаны, план потерян.
#
# Тест идёт на записанных ответах: сеть и ключ не нужны, а несостыковка
# контрактов между CLI и планировщиком ловится на том же коммите.


def test_probe_runs_end_to_end_on_recorded_answers(monkeypatch, tmp_path, recorded_dir):
    import sys as _sys

    import deckwright.llm.client as client_module
    from deckwright.cli import main
    from deckwright.llm.fake import RecordedClient

    _sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    from make_template import build_template

    template = build_template(tmp_path / "probe.pptx")

    class _Recorded(RecordedClient):
        def __init__(self, _cfg):
            super().__init__(recorded_dir)

    monkeypatch.setattr(client_module, "LiveClient", _Recorded)
    monkeypatch.setattr(
        client_module, "probe_endpoint", lambda _cfg: (True, "принят", ["fake-model"])
    )
    monkeypatch.setenv("LLM_API_KEY", "probe-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "fake-model")

    saved = tmp_path / "plan.json"
    code = main(
        [
            "probe",
            "--content",
            str(Path(__file__).parent / "fixtures" / "content_pack.json"),
            "--template",
            str(template),
            "--save",
            str(saved),
        ]
    )
    assert code == 0, "проба завершилась ошибкой на записанных ответах"
    assert saved.exists(), "план не сохранён"
