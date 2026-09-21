"""Бюджеты длины текста, выведенные из самого шаблона.

Зачем это планировщику. Заголовок, не влезший в свою рамку, обходится дорого:
фиттер либо мельчит кегль, либо зовёт модель сократить текст ещё раз — лишние
секунды в бюджете пяти минут и лишние токены в счёте. Написать текст нужной
длины сразу дешевле, чем ужимать потом.

Числа берутся не из головы, а из геометрии шаблона: сколько символов влезает
в его заголовочную рамку при кегле из его же шкалы. У разных шаблонов это
разные числа — у `vk_tech` заголовочная рамка 7.4 дюйма при кегле 18, у
`vk_education` — 11.9 дюйма при 32, и одна и та же формулировка ведёт себя в
них по-разному.

Пороги плотности приходят из Приложения 1 ТЗ через конфиг: не больше шести
буллетов на слайде, буллет не длиннее пятнадцати слов.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from deckwright.layout.text_metrics import FontMetrics, characters_that_fit, metrics_for_spec
from deckwright.schemas import Box, SlotRole, TemplateSpec

# Запас от края рамки: писать в упор нельзя, иначе любая неточность измерения
# выносит текст за границу.
BUDGET_SAFETY = 0.9

# Какую долю продемонстрированных длин бюджет обязан покрывать.
DEMONSTRATION_PERCENTILE = 0.8

# Если у шаблона не нашлось ни одной заголовочной рамки, берём долю слайда.
DEFAULT_TITLE_WIDTH_SHARE = 0.8
DEFAULT_TITLE_HEIGHT_SHARE = 0.15
DEFAULT_BODY_WIDTH_SHARE = 0.8
DEFAULT_BODY_HEIGHT_SHARE = 0.5


@dataclass(frozen=True)
class LengthBudget:
    """Сколько текста уместно писать в этот шаблон."""

    title_chars: int
    subtitle_chars: int
    bullet_chars: int
    max_bullets: int
    max_words_per_bullet: int
    # Чем мерили: шрифтом шаблона или подстановкой. Идёт в манифест.
    measured_with: str

    def as_prompt_lines(self) -> str:
        """Ограничения в том виде, в каком они уходят в промпт."""
        return (
            f"- заголовок слайда: не длиннее {self.title_chars} символов\n"
            f"- подзаголовок: не длиннее {self.subtitle_chars} символов\n"
            f"- пункт списка: не длиннее {self.bullet_chars} символов "
            f"и не длиннее {self.max_words_per_bullet} слов\n"
            f"- пунктов на слайде: не больше {self.max_bullets}"
        )


def _demonstrated_length(spec: TemplateSpec, role: SlotRole) -> int:
    """Сколько символов шаблон сам пишет в слоты этой роли.

    Самое прямое свидетельство из всех: рыбный текст в шаблоне показывает,
    на какую длину рассчитывал дизайнер. У `vk_tech` в заголовочной рамке
    написано «Заголовок в две или в одну строчку» — тридцать четыре символа,
    при том что расчёт по метрикам даёт двадцать. Расчёт осторожен и считает
    строки с округлением вниз; демонстрация точна, потому что она уже
    свёрстана и отрисована.

    Берём максимум из двух оценок: расчёт страхует шаблон без рыбного текста
    (старая презентация пользователя), демонстрация — расчёт от чрезмерной
    осторожности.
    """
    lengths = sorted(
        len(slot.placeholder_text.strip())
        for source in (
            (slot for layout in spec.layouts for slot in layout.slots),
            (slot for pattern in spec.patterns for slot in pattern.slots),
        )
        for slot in source
        if slot.role is role and slot.placeholder_text.strip()
    )
    if not lengths:
        return 0
    # Не медиана: рамка обязана держать самый длинный заголовок, который
    # дизайнер счёл приемлемым, а не средний. Но и не максимум — одна
    # случайно длинная подпись не должна задавать бюджет всей колоде.
    index = min(len(lengths) - 1, int(len(lengths) * DEMONSTRATION_PERCENTILE))
    return lengths[index]


def _median_box(boxes: list[Box], fallback: Box) -> Box:
    """Типичная рамка среди найденных. Медиана, а не среднее: одна рамка во
    весь слайд не должна утягивать оценку за собой."""
    if not boxes:
        return fallback
    return Box(
        x=fallback.x,
        y=fallback.y,
        w=max(1, round(median(box.w for box in boxes))),
        h=max(1, round(median(box.h for box in boxes))),
    )


def _boxes_for(spec: TemplateSpec, role: SlotRole) -> list[Box]:
    """Рамки этой роли: сначала из layout'ов, и только если их нет — из слайдов.

    Смешивать источники нельзя: медиана по объединению берёт ширину у одних
    рамок, а высоту у других, и получается рамка, которой в шаблоне нет. На
    `vk_tech` это давало заголовочную рамку 4.29 на 0.36 дюйма — ширину от
    layout'ов, высоту от подписей внутри карточек.

    Layout объявляет рамку явно и потому авторитетнее; слайды-примеры идут в
    дело, когда шаблон рамок не объявляет вовсе — а это обычный случай.
    """
    from_layouts = [
        slot.box
        for layout in spec.layouts
        for slot in layout.slots
        if slot.role is role
    ]
    if from_layouts:
        return from_layouts
    return [
        slot.box
        for pattern in spec.patterns
        for slot in pattern.slots
        if slot.role is role
    ]


def _scale_size(spec: TemplateSpec, position: float) -> float:
    """Кегль из шкалы шаблона по относительному положению в ней."""
    scale = spec.type_scale_pt or [18.0]
    index = min(len(scale) - 1, max(0, round((len(scale) - 1) * position)))
    return scale[index]


def _slot_size(spec: TemplateSpec, role: SlotRole, position: float) -> float:
    """Кегль, которым шаблон набирает слоты этой роли.

    Берётся медиана реальных кеглей, а не позиция в шкале: верх шкалы занят
    обложечными размерами, и бюджет рабочего заголовка по ним выходит втрое
    меньше настоящего. К шкале обращаемся, только если слоты кегля не
    сообщили.
    """
    sizes = [
        slot.style.size_pt
        for layout in spec.layouts
        for slot in layout.slots
        if slot.role is role and slot.style is not None
    ] or [
        slot.style.size_pt
        for pattern in spec.patterns
        for slot in pattern.slots
        if slot.role is role and slot.style is not None
    ]
    return median(sizes) if sizes else _scale_size(spec, position)


def compute_budget(
    spec: TemplateSpec,
    max_bullets: int,
    max_words_per_bullet: int,
    metrics: FontMetrics | None = None,
    measured_with: str = "",
) -> LengthBudget:
    """Бюджеты длины для этого шаблона.

    Без метрик шрифта измерить нечего, и тогда возвращаются только пороги
    плотности из ТЗ, а длины остаются нулевыми — промпт тогда о них не
    упоминает вовсе, вместо того чтобы называть выдуманное число.
    """
    if metrics is None:
        metrics, measured_with = metrics_for_spec(spec)
    if metrics is None:
        return LengthBudget(0, 0, 0, max_bullets, max_words_per_bullet, measured_with)

    width, height = spec.slide_width_emu, spec.slide_height_emu
    title_box = _median_box(
        _boxes_for(spec, SlotRole.TITLE),
        Box(
            x=0,
            y=0,
            w=round(width * DEFAULT_TITLE_WIDTH_SHARE),
            h=round(height * DEFAULT_TITLE_HEIGHT_SHARE),
        ),
    )
    body_box = _median_box(
        _boxes_for(spec, SlotRole.BODY),
        Box(
            x=0,
            y=0,
            w=round(width * DEFAULT_BODY_WIDTH_SHARE),
            h=round(height * DEFAULT_BODY_HEIGHT_SHARE),
        ),
    )

    title_size = _slot_size(spec, SlotRole.TITLE, 0.85)
    body_size = _slot_size(spec, SlotRole.BODY, 0.45)

    title_chars = max(
        round(
            characters_that_fit(metrics, title_size, title_box.w, title_box.h)
            * BUDGET_SAFETY
        ),
        _demonstrated_length(spec, SlotRole.TITLE),
    )
    body_chars = max(
        round(
            characters_that_fit(metrics, body_size, body_box.w, body_box.h) * BUDGET_SAFETY
        ),
        # Тело показывает длину одного пункта, а не всего списка.
        _demonstrated_length(spec, SlotRole.BODY) * max_bullets,
    )

    return LengthBudget(
        title_chars=max(20, title_chars),
        # Подзаголовок живёт в той же рамке, но мельче и короче заголовка.
        subtitle_chars=max(20, round(title_chars * 0.8)),
        # Тело делится между пунктами: шесть пунктов по всей высоте не влезут.
        bullet_chars=max(20, body_chars // max(1, max_bullets)),
        max_bullets=max_bullets,
        max_words_per_bullet=max_words_per_bullet,
        measured_with=measured_with,
    )
