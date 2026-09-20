"""Ограничитель по токенам в минуту.

Узкое место провайдера — TPM, а не число одновременных вызовов: восемь
параллельных запросов с картинкой слайда выбирают половину минутного лимита
одним всплеском. Здесь проверяется, что окно это удерживает.

Время и сон подменяются, поэтому тесты не ждут по-настоящему.
"""

from __future__ import annotations

from deckwright.llm.ratelimit import (
    RateLimiter,
    estimate_image_tokens,
    estimate_text_tokens,
)


class FakeClock:
    """Управляемое время: сон двигает часы, а не ждёт."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(clock: FakeClock, **kwargs) -> RateLimiter:
    return RateLimiter(time_source=clock.time, sleep=clock.sleep, **kwargs)


def test_no_limits_never_waits():
    clock = FakeClock()
    limiter = _limiter(clock)
    assert limiter.enabled is False
    for _ in range(100):
        limiter.acquire(10_000)
    assert clock.slept == []


def test_token_budget_holds_a_burst():
    """Всплеск параллельных вызовов не должен пробивать минутный лимит."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=40_000)

    for _ in range(16):  # 16 x 2500 = 40 000, ровно лимит
        limiter.acquire(2_500)
    assert clock.slept == [], "в лимит укладываемся, ждать незачем"

    limiter.acquire(2_500)  # семнадцатый не влезает
    assert limiter.waits == 1
    assert clock.slept == [60.0], "ждём, пока освободится самая старая запись"


def test_window_slides_and_frees_room():
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=10_000)
    limiter.acquire(10_000)

    clock.now += 61  # минута прошла, окно опустело
    limiter.acquire(10_000)
    assert clock.slept == []


def test_request_limit_is_enforced_too():
    clock = FakeClock()
    limiter = _limiter(clock, requests_per_minute=3)
    for _ in range(3):
        limiter.acquire(1)
    limiter.acquire(1)
    assert limiter.waits == 1


def test_single_call_larger_than_the_whole_limit_is_not_deadlocked():
    """Запрос крупнее лимита ждать бессмысленно: ждём отказа 429 и повтора,
    а не вечной блокировки."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=1_000)
    limiter.acquire(50_000)
    assert clock.slept == []


def test_actual_usage_replaces_the_estimate():
    """Оценка до запроса заменяется фактом, иначе окно разойдётся с реальностью."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=10_000)
    entry = limiter.acquire(5_000)
    limiter.settle(entry, 1_000)
    assert limiter.snapshot()["tokens_in_window"] == 1_000


def test_image_estimate_scales_with_area():
    """Вчетверо больше пикселей — вчетверо больше токенов (с точностью до
    округления). Отсюда и берётся экономия на разрешении картинок аудита."""
    small = estimate_image_tokens(960, 540)
    large = estimate_image_tokens(1920, 1080)
    assert abs(large - 4 * small) <= 1
    assert estimate_text_tokens("") >= 1
