"""Проверка целостности пакета OOXML.

Зачем отдельная проверка. Клонирование фигур со слайда-донора тащит за собой
ссылки: картинка, диаграмма, гиперссылка живут не в XML слайда, а в его файле
связей, а в XML стоит только `r:embed="rId5"`. Если фигуру скопировать, а связь
не перенести, `rId5` повиснет в пустоте.

Коварство в том, что **LibreOffice это проглатывает молча**: и конвертация в
PDF, и рендер в PNG проходят, картинка просто не появляется. А PowerPoint на
том же файле показывает «обнаружены неполадки, требуется восстановление». То
есть весь наш путь проверки (pptx → pdf → png → аудит по картинке) слеп ровно к
этому классу поломок, и без явной проверки они доедут до жюри.

Проверяется три вещи:

1. каждый `r:id`/`r:embed`/`r:link`, упомянутый в XML части, объявлен в её
   файле связей;
2. каждая внутренняя связь указывает на часть, которая в пакете есть;
3. каждая часть пакета имеет тип содержимого — иначе PowerPoint её не примет.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# Атрибуты, которыми части ссылаются на свои связи.
_REL_ATTRS = frozenset(
    f"{{{R_NS}}}{name}" for name in ("id", "embed", "link", "pict", "dm", "lo", "qs", "cs")
)

_CONTENT_TYPES = "[Content_Types].xml"


@dataclass
class IntegrityReport:
    """Результат проверки. Пустой `problems` — файл целостен."""

    problems: list[str] = field(default_factory=list)
    parts_checked: int = 0
    rels_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_broken(self, path: Path) -> None:
        if self.problems:
            listed = "\n  - ".join(self.problems)
            raise PackageIntegrityError(f"{path.name}: пакет повреждён\n  - {listed}")


class PackageIntegrityError(RuntimeError):
    """Пакет откроется в PowerPoint с ошибкой восстановления."""


def _rels_path_for(part: str) -> str:
    head, tail = posixpath.split(part)
    return posixpath.join(head, "_rels", tail + ".rels")


def _read_rels(zf: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """{rId: (Target, TargetMode)} для части. Нет файла связей — пустой словарь."""
    rels_path = _rels_path_for(part)
    try:
        raw = zf.read(rels_path)
    except KeyError:
        return {}
    root = etree.fromstring(raw)
    return {
        rel.get("Id"): (rel.get("Target", ""), rel.get("TargetMode", "Internal"))
        for rel in root.findall(f"{{{RELS_NS}}}Relationship")
    }


def _referenced_ids(raw: bytes) -> set[str]:
    """rId, упомянутые в XML части."""
    root = etree.fromstring(raw)
    found: set[str] = set()
    for element in root.iter():
        for attr, value in element.attrib.items():
            if attr in _REL_ATTRS and value:
                found.add(value)
    return found


def _content_type_index(zf: zipfile.ZipFile) -> tuple[set[str], set[str]]:
    """(расширения с Default, части с Override) из [Content_Types].xml."""
    try:
        root = etree.fromstring(zf.read(_CONTENT_TYPES))
    except KeyError:
        return set(), set()
    defaults = {
        (d.get("Extension") or "").lower() for d in root.findall(f"{{{CT_NS}}}Default")
    }
    overrides = {
        (o.get("PartName") or "").lstrip("/") for o in root.findall(f"{{{CT_NS}}}Override")
    }
    return defaults, overrides


def check_package(path: str | Path) -> IntegrityReport:
    """Проверяет .pptx на висящие ссылки и части без типа содержимого."""
    path = Path(path)
    report = IntegrityReport()

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        defaults, overrides = _content_type_index(zf)
        if _CONTENT_TYPES not in names:
            report.problems.append(f"нет {_CONTENT_TYPES}")
            return report

        xml_parts = [n for n in sorted(names) if n.endswith(".xml") and "/_rels/" not in n]

        for part in xml_parts:
            report.parts_checked += 1
            rels = _read_rels(zf, part)
            report.rels_checked += len(rels)

            # 1. Ссылки из XML обязаны быть объявлены в связях этой части.
            for rid in sorted(_referenced_ids(zf.read(part))):
                if rid not in rels:
                    report.problems.append(
                        f"{part}: ссылка {rid} не объявлена в {_rels_path_for(part)}"
                    )

            # 2. Внутренние связи обязаны указывать на существующие части.
            base = posixpath.dirname(part)
            for rid, (target, mode) in sorted(rels.items()):
                if mode == "External" or not target:
                    continue
                resolved = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join(base, target))
                )
                if resolved not in names:
                    report.problems.append(
                        f"{_rels_path_for(part)}: {rid} указывает на {resolved!r}, "
                        "такой части в пакете нет"
                    )

        # 3. Каждая часть пакета обязана иметь тип содержимого.
        for name in sorted(names):
            if name == _CONTENT_TYPES or name.endswith("/"):
                continue
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if name not in overrides and extension not in defaults:
                report.problems.append(f"{name}: нет типа содержимого")

    return report
