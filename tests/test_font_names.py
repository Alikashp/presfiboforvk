"""Встроенные шрифты с чужими именами не роняют разбор шаблона.

Шаблон «Презентация 2.0» падал на сайте с `ValueError: embedded null byte`:
имя семейства из заголовка EOT несло нулевой символ, и он попадал в путь
файла шрифта. Имена шрифта и шаблона — чужие данные, в путь они идут только
очищенными; неудача по шрифту — предупреждение, а не падение.
"""

from __future__ import annotations

import ctypes
import zipfile
from pathlib import Path

import pytest

from deckwright.parse import fonts
from deckwright.parse.fonts import _utf16, extract_embedded_fonts, safe_name

ROOT = Path(__file__).resolve().parents[1]
NULL_FONT_TEMPLATE = Path(__file__).parent / "fixtures" / "null_font_name.pptx"


def test_utf16_name_drops_null_characters():
    raw = "Montserrat\x00\x00"
    buffer = (ctypes.c_uint16 * len(raw))(*(ord(char) for char in raw))
    assert _utf16(buffer, len(raw) * 2) == "Montserrat"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Montserrat\x00", "Montserrat"),
        ("Open Sans", "Open_Sans"),
        ("../../etc/passwd", "etc_passwd"),
        ('Шрифт: «новый»*?', "Шрифт__«новый»"),
        ("\x00\x00", "font"),
        ("..", "font"),
    ],
)
def test_names_are_safe_for_files(name, expected):
    assert safe_name(name) == expected
    assert "\x00" not in safe_name(name) and "/" not in safe_name(name)


def test_null_byte_in_family_name_does_not_crash(tmp_path, monkeypatch):
    """Имя семейства с нулём — файл с очищенным именем, прогон идёт дальше."""
    template = tmp_path / "t.pptx"
    with zipfile.ZipFile(template, "w") as package:
        package.writestr("ppt/fonts/font1.fntdata", b"x")
    monkeypatch.setattr(fonts, "eot_to_ttf", lambda data: (b"ttf", "Mont\x00serrat", "Bold\x00"))
    extracted, warnings = extract_embedded_fonts(template, tmp_path / "out")
    assert [font.path.name for font in extracted] == ["Mont_serrat-Bold.ttf"]
    assert warnings == []


def test_font_that_cannot_be_written_is_skipped_with_a_warning(tmp_path, monkeypatch):
    template = tmp_path / "t.pptx"
    with zipfile.ZipFile(template, "w") as package:
        package.writestr("ppt/fonts/font1.fntdata", b"x")
        package.writestr("ppt/fonts/font2.fntdata", b"y")

    def broken(data):
        if data == b"x":
            raise ValueError("embedded null byte")
        return b"ttf", "Play", "Regular"

    monkeypatch.setattr(fonts, "eot_to_ttf", broken)
    extracted, warnings = extract_embedded_fonts(template, tmp_path / "out")
    assert [font.family for font in extracted] == ["Play"]
    assert len(warnings) == 1 and "font1" in warnings[0]


def test_template_file_name_with_odd_characters(tmp_path, monkeypatch):
    """Имя файла шаблона — тоже в путь: кириллица, пробелы, кавычки, ноль."""
    template = tmp_path / "Презентация 2.0 «итог».pptx"
    with zipfile.ZipFile(template, "w") as package:
        package.writestr("ppt/fonts/font1.fntdata", b"x")
    monkeypatch.setattr(fonts, "eot_to_ttf", lambda data: (b"ttf", "Play", "Regular"))
    extracted, warnings = extract_embedded_fonts(template, tmp_path / "out" / "a\x00b")
    assert extracted and warnings == []
    assert "\x00" not in str(extracted[0].path)


def test_template_whose_font_name_carries_a_null_parses(tmp_path):
    """Шаблон, на котором сайт упал, разбирается целиком."""
    if not NULL_FONT_TEMPLATE.exists():
        pytest.skip("нет шаблона с нулём в имени шрифта")
    from deckwright.parse.opener import parse_template

    spec = parse_template(NULL_FONT_TEMPLATE, font_dir=tmp_path)
    assert spec.patterns
