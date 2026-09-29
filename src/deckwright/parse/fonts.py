"""Извлечение встроенных в шаблон шрифтов.

Шрифты внутри `.pptx` лежат в `ppt/fonts/*.fntdata` и сжаты. Это не TTF:
первые четыре байта — размер файла, дальше заголовок EOT версии 0x00020002 с
флагом сжатия MicroType Express. Сигнатуры `\\x00\\x01\\x00\\x00` в файле нет,
срезом заголовка шрифт не достать.

Зачем это нужно. Шрифта шаблона в системе почти наверняка нет: `Play` из всех
трёх шаблонов датасета отсутствует в базовом образе, и LibreOffice молча
подставляет свой. Тогда расходятся три вещи сразу — PDF, картинки слайдов для
аудита и измерение текста по метрикам, на котором держится детерминированное
разрешение переполнения. Подставленный шрифт шире или уже настоящего, и
рассчитанная вёрстка перестаёт соответствовать тому, что увидит человек.

Распаковка идёт через `libeot` — она есть в дистрибутивах как разделяемая
библиотека и приезжает вместе с LibreOffice. Отдельного исполняемого файла
`eot2ttf` в пакетах нет, поэтому вызов идёт напрямую через `ctypes`.
"""

from __future__ import annotations

import ctypes
import zipfile
from dataclasses import dataclass
from pathlib import Path

LIBEOT_SONAME = "libeot.so.0"

_u8, _u16, _u32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32


class _EUDCInfo(ctypes.Structure):
    _fields_ = [
        ("exists", ctypes.c_bool),
        ("codePage", _u32),
        ("flags", _u32),
        ("fontDataSize", _u32),
        ("fontData", ctypes.POINTER(_u8)),
    ]


class _RootString(ctypes.Structure):
    _fields_ = [("rootStringSize", _u16), ("rootString", ctypes.POINTER(_u16))]


class _EOTMetadata(ctypes.Structure):
    """Раскладка `struct EOTMetadata` из заголовков libeot.

    Порядок полей обязан совпадать с библиотекой: ctypes ничего не проверяет,
    и расхождение даёт не ошибку, а мусор в именах и размерах.
    """

    _fields_ = [
        ("totalSize", _u32),
        ("version", ctypes.c_int),
        ("flags", _u32),
        ("panose", _u8 * 10),
        ("charset", ctypes.c_int),
        ("italic", ctypes.c_bool),
        ("weight", _u32),
        ("permissions", _u16),
        ("unicodeRange", _u32 * 4),
        ("codePageRange", _u32 * 2),
        ("checkSumAdjustment", _u32),
        ("familyNameSize", _u16),
        ("familyName", ctypes.POINTER(_u16)),
        ("styleNameSize", _u16),
        ("styleName", ctypes.POINTER(_u16)),
        ("versionNameSize", _u16),
        ("versionName", ctypes.POINTER(_u16)),
        ("fullNameSize", _u16),
        ("fullName", ctypes.POINTER(_u16)),
        ("numRootStrings", ctypes.c_uint),
        ("rootStrings", ctypes.POINTER(_RootString)),
        ("fontDataSize", _u32),
        ("fontDataOffset", ctypes.c_uint),
        ("eudcInfo", _EUDCInfo),
        ("do_not_use_size", _u16),
        ("do_not_use", ctypes.POINTER(_u16)),
    ]


@dataclass(frozen=True)
class ExtractedFont:
    family: str
    style: str
    path: Path

    @property
    def bold(self) -> bool:
        return "bold" in self.style.lower()

    @property
    def italic(self) -> bool:
        return "italic" in self.style.lower() or "oblique" in self.style.lower()


class FontExtractionError(RuntimeError):
    """Встроенный шрифт не распаковался."""


