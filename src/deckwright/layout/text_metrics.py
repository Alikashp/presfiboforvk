"""Измерение текста по метрикам шрифта.

На autofit PowerPoint полагаться нельзя: он подбирает кегль сам, по своим
правилам, в момент открытия файла — и подбирает его из любых значений, а не из
типографической шкалы шаблона. Слайд, свёрстанный в расчёте на autofit,
открывается разным у разных людей и нарушает шкалу, что справедливо найдёт
проверка «кегль не из шкалы шаблона».

Поэтому текст меряется здесь, до записи в файл, по метрикам того самого
шрифта, который будет его набирать. Шрифт берётся извлечённым из шаблона
(`parse/fonts.py`); если извлечь не удалось, подставляется системный, и это
записывается в манифест — подставленный шрифт шире или уже настоящего, и
рассчитанная вёрстка перестаёт соответствовать увиденному.

Точность. Считается сумма ширин глифов без кернинга и без сложного шейпинга:
для кириллицы и латиницы это даёт погрешность порядка двух-трёх процентов в
меньшую сторону. Погрешность гасится запасом при подгонке, а не игнорируется.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

from deckwright.schemas import EMU_PER_POINT

# Запас на кернинг и шейпинг, которых упрощённое измерение не видит.
MEASUREMENT_SLACK = 1.03

# Доля кегля, уходящая на межстрочный интервал по умолчанию.
DEFAULT_LINE_HEIGHT = 1.2

# Внутренние поля текстового фрейма PowerPoint по умолчанию: 0.1 дюйма слева и
# справа, 0.05 сверху и снизу. Не учитывать их — значит систематически считать,
# что в бокс влезает больше, чем влезает.
FRAME_INSET_X_EMU = 91_440
FRAME_INSET_Y_EMU = 45_720

# Ширина глифа, которого в шрифте нет, — половина кегля. Грубо, но лучше, чем
# считать такой символ нулевым.
FALLBACK_ADVANCE_RATIO = 0.5


class FontUnavailable(RuntimeError):
    """Шрифт для измерения не найден."""


@dataclass(frozen=True)
class FontMetrics:
    """Метрики одного начертания, достаточные для измерения строки."""

    family: str
    units_per_em: int
    advances: dict[int, int]
    path: str

    def advance(self, char: str) -> int:
        return self.advances.get(
            ord(char), round(self.units_per_em * FALLBACK_ADVANCE_RATIO)
        )

    def width_pt(self, text: str, size_pt: float) -> float:
        """Ширина строки в пунктах при заданном кегле."""
        if not text:
            return 0.0
        total = sum(self.advance(char) for char in text)
        return total / self.units_per_em * size_pt

    def width_emu(self, text: str, size_pt: float) -> int:
        return round(self.width_pt(text, size_pt) * EMU_PER_POINT)


@lru_cache(maxsize=32)
def load_metrics(path: str) -> FontMetrics:
    """Читает метрики шрифта. Результат кэшируется: файл один на весь прогон."""
    font_path = Path(path)
    if not font_path.exists():
        raise FontUnavailable(f"шрифт не найден: {path}")
    try:
        font = TTFont(str(font_path), lazy=True)
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]
        units = font["head"].unitsPerEm
        advances = {
            code: hmtx[glyph][0] for code, glyph in cmap.items() if glyph in hmtx.metrics
        }
        family = str(
            next(
                (record for record in font["name"].names if record.nameID == 1),
                "",
            )
        )
    except Exception as exc:  # повреждённый шрифт — не повод ронять прогон
        raise FontUnavailable(f"{path}: метрики не читаются ({exc})") from exc
    return FontMetrics(family=family, units_per_em=units, advances=advances, path=path)


def wrap(text: str, metrics: FontMetrics, size_pt: float, width_emu: int) -> list[str]:
    """Разбивает строку по словам так, как её перенесёт PowerPoint.

    Слово длиннее строки не режется: PowerPoint его тоже не режет, а
    выпускает за край. Пусть проверка «текст не поместился» это и увидит.
    """
    limit = width_emu / MEASUREMENT_SLACK
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and metrics.width_emu(candidate, size_pt) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def measure_height_emu(
    text: str,
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> int:
    """Высота, которую текст займёт в боксе такой ширины."""
    usable = max(1, width_emu - FRAME_INSET_X_EMU)
    lines = wrap(text, metrics, size_pt, usable)
    return round(len(lines) * size_pt * line_height * EMU_PER_POINT)


def fits(
    text: str,
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    height_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> bool:
    usable_height = max(1, height_emu - FRAME_INSET_Y_EMU)
    return measure_height_emu(text, metrics, size_pt, width_emu, line_height) <= usable_height


def characters_that_fit(
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    height_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> int:
    """Сколько примерно символов помещается в бокс.

    Нужно планировщику: дешевле сразу написать текст нужной длины, чем потом
    ужимать его фиттером — ужимание либо мельчит кегль, либо зовёт модель ещё
    раз, и то и другое дороже.

    Считается по средней ширине символа этого шрифта, а не по абстрактной:
    у узкой гарнитуры в ту же строку влезает заметно больше.
    """
    if not metrics.advances:
        return 0
    average = sum(metrics.advances.values()) / len(metrics.advances)
    char_emu = average / metrics.units_per_em * size_pt * EMU_PER_POINT
    if char_emu <= 0:
        return 0

    usable_width = max(1, width_emu - FRAME_INSET_X_EMU)
    usable_height = max(1, height_emu - FRAME_INSET_Y_EMU)
    line_emu = size_pt * line_height * EMU_PER_POINT
    lines = max(1, int(usable_height // line_emu))
    per_line = max(1, int(usable_width / char_emu / MEASUREMENT_SLACK))
    return lines * per_line


# Куда смотреть за подстановкой, когда шрифт шаблона извлечь не удалось.
# Порядок не случаен: сначала метрически близкие к распространённым
# гарнитурам, потом что угодно с кириллицей.
FALLBACK_FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts")
FALLBACK_PREFERENCE = ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf")


def _system_font() -> str | None:
    for directory in FALLBACK_FONT_DIRS:
        root = Path(directory)
        if not root.is_dir():
            continue
        for name in FALLBACK_PREFERENCE:
            found = next(root.rglob(name), None)
            if found is not None:
                return str(found)
        any_ttf = next(root.rglob("*.ttf"), None)
        if any_ttf is not None:
            return str(any_ttf)
    return None


def metrics_for_spec(spec) -> tuple[FontMetrics | None, str]:
    """Метрики основной гарнитуры шаблона. Возвращает (метрики, пояснение).

    Предпочтение — шрифту, извлечённому из шаблона: только он даёт те самые
    ширины, по которым дизайнер верстал. Подстановка возможна, но она обязана
    быть названа: расчёт на её метриках расходится с тем, что увидит человек.
    """
    for token in spec.fonts:
        if token.embedded and token.file_path:
            try:
                return load_metrics(token.file_path), f"шрифт шаблона {token.family}"
            except FontUnavailable:
                continue

    fallback = _system_font()
    if fallback is None:
        return None, "шрифтов нет вовсе: измерить текст нечем"
    wanted = spec.fonts[0].family if spec.fonts else "?"
    try:
        metrics = load_metrics(fallback)
    except FontUnavailable as exc:
        return None, str(exc)
    return metrics, f"подстановка {metrics.family} вместо {wanted}"
