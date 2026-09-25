"""Вёрстка: план + шаблон → `DeckIR`.

Слайд раскладывается на композицию шаблона, а не рисуется заново. Композиция
(`Pattern`) снята с настоящего слайда-примера и несёт `donor_slide_index` —
то, откуда рендерер потом клонирует поддерево фигур. Так оформление, ради
которого шаблон и берут, достаётся даром; нарисованное заново было бы «похоже
на шаблон», а это не то же самое.

Выбор композиции идёт **по структуре**: у слайда есть набор нужных мест
(«заголовок, список, график»), у композиции — набор имеющихся. Класс
композиции поднимает её в выдаче, но неопознанный класс не исключает её
вовсе: на чужом шаблоне правила не опознают заметную долю композиций, и
выбрасывать их значит добровольно обеднить вёрстку.

Если ни одна композиция не подошла, работает запасной путь по layout'ам —
плейсхолдеры есть даже у шаблона без слайдов-примеров.
"""

from __future__ import annotations

from deckwright.layout.fitter import FitResult, fit_paragraphs, fit_size, split_blocks
from deckwright.layout.strategy import Strategy, ladder_for_role, role_typical
from deckwright.layout.text_metrics import FontMetrics, metrics_for_spec
from deckwright.schemas import (
    BlockKind,
    Box,
    ChartContent,
    ChartKind,
    CheckKind,
    Color,
    DeckIR,
    Element,
    ElementKind,
    FixKind,
    Issue,
    IssueCategory,
    LayoutSpec,
    Paragraph,
    Pattern,
    ProposedFix,
    Provenance,
    Severity,
    SlideIntent,
    SlideIR,
    SlidePlan,
    SlotRole,
    SourceKind,
    TableContent,
    TemplateSpec,
    TextContent,
    TextStyle,
    readable_text_color,
)
from deckwright.visuals.charts import series_from_pack

# Намерения, которым хватает одного заголовка.
_BARE_INTENTS = frozenset({SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING})

# Роли, куда можно положить содержание. Подпись и подпись к числу входят
# сюда наравне с телом текста: в карточке шаблона содержание живёт именно в
# них, и без них композиция из трёх карточек выглядит для вёрстки пустой.
_CONTENT_ROLES = frozenset(
    {
        SlotRole.BODY,
        SlotRole.BULLETS,
        SlotRole.CAPTION,
        SlotRole.CHART,
        SlotRole.TABLE,
        SlotRole.IMAGE,
        SlotRole.KPI_VALUE,
        SlotRole.KPI_LABEL,
        SlotRole.QUOTE,
    }
)