def _library() -> ctypes.CDLL:
    try:
        lib = ctypes.CDLL(LIBEOT_SONAME)
    except OSError as exc:
        raise FontExtractionError(
            f"{LIBEOT_SONAME} недоступна: без неё встроенные шрифты не извлечь, "
            "и вёрстка будет считаться по метрикам подставленного шрифта"
        ) from exc
    lib.EOT2ttf_buffer.argtypes = [
        ctypes.POINTER(_u8),
        ctypes.c_uint,
        ctypes.POINTER(_EOTMetadata),
        ctypes.POINTER(ctypes.POINTER(_u8)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.EOT2ttf_buffer.restype = ctypes.c_int
    lib.EOTfreeBuffer.argtypes = [ctypes.POINTER(_u8)]
    return lib


def _utf16(pointer, size_bytes: int) -> str:
    """Имя из заголовка EOT: без завершающего и любого другого нулевого символа.

    Размер имени в EOT бывает с завершающим нулём, а бывает имя, забитое
    нулями до длины поля: шаблон «Презентация 2.0» падал на разборе с
    `ValueError: embedded null byte` — ноль из имени шрифта попадал в путь.
    """
    if not pointer or size_bytes <= 0:
        return ""
    return "".join(chr(pointer[i]) for i in range(size_bytes // 2)).replace("\x00", "").strip()


# Символы, которых не бывает в имени файла: разделители путей, управляющие,
# запрещённые в Windows. Имя шрифта из файла шаблона — чужие данные.
_UNSAFE = frozenset('/\\:*?"<>|') | {chr(code) for code in range(32)} | {"\x7f"}


def safe_name(name: str, fallback: str = "font") -> str:
    """Имя, пригодное для файла или каталога: без нулей, разделителей и точек в начале."""
    cleaned = "".join("_" if char in _UNSAFE or char.isspace() else char for char in name)
    cleaned = cleaned.strip("._")
    return cleaned[:100] or fallback


def eot_to_ttf(data: bytes) -> tuple[bytes, str, str]:
    """Распаковывает EOT в TTF. Возвращает (шрифт, семейство, начертание)."""
    lib = _library()
    buffer = (_u8 * len(data)).from_buffer_copy(data)
    metadata = _EOTMetadata()
    out = ctypes.POINTER(_u8)()
    size = ctypes.c_uint()

    code = lib.EOT2ttf_buffer(
        buffer, len(data), ctypes.byref(metadata), ctypes.byref(out), ctypes.byref(size)
    )
    if code != 0 or not size.value:
        raise FontExtractionError(f"libeot вернула код {code}")

    try:
        ttf = bytes(bytearray(out[i] for i in range(size.value)))
    finally:
        lib.EOTfreeBuffer(out)

    return (
        ttf,
        _utf16(metadata.familyName, metadata.familyNameSize),
        _utf16(metadata.styleName, metadata.styleNameSize),
    )


def extract_embedded_fonts(
    template_path: str | Path, target_dir: str | Path
) -> tuple[list[ExtractedFont], list[str]]:
    """Достаёт все встроенные шрифты шаблона. Возвращает (шрифты, предупреждения).

    Неудача по одному шрифту не отменяет остальные: лучше иметь верные метрики
    для трёх начертаний из четырёх, чем ни для одного.

    Ни один шрифт не роняет разбор: битый архив, имя с нулём, запрет записи —
    шрифт пропускается с предупреждением, вёрстка идёт по метрикам замены.
    """
    template_path = Path(template_path)
    target_dir = Path(target_dir)
    target_dir = target_dir.parent / safe_name(target_dir.name, "template")

    fonts: list[ExtractedFont] = []
    warnings: list[str] = []
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(template_path) as package:
            entries = sorted(n for n in package.namelist() if n.startswith("ppt/fonts/"))
            for entry in entries:
                try:
                    ttf, family, style = eot_to_ttf(package.read(entry))
                    if not family:
                        warnings.append(f"{entry}: распаковался, но без имени семейства")
                        continue
                    style = style or "Regular"
                    path = target_dir / f"{safe_name(family)}-{safe_name(style, 'Regular')}.ttf"
                    path.write_bytes(ttf)
                except (FontExtractionError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    warnings.append(f"{entry}: шрифт пропущен — {exc}")
                    continue
                fonts.append(ExtractedFont(family=family, style=style, path=path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        warnings.append(f"встроенные шрифты не извлечены — {exc}")
    return fonts, warnings
