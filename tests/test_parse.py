"""Контракт парсера: произвольный .pptx → валидный `TemplateSpec`.

Проверяются инварианты, которые обязаны держаться на **любом** шаблоне, а не
значения, снятые с трёх присланных файлов. Ожидание вида «палитра начинается с
0077FF» было бы тем самым оверфитом, который запрещён: на финале шаблон будет
другой.
"""

from __future__ import annotations

import pytest

from deckwright.parse.geometry import IDENTITY, Transform, shape_box
from deckwright.parse.opener import parse_template
from deckwright.parse.tokens import Usage, build_fonts, build_grid, build_type_scale
from deckwright.schemas import SlotRole


@pytest.fixture(scope="module")
def specs(template_paths):
    return [parse_template(path, font_dir=None) for path in template_paths]


# ── Контракт: что обязано быть в TemplateSpec (критерий A2) ──────────────────

def test_every_template_yields_a_usable_spec(specs, template_paths):
    for spec, path in zip(specs, template_paths, strict=True):
        assert spec.source_name == path.name
        assert spec.slide_width_emu > 0 and spec.slide_height_emu > 0
        assert spec.palette, f"{path.name}: палитра пуста"
        assert spec.fonts, f"{path.name}: гарнитуры не найдены"
        assert spec.type_scale_pt, f"{path.name}: типографическая шкала пуста"
        assert spec.layouts, f"{path.name}: layout'ы не разобраны"


def test_margins_leave_room_for_content(specs):
    """Поля обязаны быть меньше половины слайда.

    Кластеризация рёбер однажды дала левое поле 4.69 дюйма и нижнее 7.51 при
    высоте слайда 7.5: выбиралась самая населённая направляющая, а не самая
    близкая к краю. Такие поля не оставляют места содержанию.
    """
    for spec in specs:
        grid = spec.grid
        if grid is None:
            continue
        assert grid.margin_left_emu + grid.margin_right_emu < spec.slide_width_emu * 0.5
        assert grid.margin_top_emu + grid.margin_bottom_emu < spec.slide_height_emu * 0.5


def test_type_scale_is_ordered_and_compact(specs):
    """Шкала — набор ступеней, а не перечень всех встреченных кеглей."""
    for spec in specs:
        assert spec.type_scale_pt == sorted(set(spec.type_scale_pt))
        assert len(spec.type_scale_pt) <= 20, "это уже не шкала, а список значений"


def test_patterns_stay_inside_the_slide(specs):
    """Область содержания обязана помещаться на слайде.

    Дизайнеры выпускают декоративные фигуры за обрез, и объемлющий
    прямоугольник слайда-примера выходит за холст. Положить туда содержание
    значит отправить его часть за край.
    """
    for spec in specs:
        for pattern in spec.patterns:
            area = pattern.content_area
            assert area.x >= 0 and area.y >= 0, f"{pattern.id}: область начинается за краем"
            assert area.right <= spec.slide_width_emu, f"{pattern.id}: шире слайда"
            assert area.bottom <= spec.slide_height_emu, f"{pattern.id}: выше слайда"


def test_patterns_have_something_to_fill(specs):
    for spec in specs:
        for pattern in spec.patterns:
            assert pattern.capacity > 0, f"{pattern.id} нечем заполнять"


def test_recurring_elements_carry_content(specs):
    """Логотип и колонтитул — картинка или текст, а не декоративная точка."""
    for spec in specs:
        for element in spec.recurring:
            assert element.frequency >= 0.5
            assert element.text is not None or element.role is SlotRole.LOGO
            assert element.box.right <= spec.slide_width_emu
            assert element.box.bottom <= spec.slide_height_emu


def test_template_without_content_placeholders_is_reported(specs):
    """Шаблон, у которого layout'ы несут только заголовок, обязан быть разобран
    и обязан об этом сказать: вёрстке придётся опираться на паттерны."""
    for spec in specs:
        content_roles = {
            slot.role
            for layout in spec.layouts
            for slot in layout.slots
            if slot.role not in (SlotRole.TITLE, SlotRole.FOOTER, SlotRole.SLIDE_NUMBER)
        }
        if not content_roles:
            assert any("плейсхолдеров" in w for w in spec.warnings)
            assert spec.patterns, "без плейсхолдеров паттерны обязаны быть найдены"


def test_cache_returns_the_same_spec(template_paths, tmp_path):
    first = parse_template(template_paths[0], cache_dir=tmp_path)
    second = parse_template(template_paths[0], cache_dir=tmp_path)
    assert first == second
    assert list(tmp_path.glob("*.json")), "кэш не записан"


# ── Геометрия групп ──────────────────────────────────────────────────────────