# Роли, которые годятся под текст, если запрошенной не нашлось. Порядок —
# порядок предпочтения: список охотнее ложится в список, чем в абзац.
_TEXT_FALLBACK: dict[SlotRole, tuple[SlotRole, ...]] = {
    SlotRole.BULLETS: (SlotRole.BODY, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.BODY: (SlotRole.BULLETS, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.CAPTION: (SlotRole.BODY, SlotRole.BULLETS, SlotRole.KPI_LABEL),
    SlotRole.QUOTE: (SlotRole.BODY, SlotRole.BULLETS, SlotRole.CAPTION),
    SlotRole.KPI_VALUE: (SlotRole.BODY, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.CHART: (SlotRole.IMAGE, SlotRole.TABLE, SlotRole.BODY),
    SlotRole.TABLE: (SlotRole.CHART, SlotRole.BODY, SlotRole.BULLETS),
    SlotRole.IMAGE: (SlotRole.CHART,),
}


# Блоки, которые раскладываются по элементам повторителя, по пункту в элемент.
_SPREAD_KINDS = frozenset({BlockKind.BULLETS, BlockKind.STEPS})


class LayoutError(RuntimeError):
    """Шаблон не даёт ни одного места, куда положить содержание."""


# ── Подбор места ─────────────────────────────────────────────────────────────


def needed_profile(plan_slide: SlidePlan, strategy: Strategy) -> dict[SlotRole, int]:
    """Какие места нужны этому слайду. Это и есть его блочная сигнатура.

    Сигнатура строится из плана, а не из текста: во что превратится числовой
    ряд — график, таблицу или крупное число — решает вариант, и у трёх
    вариантов одного слайда сигнатуры законно разные.
    """
    profile: dict[SlotRole, int] = {SlotRole.TITLE: 1}
    for block in plan_slide.blocks:
        role = strategy.role_for(block)
        profile[role] = profile.get(role, 0) + 1
    return profile


def _title_fits(
    pattern: Pattern,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladder: list[float],
) -> bool:
    """Влезает ли заголовок слайда в заголовочную рамку этой композиции.

    Меряем до выбора, а не после. Композиция с узкой заголовочной рамкой
    посреди слайда годится для «Итоги», но не для формулировки в семь слов, и
    узнать это дешевле сейчас: потом останется только завести находку.
    """
    if metrics is None:
        return True
    slot = _title_slot(pattern)
    if slot is None:
        return False
    declared = slot.style.size_pt if slot.style is not None else (ladder[-1] if ladder else 18.0)
    start = strategy.start_size(ladder, declared)
    return fit_size(plan_slide.takeaway_title, metrics, slot.box, ladder, start).fits


# Роли, для которых «влезает» мерится площадью, а не текстом, и какую долю
# слайда рамка обязана занимать по каждой стороне. График в рамке подписи
# формально «влезает» — строка идентификатора ряда короткая, — а на слайде
# его не видно: так было на `vk_tech`, график 4.7 × 0.7 дюйма. Четверть высоты
# и треть ширины — нижняя граница, при которой у столбцов остаются оси и
# подписи.
_DATA_ROLES = frozenset({SlotRole.CHART, SlotRole.TABLE, SlotRole.IMAGE})
_DATA_MIN_HEIGHT_SHARE = 0.25
_DATA_MIN_WIDTH_SHARE = 1 / 3


def _roomy_for_data(box: Box, spec: TemplateSpec) -> bool:
    return (
        box.h >= spec.slide_height_emu * _DATA_MIN_HEIGHT_SHARE
        and box.w >= spec.slide_width_emu * _DATA_MIN_WIDTH_SHARE
    )


def _seats_all(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None = None,
    ladders: dict[SlotRole, list[float]] | None = None,
) -> bool:
    """Получит ли каждый блок свой слот — с учётом доли мест варианта.

    Слабее `_blocks_fit`: влезет ли текст целиком, не проверяет. Нужен
    запасному пути, когда не влезает нигде: блок без слота уходит в запасную
    полосу, а та на `vk_education` ложилась поверх соседнего блока. Но слот,
    куда не помещается ни одной строки, — не место: так показатель садился в
    рамку высотой меньше строки и пропадал со слайда.
    """
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    title = _title_slot(pattern)
    taken = [title.box] if title is not None else []
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        role = strategy.role_for(block)
        seats = _seat(pattern, block, role, free, taken)
        if seats is None:
            return False
        for slot, lines in seats:
            ladder = (ladders or {}).get(slot.role) or []
            if metrics is not None and ladder and role not in _DATA_ROLES:
                smallest = fit_paragraphs(lines, metrics, slot.box, ladder, min(ladder))
                if not smallest.capacity_lines:
                    return False
            taken.append(slot.box)
    return True


def _spare_cards(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> int:
    """Сколько карточек донора останется без пункта после раскладки списков."""
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    title = _title_slot(pattern)
    taken = [title.box] if title is not None else []
    spare = 0
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        seats = _seat(pattern, block, strategy.role_for(block), free, taken)
        if seats is None:
            continue
        # Повторитель, по которому разложен список, и его элементы донора.
        for repeater in pattern.repeaters:
            if len(seats) > 1 and seats[0][0].id.startswith(f"{repeater.id}_"):
                spare += max(0, repeater.observed_count - len(seats))
        taken.extend(slot.box for slot, _ in seats)
    return spare


def _blocks_fit(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]] | None,
    typical: dict[SlotRole, float] | None = None,
) -> bool:
    """Влезает ли содержание слайда в рамки этой композиции.

    `typical` — строже: текст обязан влезть кеглем не мельче типичного для
    его роли в этом шаблоне (`role_typical`), а не только минимальным.

    Та же проверка, что `_title_fits`, но для блоков, и тем же порядком, что
    и сборка слайда: доля занимаемых мест по варианту, подмена ролей, рамки,
    уже занятые заголовком и соседями, шкала кеглей от стартового вниз.

    Без неё композиция выбиралась по сигнатуре и заголовку, а текст тела не
    мерился вовсе: на живом плане #12 повестка из пяти пунктов ложилась в
    рамку на две строки, хотя в том же шаблоне под ту же сигнатуру были
    рамки на пять. 37 переполненных слайдов из 90.
    """
    if metrics is None or not ladders:
        return True
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    title = _title_slot(pattern)
    taken = [title.box] if title is not None else []
    for block in plan_slide.blocks:
        lines = _block_lines(block)
        if not lines:
            continue
        role = strategy.role_for(block)
        seats = _seat(pattern, block, role, free, taken)
        if seats is None:
            # Блок без места уходит в запасную полосу — это уже не
            # композиция шаблона, а вынужденная мера.
            return False
        slot = seats[0][0]
        if role in _DATA_ROLES:
            if not _roomy_for_data(slot.box, spec):
                return False
            taken.append(slot.box)
            continue
        ladder = ladders[slot.role]
        declared = (
            slot.style.size_pt
            if slot.style is not None
            else (ladder[len(ladder) // 2] if ladder else 18.0)
        )
        start = strategy.start_size(ladder, declared)
        fits = _fit_seats([(seat.box, text) for seat, text in seats], metrics, ladder, start)
        if not all(fit.fits for fit in fits):
            return False
        if typical is not None and min(f.size_pt for f in fits) < typical.get(role, 0.0):
            return False
        taken.extend(seat.box for seat, _ in seats)
    return True


def pick_pattern(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    used: set[str] | None = None,
    metrics: FontMetrics | None = None,
    title_ladder: list[float] | None = None,
    ladders: dict[SlotRole, list[float]] | None = None,
    avoid: set[str] | None = None,
) -> Pattern | None:
    """Композиция под сигнатуру слайда, или None, если такой нет.

    Среди одинаково подходящих предпочитается та, которой колода ещё не
    пользовалась. Без этого все десять слайдов ложатся в одну и ту же
    композицию: `patterns_matching` возвращает устойчивый порядок, а слайды
    с одинаковой сигнатурой берут из него первый. Колода из десяти
    одинаковых слайдов формально верна и практически бесполезна — шаблон
    даёт десятки композиций именно затем, чтобы презентация не выглядела
    одним слайдом, повторённым десять раз.
    """
    used = used or set()
    avoid = avoid or set()
    title_ladder = title_ladder or (ladders or {}).get(SlotRole.TITLE, [])
    needed = needed_profile(plan_slide, strategy)

    typical = {role: role_typical(spec, role) for role in SlotRole}

    def fitting(candidates: list[Pattern]) -> list[Pattern]:
        """Те, куда влезают и заголовок, и содержание.

        Сначала — где содержание читается типичным кеглем шаблона; если таких
        нет, где оно влезает хотя бы минимальным.
        """
        titled = [
            pattern
            for pattern in candidates
            if _title_fits(pattern, plan_slide, strategy, metrics, title_ladder)
        ]
        for strict in (typical, None):
            found = [
                pattern
                for pattern in titled
                if _blocks_fit(pattern, spec, plan_slide, strategy, metrics, ladders, strict)
            ]
            if found:
                return found
        return []

    def best_of(candidates: list[Pattern]) -> Pattern:
        """Порядок решений: влезает текст → ещё не было в колоде → вариант.

        «Влезает» стоит первым сознательно. Разнообразие композиций ценно,
        но слайд, где текст не помещается, модель со зрением в #12 видела
        пустым — «только заголовок и пустые маркеры». Предпочтение варианта
        сохраняется порядком кандидатов внутри каждой ступени.
        """
        roomy = fitting(candidates) or [
            pattern
            for pattern in candidates
            if _title_fits(pattern, plan_slide, strategy, metrics, title_ladder)
        ] or candidates
        order = {cls: rank for rank, cls in enumerate(strategy.pattern_preference)}

        def key(pattern: Pattern) -> tuple[bool, bool, int, int]:
            # `avoid` — композиции, которые для этого слайда уже взяли другие
            # варианты. Варианты строятся независимо, и там, где влезающих
            # композиций мало, два из них брали одну и ту же: на `vk_tech`
            # dense и balanced совпали на всех десяти слайдах (A12).
            # Лишние карточки — после разнообразия: пустую карточку рендер
            # уберёт, но не ту, что нарисована в самом layout'е, — на
            # `vk_tech` три пункта ложились в пять пронумерованных карточек.
            return (
                pattern.id in avoid,
                pattern.id in used,
                _spare_cards(pattern, spec, plan_slide, strategy),
                order.get(pattern.pattern_class, len(order)),
            )

        return min(roomy, key=key)
    preferred = None
    for candidate in strategy.pattern_preference:
        matches = spec.patterns_matching(needed, preferred=candidate)
        if matches and matches[0].pattern_class is candidate:
            preferred = candidate
            break

    # Строгая сигнатура: паттерн обязан дать ровно то, что просят.
    matches = [
        pattern
        for pattern in spec.patterns_matching(needed, preferred=preferred)
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]
    if matches and fitting(matches):
        return best_of(matches)

    # Мягкий подбор: заголовок плюс сколько-нибудь мест. Здесь порядок решает
    # не класс, а **сколько блоков паттерн реально усадит**. Иначе выбирается
    # композиция с единственным местом под картинку, два текстовых блока не
    # находят себе рамки и ложатся друг на друга в запасной области.
    wanted = [role for role, count in needed.items() if role is not SlotRole.TITLE
              for _ in range(count)]
    relaxed = [
        pattern
        for pattern in spec.patterns_matching({SlotRole.TITLE: 1}, preferred=preferred)
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]
    # Строгой сигнатуры нет или текст в неё не влезает. Композиция с
    # текстовыми рамками другой роли, куда он влезает, лучше: подмена роли
    # («абзац» вместо «списка») видна только в коде, переполнение — глазами.
    roomy = fitting(relaxed)
    if roomy:
        return best_of(roomy)
    # Не влезает нигде. Тогда хотя бы так, чтобы у каждого блока был свой
    # слот: переполнение останется находкой, но блоки не лягут друг на друга.
    seated = [
        pattern
        for pattern in (matches or []) + relaxed
        if _seats_all(pattern, spec, plan_slide, strategy, metrics, ladders)
    ]
    if seated:
        return best_of(seated)
    if matches:
        return best_of(matches)
    if not relaxed:
        return None

    def seats(pattern: Pattern) -> int:
        free = _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
        return sum(1 for role in wanted if _assign(free, role) is not None)

    best = max(seats(pattern) for pattern in relaxed)
    return best_of([pattern for pattern in relaxed if seats(pattern) == best])


def _on_slide(box: Box, width: int, height: int) -> bool:
    """Помещается ли рамка на слайде целиком."""
    return box.x >= 0 and box.y >= 0 and box.right <= width and box.bottom <= height


def _usable_slots(pattern: Pattern, slide_w: int, slide_h: int) -> list:
    """Слоты композиции, куда можно положить содержание, в порядке чтения.

    Повторители раскрываются в свои места: сетка из четырёх карточек даёт
    четыре комплекта слотов, и содержание раскладывается по ним.

    Раскрытие обрывается краем слайда. `max_count` — оценка того, сколько
    элементов выдержит сетка, и она бывает щедрее слайда: на `vk_education`
    пятая карточка уезжала на полтора дюйма за правый край. Место, которого
    на слайде нет, — не место.

    Слоты, выходящие за край в самом шаблоне, тоже отбрасываются: содержание
    в них гарантированно даст находку «текст за границей слайда».
    """
    slots = [
        s
        for s in pattern.slots
        if s.role in _CONTENT_ROLES and _on_slide(s.box, slide_w, slide_h)
    ]
    for repeater in pattern.repeaters:
        for index in range(repeater.max_count):
            dx, dy = repeater.offset(index)
            expanded = []
            for slot in repeater.item_slots:
                if slot.role not in _CONTENT_ROLES:
                    continue
                box = Box(x=slot.box.x + dx, y=slot.box.y + dy, w=slot.box.w, h=slot.box.h)
                if not _on_slide(box, slide_w, slide_h):
                    expanded = []
                    break
                backdrop = (
                    repeater.member_backdrops[index]
                    if index < len(repeater.member_backdrops)
                    else None
                )
                expanded.append(
                    slot.model_copy(
                        update={
                            "id": f"{repeater.id}_{index}_{slot.id}",
                            "box": box,
                            "backdrop": backdrop,
                        }
                    )
                )
            if not expanded:
                # Этот элемент сетки уже не на слайде — следующие тем более.
                break
            slots.extend(expanded)
    return sorted(slots, key=lambda s: (s.box.y, s.box.x))


def _fill_share(pattern: Pattern | None, slots: list, strategy: Strategy) -> list:
    """Места, которые вариант согласен занять.

    Вариант решает, какую долю мест композиции занимать: плотный забивает
    все, воздушный оставляет воздух. Доля режет одиночные места композиции,
    но не карточки повторителя: их число задаёт список, а лишние уходят в
    рендере. Резать и их значило отдавать списку две карточки из четырёх —
    подписи всех карточек стоят в порядке чтения раньше их текста.
    """
    fixed_ids = {slot.id for slot in pattern.slots} if pattern is not None else None
    fixed = [slot for slot in slots if fixed_ids is None or slot.id in fixed_ids]
    allowed = max(1, round(len(fixed) * strategy.slot_fill_target)) if fixed else 0
    kept = {id(slot) for slot in fixed[:allowed]}
    return [
        slot
        for slot in slots
        if id(slot) in kept or (fixed_ids is not None and slot.id not in fixed_ids)
    ]


def _title_slot(container):
    for slot in container.slots:
        if slot.role is SlotRole.TITLE:
            return slot
    return None


def _bookend(spec: TemplateSpec, plan_slide: SlidePlan) -> Pattern | None:
    """Обложка шаблона для титула, его финал — для финала.

    Их берут целиком: первый и последний слайды колоды — это титул и финал
    самого шаблона, меняется только текст. Финала в шаблоне нет — финалом
    служит обложка.
    """
    by_id = {pattern.id: pattern for pattern in spec.patterns}
    if plan_slide.intent is SlideIntent.TITLE:
        wanted = spec.cover_pattern_id
    elif plan_slide.intent is SlideIntent.CLOSING:
        wanted = spec.closing_pattern_id or spec.cover_pattern_id
    else:
        return None
    return by_id.get(wanted) if wanted else None


def pick_layout(spec: TemplateSpec, plan_slide: SlidePlan) -> LayoutSpec:
    """Запасной путь: layout, чья структура не противоречит намерению."""
    if not spec.layouts:
        raise LayoutError(f"в шаблоне {spec.source_name!r} нет ни одного layout'а")

    with_title = [layout for layout in spec.layouts if _title_slot(layout) is not None]
    candidates = with_title or spec.layouts
    if plan_slide.intent in _BARE_INTENTS:
        return candidates[0]
    with_content = [
        layout
        for layout in candidates
        if any(slot.role in _CONTENT_ROLES for slot in layout.slots)
    ]
    return (with_content or candidates)[0]


# Какую долю меньшей из двух рамок разрешено перекрыть, прежде чем считать,
# что они налезли друг на друга. Ноль здесь не годится: рамки шаблона
# соприкасаются краями на пиксель сплошь и рядом.
_OVERLAP_TOLERANCE = 0.15

# Минимальная высота полосы под блок без слота. Меньше этого рамка не
# удерживает и одной строки, и фиттер честно заведёт находку о переполнении.
_MIN_BAND_SHARE_DIVISOR = 274_320  # 0.3 дюйма


def _overlaps(a: Box, b: Box) -> bool:
    """Существенно ли перекрываются две рамки."""
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    if width <= 0 or height <= 0:
        return False
    smaller = min(a.w * a.h, b.w * b.h)
    return smaller > 0 and (width * height) / smaller > _OVERLAP_TOLERANCE


def _assign(
    slots: list, role: SlotRole, taken: list[Box] | None = None, fallback: bool = True
) -> object | None:
    """Свободный слот нужной роли, иначе — ближайший подходящий.

    Точное совпадение роли предпочтительнее, но отказываться от вёрстки из-за
    того, что в композиции список называется абзацем, не за что: и то и
    другое — рамка под текст.

    Слот, налезающий на уже занятую рамку, не берётся вовсе. Композиция
    снимается со слайда-примера, и её слоты перекрываются там, где у дизайнера
    надпись лежала поверх карточки. Положить туда два разных текста значит
    выдать кашу: на `zelenie_investicii` так получалось до шести наложений на
    колоду.
    """
    taken = taken or []
    for wanted in (role, *(_TEXT_FALLBACK.get(role, ()) if fallback else ())):
        free = [slot for slot in slots if slot.role is wanted]
        clear = [
            slot for slot in free if not any(_overlaps(slot.box, box) for box in taken)
        ]
        if clear:
            slots.remove(clear[0])
            return clear[0]
    return None


def _chunks(items: list[str], count: int) -> list[list[str]]:
    """Пункты подряд на `count` частей, различающихся не больше чем на один."""
    size, extra = divmod(len(items), count)
    parts, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        parts.append(list(items[start:end]))
        start = end
    return parts


def _spread(
    pattern: Pattern | None, block, wanted: SlotRole, free: list, taken: list[Box]
) -> list[tuple[object, list[str]]] | None:
    """Список, разложенный по элементам повторителя: пункт в карточку.

    Донор с четырьмя карточками — это четыре места под четыре мысли. Весь
    список в первой карточке при трёх пустых рядом — главный дефект листов
    #12: 14 слайдов из 90 на `vk_tech`. Пунктов больше, чем карточек, —
    они делятся поровну подряд; меньше — лишние карточки уходят в рендере.

    Берутся только элементы, которые есть у донора (`observed_count`): свою
    подложку и пиктограмму получают лишь они, дорисованная карточка была бы
    текстом без карточки. Все места повторителя после этого заняты — иначе
    следующий блок сел бы в четвёртую карточку при пустых второй и третьей.
    """
    if pattern is None or block.kind not in _SPREAD_KINDS or block.heading:
        return None
    if len(block.items) < 2:
        return None
    by_id = {slot.id: slot for slot in free}
    for repeater in pattern.repeaters:
        for item_slot in sorted(repeater.item_slots, key=lambda s: -s.box.area):
            if item_slot.role is not wanted:
                continue
            members = []
            for index in range(repeater.observed_count):
                slot = by_id.get(f"{repeater.id}_{index}_{item_slot.id}")
                if slot is None or any(_overlaps(slot.box, box) for box in taken):
                    break
                members.append(slot)
            if len(members) < 2:
                continue
            count = min(len(block.items), len(members))
            prefix = f"{repeater.id}_"
            free[:] = [slot for slot in free if not slot.id.startswith(prefix)]
            return list(
                zip(members[:count], _chunks(list(block.items), count), strict=True)
            )
    return None


def _seat(
    pattern: Pattern | None, block, role: SlotRole, free: list, taken: list[Box]
) -> list[tuple[object, list[str]]] | None:
    """Места блока и строки для каждого; None — места не нашлось.

    Роли перебираются в порядке предпочтения, и на каждой сначала —
    повторитель, потом одиночный слот. Не наоборот: на holdout повторитель
    был только из подписей в кружках, и список, раскладываясь «по
    повторителю любой роли», уходил в кружки мимо трёх карточек под текст.

    Одна и та же раскладка нужна подбору композиции, расчёту ёмкости и
    сборке слайда: мерить одно, а собирать другое значит снова получить
    переполнение, которого подбор не предвидел.
    """
    for wanted in (role, *_TEXT_FALLBACK.get(role, ())):
        spread = _spread(pattern, block, wanted, free, taken)
        if spread:
            return spread
        slot = _assign(free, wanted, taken, fallback=False)
        if slot is not None:
            return [(slot, _block_lines(block))]
    return None


def _fit_seats(
    seats: list[tuple[Box, list[str]]],
    metrics: FontMetrics,
    ladder: list[float],
    start: float,
) -> list[FitResult]:
    """Один кегль на все места блока: карточки одного ряда пишутся одинаково."""
    first = [fit_paragraphs(lines, metrics, box, ladder, start) for box, lines in seats]
    common = min(fit.size_pt for fit in first)
    if all(fit.size_pt == common for fit in first):
        return first
    return [fit_paragraphs(lines, metrics, box, ladder, common) for box, lines in seats]


# ── Цвет и свободная область ─────────────────────────────────────────────────


# Порог контраста из Приложения 1 ТЗ. Тем же числом меряет и аудит, поэтому
# вёрстка не имеет права выдавать то, что он справедливо забракует.
MIN_CONTRAST = 4.5

# Высота строки таблицы в кеглях и сколько символов кегля нужно колонке, чтобы
# в неё влезло хоть что-то. Оценка грубая и намеренно такая: точную ширину
# колонок считает PowerPoint, а здесь решается только «влезет или переехать».
TABLE_ROW_HEIGHT = 1.8
TABLE_MIN_CHARS = 6
EMU_PER_POINT = 12_700


def _under(slot, background: Color | None) -> Color | None:
    """Фон, на котором окажется текст слота: его подложка, иначе фон слайда."""
    if slot is not None and getattr(slot, "backdrop", None) is not None:
        return slot.backdrop
    return background


def _text_color(
    container,
    role: SlotRole,
    is_dark: bool,
    background: Color | None,
    spec: TemplateSpec | None = None,
) -> Color:
    """Цвет текста для роли — взятый из самого шаблона и проверенный на фоне.

    Порядок предпочтений: цвет, которым шаблон пишет текст этой роли здесь;
    затем цвет любого его текстового слота; и только если шаблон не сказал
    ничего — выбор по яркости фона.

    Так правильнее, чем всегда считать по фону: шаблон уже решил, каким
    цветом здесь писать, и его решение учитывает градиенты, фоновые картинки
    и декор, о которых мы не знаем ничего.

    Но взятый цвет обязан пройти проверку контрастом. Композиция снимается со
    слайда-примера, а фон слайду назначает его layout — и это законно разные
    слайды: на `vk_workspace` композиция со светлого примера приезжала на
    чёрный фон, и текст получался чёрным по чёрному. Не влезающий в порог
    цвет заменяется на тот, что читается: обратное — отдать колоду, которую
    аудит забракует, а человек не прочитает.
    """
    chosen: Color | None = None
    for slot in container.slots:
        if slot.role is role and slot.style is not None:
            chosen = slot.style.color
            break
    if chosen is None:
        for slot in container.slots:
            if slot.style is not None:
                chosen = slot.style.color
                break

    # Фон бывает неизвестен: композиция снята с донора, а фон слайду назначает
    # его layout. Судим тогда по яркости шаблона — ею фон и окажется.
    judged = background or (Color(rgb="000000") if is_dark else Color(rgb="FFFFFF"))
    if chosen is not None and chosen.contrast_ratio(judged) >= MIN_CONTRAST:
        return chosen

    # Донорский цвет на нашем фоне не читается (или его нет). Прежде чем
    # придумывать свой, спрашиваем палитру шаблона: колода обязана быть
    # набрана его цветами, а не нашими. `#111111` остаётся крайним случаем —
    # шаблоном, в палитре которого нет ни одного читаемого цвета текста.
    if spec is not None:
        from_palette = readable_text_color(spec.palette, background, is_dark)
        if from_palette is not None:
            return from_palette
    # Ни один цвет шаблона не читается. Из белого и почти чёрного — тот, что
    # контрастнее на этом фоне: на средне-яркой карточке признак «шаблон
    # тёмный» выбирал не тот.
    return max(
        (Color(rgb="FFFFFF"), Color(rgb="111111")),
        key=lambda color: color.contrast_ratio(judged),
    )


def _content_area(spec: TemplateSpec, container) -> Box:
    """Свободная область: поля шаблона минус то, что занимает заголовок."""
    grid = spec.grid
    left = grid.margin_left_emu if grid else spec.slide_width_emu // 20
    right = grid.margin_right_emu if grid else spec.slide_width_emu // 20
    top = grid.margin_top_emu if grid else spec.slide_height_emu // 12
    bottom = grid.margin_bottom_emu if grid else spec.slide_height_emu // 12

    width = spec.slide_width_emu - left - right
    if width <= 0:
        raise LayoutError(
            f"поля шаблона {spec.source_name!r} не оставляют места под содержание"
        )

    # Обычно содержание идёт под заголовком. Но заголовок композиции бывает и
    # внизу слайда — тогда места под ним нет, и упираться в это нельзя:
    # берём всю область внутри полей, а разложить по ней — забота слотов.
    title = _title_slot(container)
    under_title = top
    if title is not None:
        under_title = max(top, title.box.bottom + spec.slide_height_emu // 40)
    if spec.slide_height_emu - under_title - bottom > spec.slide_height_emu // 10:
        top = under_title

    height = spec.slide_height_emu - top - bottom
    if height <= 0:
        raise LayoutError(
            f"поля шаблона {spec.source_name!r} не оставляют места под содержание"
        )
    return Box(x=left, y=top, w=width, h=height)


def _free_band(
    spec: TemplateSpec, container, index: int, bands: int, taken: list[Box]
) -> Box:
    """Полоса свободной области для блока, которому не досталось слота.

    Блоки без слота нельзя класть в одну и ту же рамку: на `vk_workspace`
    два таких блока легли друг на друга, и слайд читался как каша из двух
    текстов. Свободная область делится на равные полосы по числу блоков
    слайда — верхняя граница, зато без наложений между самими полосами.

    Отсчёт идёт ниже всего, что слайд уже занял. Иначе полоса ложится на
    слот, взятый предыдущим блоком: на `zelenie_investicii` она садилась
    ровно на рамку соседа, потому что свободная область считалась от
    заголовка и про занятые слоты не знала.
    """
    area = _content_area(spec, container)
    floor = max(
        (box.bottom for box in taken if box.bottom <= area.bottom), default=area.y
    )
    top = min(max(area.y, floor), area.bottom - _MIN_BAND_SHARE_DIVISOR)
    remaining = max(_MIN_BAND_SHARE_DIVISOR, area.bottom - top)
    share = max(1, bands - index)
    return Box(x=area.x, y=top, w=area.w, h=max(1, remaining // share))


def _data_element(
    element_id: str,
    block,
    role: SlotRole,
    box: Box,
    style: TextStyle,
    spec: TemplateSpec,
    pack,
    background: Color | None,
    is_dark: bool,
    roomy: Box,
) -> Element | None:
    """Блок плана как нативный график или таблица, если это возможно.

    Возвращает None, когда данных не хватает: ряда нет в пакете, категории
    рядов не сошлись, таблица пуста. Тогда блок верстается текстом — это хуже
    графика, но честнее пустой рамки.

    Растр здесь невозможен по построению: и `c:chart`, и `a:tbl` — нативные
    объекты, которые человек может открыть и поправить.
    """
    palette = _visible_palette(spec, background, is_dark)

    if role is SlotRole.CHART and block.series_ids and pack is not None:
        categories, series = series_from_pack(block.series_ids, pack, palette)
        if categories and series:
            return Element(
                id=element_id,
                kind=ElementKind.CHART,
                role=SlotRole.CHART,
                box=box,
                provenance=Provenance(kind=SourceKind.DERIVED, ref=block.id),
                chart=ChartContent(
                    chart_kind=ChartKind.COLUMN if len(categories) > 2 else ChartKind.BAR,
                    categories=categories,
                    series=series,
                    has_legend=len(series) > 1,
                    label_style=style,
                ),
            )

    if role is SlotRole.TABLE:
        table = _table_content(block, pack, style)
        if table is not None:
            return Element(
                id=element_id,
                kind=ElementKind.TABLE,
                role=SlotRole.TABLE,
                box=_table_box(table, style, box, roomy),
                provenance=Provenance(kind=SourceKind.DERIVED, ref=block.id),
                table=table,
            )
    return None


def _table_box(table: TableContent, style: TextStyle, slot: Box, roomy: Box) -> Box:
    """Рамка, в которую таблица действительно помещается.

    Слот композиции бывает подписью в одну строку, а таблице нужно столько
    строк, сколько в ней данных. Втиснутая в подпись таблица не ужимается —
    она растёт вниз и уезжает за слайд, а колонки сжимаются до одной буквы в
    строке. Поэтому высота считается заранее, и если слот её не держит,
    таблица переезжает в свободную область.

    Ширина смотрится так же: колонке нужно место хотя бы под пару символов
    кегля, которым её набирают.
    """
    rows = len(table.rows) + 1
    needed_h = round(rows * style.size_pt * TABLE_ROW_HEIGHT * EMU_PER_POINT)
    needed_w = round(len(table.header) * style.size_pt * TABLE_MIN_CHARS * EMU_PER_POINT)
    if slot.h >= needed_h and slot.w >= needed_w:
        return slot
    return Box(
        x=roomy.x,
        y=roomy.y,
        w=max(roomy.w, needed_w) if roomy.w >= needed_w else roomy.w,
        h=min(roomy.h, max(needed_h, roomy.h)) if needed_h <= roomy.h else roomy.h,
    )


def _visible_palette(
    spec: TemplateSpec, background: Color | None, is_dark: bool
) -> list[Color]:
    """Цвета шаблона, которые видно на его же фоне.

    Первый цвет палитры — самый употребительный по площади, а на тёмном
    шаблоне это фон. Покрасить им столбики значит нарисовать чёрное по
    чёрному: график есть, данные верные, видно ничего. Та же ошибка, что и с
    текстом, и тот же порог из Приложения 1.
    """
    colors = [token.color for token in spec.palette]
    if background is not None:
        colors = [
            color
            for color in colors
            if color.contrast_ratio(background) >= MIN_CONTRAST
        ]
    if colors:
        return colors
    return [Color(rgb="FFFFFF") if is_dark else Color(rgb="111111")]


def _table_content(block, pack, style: TextStyle) -> TableContent | None:
    """Таблица блока: своя, если планировщик её составил, иначе из рядов."""
    if block.table is not None and block.table.columns:
        return TableContent(
            header=list(block.table.columns),
            rows=[list(row) for row in block.table.rows],
            header_style=style,
            cell_style=style,
        )

    known = {item.id: item for item in getattr(pack, "series", [])} if pack else {}
    chosen = [known[sid] for sid in block.series_ids if sid in known]
    if not chosen:
        return None
    categories = chosen[0].categories
    chosen = [item for item in chosen if item.categories == categories]
    if not chosen:
        return None
    return TableContent(
        header=["", *[item.name for item in chosen]],
        rows=[
            [label, *[f"{item.values[row]:g}" for item in chosen]]
            for row, label in enumerate(categories)
        ],
        header_style=style,
        cell_style=style,
    )


def _block_lines(block) -> list[str]:
    """Блок плана, приведённый к строкам для текстовой рамки."""
    lines: list[str] = []
    if block.heading:
        lines.append(block.heading)
    lines.extend(block.items)
    if block.table is not None and not lines:
        lines.append(" | ".join(block.table.columns))
        lines.extend(" | ".join(row) for row in block.table.rows)
    if block.series_ids and not lines:
        lines.append(", ".join(block.series_ids))
    return lines


# ── Сборка слайда ────────────────────────────────────────────────────────────


def _overflow_issue(
    slide_index: int, element_id: str, box: Box, result: FitResult, what: str
) -> Issue:
    """Находка о тексте, не влезшем даже на минимальной ступени шкалы.

    Заводится честно и сразу: молча обрезанный текст хуже помеченного, а
    дальше уменьшать кегль нельзя — промежуточное значение поймает проверка
    «кегль не из шкалы шаблона».
    """
    return Issue(
        check_id="layout.text_overflow",
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=Severity.WARNING,
        slide_index=slide_index,
        element_ids=[element_id],
        bbox=box,
        message=(
            f"{what} не помещается в рамку на минимальном кегле шкалы "
            f"({result.size_pt:g} pt): помещается {result.capacity_lines} "
            f"строк, занято {result.lines}"
        ),
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="сократить текст или разнести блоки на два слайда",
            action="shorten_or_split",
            # Ёмкость рамки едет с находкой: тот, кто будет сокращать, должен
            # знать не «покороче», а «в две строки».
            params={
                "slide_index": slide_index,
                "element_id": element_id,
                "capacity_lines": result.capacity_lines,
                "used_lines": result.lines,
            },
        ),
    )


def build_slide_ir(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    font_family: str,
    used: set[str] | None = None,
    pack=None,
    avoid: set[str] | None = None,
) -> tuple[SlideIR, list[Issue]]:
    """Один слайд: композиция шаблона, заполненная содержанием плана."""
    pattern = _bookend(spec, plan_slide) or pick_pattern(
        spec, plan_slide, strategy, used, metrics, ladders[SlotRole.TITLE], ladders, avoid
    )
    layout = pick_layout(spec, plan_slide)
    container = pattern if pattern is not None else layout

    if pattern is not None:
        is_dark = pattern.is_dark
        background = next(
            (lay.background for lay in spec.layouts if lay.id == pattern.layout_id),
            layout.background,
        )
        provenance = Provenance(kind=SourceKind.SLIDE, ref=pattern.id)
    else:
        is_dark = layout.is_dark
        background = layout.background
        provenance = Provenance(kind=SourceKind.LAYOUT, ref=layout.id)

    issues: list[Issue] = []
    elements: list[Element] = []

    # ── Заголовок ────────────────────────────────────────────────────────
    title_slot = _title_slot(container)
    title_box = title_slot.box if title_slot else _content_area(spec, container)
    title_ladder = ladders[SlotRole.TITLE]
    title_declared = (
        title_slot.style.size_pt
        if title_slot is not None and title_slot.style is not None
        else (title_ladder[-1] if title_ladder else 18.0)
    )
    title_start = strategy.start_size(title_ladder, title_declared)
    title_id = f"s{plan_slide.index}_title"

    title_overflowed = False
    title_steps = 0
    title_capacity = 0
    title_used = 0
    if metrics is not None:
        fit = fit_size(
            plan_slide.takeaway_title, metrics, title_box, title_ladder, title_start
        )
        title_size = fit.size_pt
        title_steps = fit.steps_down
        title_overflowed = not fit.fits
        title_capacity, title_used = fit.capacity_lines, fit.lines
        if title_overflowed:
            issues.append(
                _overflow_issue(plan_slide.index, title_id, title_box, fit, "заголовок")
            )
    else:
        title_size = title_start

    elements.append(
        Element(
            id=title_id,
            kind=ElementKind.TEXT,
            role=SlotRole.TITLE,
            box=title_box,
            provenance=provenance,
            backdrop=_under(title_slot, None),
            text=TextContent(
                scale_steps_down=title_steps,
                truncated=title_overflowed,
                capacity_lines=title_capacity,
                used_lines=title_used,
                paragraphs=[
                    Paragraph(
                        text=plan_slide.takeaway_title,
                        style=TextStyle(
                            font_family=font_family,
                            size_pt=title_size,
                            bold=True,
                            color=_text_color(
                                container,
                                SlotRole.TITLE,
                                is_dark,
                                _under(title_slot, background),
                                spec,
                            ),
                        ),
                    )
                ]
            ),
        )
    )

    # ── Содержание ───────────────────────────────────────────────────────
    bookend = pattern is not None and pattern.id in spec.bookend_ids
    free_slots = (
        # У обложки и финала порядок мест задал разбор: заголовок, под ним
        # подзаголовок и контакты. Порядок чтения отдал бы текст квадрату
        # под QR-код над заголовком.
        [slot for slot in pattern.slots if slot.role in _CONTENT_ROLES]
        if bookend
        else _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
        if pattern is not None
        else [
            slot
            for slot in layout.slots
            if slot.role in _CONTENT_ROLES
            and _on_slide(slot.box, spec.slide_width_emu, spec.slide_height_emu)
        ]
    )
    # На обложке и финале доля мест не режется: мест там два-три, и все они
    # — места шаблона под название, подзаголовок и контакты.
    if not bookend:
        free_slots = _fill_share(pattern, free_slots, strategy)

    # Сколько блоков уже не нашли себе слота: каждому следующему достаётся
    # своя полоса свободной области, иначе они лягут друг на друга.
    homeless = 0
    bands = max(1, len(plan_slide.blocks))
    # Рамки, которые слайд уже занял. Заголовок занимает свою первым.
    taken = [title_box]

    for position, block in enumerate(plan_slide.blocks):
        lines = _block_lines(block)
        if not lines:
            continue
        role = strategy.role_for(block)
        seats = _seat(pattern, block, role, free_slots, taken)
        slot = seats[0][0] if seats else None
        if slot is None:
            seats = [(None, lines)]
            box = _free_band(spec, container, homeless, bands, taken)
            homeless += 1
        else:
            box = slot.box
        block_ladder = ladders[slot.role if slot is not None else role]
        declared = (
            slot.style.size_pt
            if slot is not None and slot.style is not None
            else (block_ladder[len(block_ladder) // 2] if block_ladder else 18.0)
        )
        start = strategy.start_size(block_ladder, declared)
        element_id = f"s{plan_slide.index}_b{position}"
        placed = [
            (seat.box if seat is not None else box, text) for seat, text in seats
        ]
        # Цвет — по подложке каждого места: карточки одного ряда бывают
        # разного цвета.
        colors = [
            _text_color(container, role, is_dark, _under(seat, background), spec)
            for seat, _ in seats
        ]
        fits = (
            _fit_seats(placed, metrics, block_ladder, start) if metrics is not None else []
        )
        color = colors[0]
        taken.extend(seat_box for seat_box, _ in placed)

        # Числовой ряд и таблица становятся нативными объектами, а не
        # пересказом строками: ТЗ засчитывает только `c:chart` и `a:tbl`, и
        # человек должен мочь открыть данные и поправить их.
        native = _data_element(
            element_id,
            block,
            role,
            box,
            TextStyle(
                font_family=font_family,
                size_pt=fits[0].size_pt if fits else start,
                color=color,
            ),
            spec,
            pack,
            background,
            is_dark,
            _content_area(spec, container),
        )
        if native is not None:
            elements.append(native)
            continue

        for number, (seat_box, text) in enumerate(placed):
            # Блок, разложенный по карточкам, даёт элемент на карточку.
            seat_id = element_id if len(placed) == 1 else f"{element_id}_{number}"
            fit = fits[number] if fits else None
            if fit is not None and not fit.fits:
                issues.append(
                    _overflow_issue(
                        plan_slide.index, seat_id, seat_box, fit, f"блок {block.id!r}"
                    )
                )
            style = TextStyle(
                font_family=font_family,
                size_pt=fit.size_pt if fit is not None else start,
                color=colors[number],
            )
            elements.append(
                Element(
                    id=seat_id,
                    kind=ElementKind.TEXT,
                    role=slot.role if slot is not None else role,
                    box=seat_box,
                    provenance=provenance,
                    backdrop=_under(seats[number][0], None),
                    text=TextContent(
                        paragraphs=[
                            Paragraph(text=line, style=style, bullet=len(text) > 1)
                            for line in text
                        ],
                        # Переполнение записывается в само представление, а не
                        # только в находки вёрстки: аудит читает IR и обязан
                        # видеть то же, что видел фиттер.
                        scale_steps_down=fit.steps_down if fit is not None else 0,
                        truncated=fit is not None and not fit.fits,
                        # Ёмкость рамки и занятое ею: «сократите текст» без этих
                        # чисел — совет, который живая модель уже не выполнила.
                        capacity_lines=fit.capacity_lines if fit is not None else 0,
                        used_lines=fit.lines if fit is not None else 0,
                    ),
                )
            )

    slide = SlideIR(
        index=plan_slide.index,
        layout_id=pattern.layout_id if pattern is not None else layout.id,
        pattern_id=pattern.id if pattern is not None else None,
        donor_slide_index=pattern.donor_slide_index if pattern is not None else None,
        elements=elements,
        background=background,
        is_dark=is_dark,
        speaker_notes=plan_slide.speaker_notes,
    )
    return slide, issues


def build_deck_ir(
    spec: TemplateSpec,
    plan,
    variant,
    strategy: Strategy | None = None,
    pack=None,
    siblings: dict[str, dict[int, str]] | None = None,
) -> tuple[DeckIR, list[Issue]]:
    """Колода одного варианта и находки, которые вёрстка завела о себе сама.

    `variant` принимается и строкой, и пресетом из конфига: строка означает
    стратегию по умолчанию, и тогда вёрстка ведёт себя как «сбалансированная».

    `siblings` — реестр выбранных композиций, общий для вариантов одного
    прогона: {вариант: {номер слайда плана: композиция}}. Вариант избегает
    чужих композиций на том же слайде, если есть другая, куда текст влезает,
    и дописывает в реестр свои. Своих не избегает: пересборка в цикле
    исправления обязана выбрать то же, что и в первый раз.
    """
    if strategy is None:
        strategy = (
            Strategy.from_config(variant)
            if hasattr(variant, "strategy")
            else Strategy(
                name=str(variant),
                slot_fill_target=0.7,
                type_scale_bias="mid",
                data_viz_mode="auto",
                blocks_per_slide=2,
                pattern_preference=(),
            )
        )
    variant_name = getattr(variant, "name", str(variant))

    font_family = spec.fonts[0].family if spec.fonts else "Arial"
    metrics = metrics_for_spec(spec).metrics
    # Лестница у каждой роли своя: предел «мельче нельзя» шаблон задаёт для
    # заголовка и для тела текста по-разному.
    ladders = {role: ladder_for_role(spec, role) for role in SlotRole}

    slides: list[SlideIR] = []
    issues: list[Issue] = []
    used: set[str] = set()
    others = [
        chosen for name, chosen in (siblings or {}).items() if name != variant_name
    ]
    mine: dict[int, str] = {}
    for plan_slide in plan.slides:
        avoid = {chosen[plan_slide.index] for chosen in others if plan_slide.index in chosen}
        built, found = _slides_for(
            spec, plan_slide, strategy, metrics, ladders, font_family, used, pack, avoid
        )
        if built and built[0].pattern_id:
            mine[plan_slide.index] = built[0].pattern_id
        slides.extend(built)
        issues.extend(found)
        used.update(s.pattern_id for s in built if s.pattern_id)

    if siblings is not None:
        siblings[variant_name] = mine

    # Индексы обязаны идти подряд: разбиение слайда сдвигает всё, что ниже.
    for position, slide in enumerate(slides, start=1):
        slide.index = position

    return (
        DeckIR(
            variant=variant_name,
            template_sha256=spec.template_sha256,
            slide_width_emu=spec.slide_width_emu,
            slide_height_emu=spec.slide_height_emu,
            slides=slides,
        ),
        issues,
    )


def _slides_for(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    font_family: str,
    used: set[str],
    pack=None,
    avoid: set[str] | None = None,
) -> tuple[list[SlideIR], list[Issue]]:
    """Слайд, а если он переполнен и деление помогает — два.

    Деление — третий шаг закона фиттера, и применяется **после** того, как
    шкала кончилась: пока кегль ещё можно опустить на ступень, слайд делить
    не за что.

    Второе средство того же шага — сокращение текста — требует ещё одного
    обращения к модели, а сокращает текст планировщик, который отработал
    раньше вёрстки. Пока сокращателя нет, `SHORTEN` пользуется тем средством,
    которое есть. Молча не делать ничего нельзя: без деления блоки, которым
    не хватило места, ложатся друг на друга — на `theme_only` так выходило
    шесть наложений на колоду.
    """
    slide, issues = build_slide_ir(
        spec, plan_slide, strategy, metrics, ladders, font_family, used, pack, avoid
    )

    # Переполнение заголовка делением не лечится: у обеих половин заголовок
    # тот же. Делить имеет смысл только из-за содержания.
    title_id = f"s{plan_slide.index}_title"
    content_overflow = [
        issue
        for issue in issues
        if issue.check_id == "layout.text_overflow" and title_id not in issue.element_ids
    ]
    # Обложку и финал не делят: вторая обложка — не выход.
    if not content_overflow or slide.pattern_id in spec.bookend_ids:
        return [slide], issues

    parts = split_blocks(list(plan_slide.blocks))
    if len(parts) < 2:
        # Делить нечего: блок один. Находка остаётся — это четвёртый шаг
        # закона, и он честнее молчаливой обрезки.
        return [slide], issues

    halves: list[SlideIR] = []
    remaining: list[Issue] = []
    for part_no, chunk in enumerate(parts):
        piece = plan_slide.model_copy(
            update={"index": plan_slide.index + part_no, "blocks": chunk}
        )
        built, found = build_slide_ir(
            spec,
            piece,
            strategy,
            metrics,
            ladders,
            font_family,
            used | {slide.pattern_id or ""},
            pack,
            avoid,
        )
        # Идентификаторы элементов обязаны остаться уникальными в колоде.
        for element in built.all_elements():
            element.id = f"{element.id}_p{part_no}"
        for issue in found:
            issue.element_ids = [f"{i}_p{part_no}" for i in issue.element_ids]
        halves.append(built)
        remaining.extend(found)

    # Деление обязано себя окупить. Лишний слайд, который не убрал ни одного
    # переполнения, — чистый проигрыш: колода длиннее, а текст всё так же не
    # влезает. На синтетическом шаблоне безусловное деление растило колоду с
    # десяти слайдов до пятнадцати и число находок с шести до одиннадцати.
    if len(remaining) >= len(issues):
        return [slide], issues
    return halves, remaining
