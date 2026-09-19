"""Общие фикстуры. Шаблоны берутся из каталога, заданного окружением.

Шаблоны датасета в репозиторий не коммитятся: это 58 МБ чужих материалов.
Путь задаётся переменной `DECKWRIGHT_TEMPLATES`; если его нет, тесты,
которым нужен настоящий `.pptx`, пропускаются, а остальные идут. В CI
переменная указывает на синтетические шаблоны, собираемые на месте, —
так сквозной тест не зависит от датасета и заодно проверяет обобщаемость.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def templates_dir() -> Path:
    raw = os.environ.get("DECKWRIGHT_TEMPLATES")
    if not raw:
        pytest.skip("DECKWRIGHT_TEMPLATES не задан: тестам нужен каталог с .pptx")
    directory = Path(raw)
    if not directory.is_dir():
        pytest.skip(f"DECKWRIGHT_TEMPLATES={raw} — не каталог")
    return directory


@pytest.fixture(scope="session")
def template_paths(templates_dir: Path) -> list[Path]:
    paths = sorted(templates_dir.glob("*.pptx"))
    if not paths:
        pytest.skip(f"в {templates_dir} нет ни одного .pptx")
    return paths


@pytest.fixture(scope="session")
def content_pack_path() -> Path:
    return Path(__file__).parent / "fixtures" / "content_pack.json"


@pytest.fixture(scope="session")
def recorded_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "recorded"