def test_group_children_are_placed_by_the_group_transform():
    """Без пересчёта дети всех групп сетки оказываются в одном месте.

    На карточной сетке это давало бы наложения, которых на слайде нет, и
    детерминированные проверки аудита находили бы их пачками.
    """
    from lxml import etree

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    xml = f"""
    <sp xmlns:a="{A}"><spPr><a:xfrm>
      <a:off x="100" y="200"/><a:ext cx="300" cy="400"/>
    </a:xfrm></spPr></sp>
    """
    element = etree.fromstring(xml)

    assert shape_box(element, IDENTITY).x == 100

    # Группа стоит на 1000 и вдвое растягивает своих детей, чьи координаты
    # заданы от нуля.
    moved = Transform(offset_x=1000, offset_y=0, scale_x=2.0, scale_y=2.0, child_x=0, child_y=0)
    box = shape_box(element, moved)
    assert box.x == 1200  # 1000 + 100 * 2
    assert box.w == 600  # 300 * 2


def test_rotated_shape_reports_the_space_it_really_occupies():
    """Повёрнутый блок занимает больше своих ширины и высоты.

    Без этого проверка «элемент вышел за границы слайда» не замечает блок,
    который краем слайд пересекает.
    """
    from lxml import etree

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    xml = f"""
    <sp xmlns:a="{A}"><spPr><a:xfrm rot="2700000">
      <a:off x="0" y="0"/><a:ext cx="1000" cy="100"/>
    </a:xfrm></spPr></sp>
    """
    box = shape_box(etree.fromstring(xml), IDENTITY)
    assert box.h > 100, "поворот на 45° обязан увеличить высоту"


# ── Токены на вырожденных входах ─────────────────────────────────────────────

def test_empty_usage_does_not_invent_tokens():
    """Пустой шаблон не должен порождать выдуманную сетку и шкалу."""
    empty = Usage()
    assert build_type_scale(empty) == []
    assert build_grid(empty) is None
    assert build_fonts(empty, []) == []


def test_declared_fonts_rank_below_used_ones():
    """Гарнитура, объявленная мастером, но не употреблённая, не должна
    становиться гарнитурой шаблона: у двух шаблонов датасета мастер щедро
    объявляет Calibri, а набрано всё шрифтом Play."""
    usage = Usage()
    usage.fonts["Play"] = 10
    usage.fonts_declared["Calibri"] = 1000
    families = [token.family for token in build_fonts(usage, ["Arial"])]
    assert families[0] == "Play"
    assert families[-1] == "Arial"


def test_layout_inherits_its_size_from_the_master(template_paths):
    """Кегль, не объявленный в layout'е, берётся из `p:txStyles` мастера.

    Так его разрешает PowerPoint. Пока вместо этого подставлялась середина
    типографической шкалы, заголовки чужого шаблона считались набранными
    двадцатым кеглем вместо сорок четвёртого — и бюджет длины заголовка
    вырастал с тридцати символов до двухсот семидесяти семи, то есть просил
    у модели абзац в рамку на одну строку.
    """
    import re
    import zipfile

    for path in template_paths:
        spec = parse_template(path)
        with zipfile.ZipFile(path) as archive:
            master = archive.read("ppt/slideMasters/slideMaster1.xml").decode("utf-8")
        block = re.search(r"<p:titleStyle>.*?</p:titleStyle>", master, re.S)
        if block is None:
            continue
        found = re.search(r'sz="(\d+)"', block.group(0))
        if found is None:
            continue
        declared = int(found.group(1)) / 100

        # Наследовать нечего, если каждый layout объявил кегль сам. У
        # `vk_tech` так и есть: 37 заголовков из 37 со своим `sz`, и требовать
        # там кегль мастера значит требовать того, чего в шаблоне нет.
        # Проверяем механизм там, где он работает, а не факт совпадения.
        with zipfile.ZipFile(path) as archive:
            silent = [
                name
                for name in sorted(archive.namelist())
                if re.match(r"ppt/slideLayouts/slideLayout\d+\.xml$", name)
                and any(
                    ('type="title"' in sp or 'type="ctrTitle"' in sp)
                    and not re.search(r'sz="\d+"', sp)
                    for sp in re.findall(
                        r"<p:sp>.*?</p:sp>", archive.read(name).decode("utf-8"), re.S
                    )
                )
            ]
        if not silent:
            continue

        inherited = [
            slot.style.size_pt
            for layout in spec.layouts
            for slot in layout.slots
            if slot.role is SlotRole.TITLE and slot.style is not None
        ]
        assert declared in inherited, (
            f"{path.name}: {len(silent)} layout'ов не объявляют кегль заголовка, "
            f"и он обязан достаться им из мастера ({declared} pt); "
            f"получено: {sorted(set(inherited))}"
        )
