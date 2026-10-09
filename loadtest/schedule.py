"""開放式負載排程器（README 第 7 節）：依到達速率排定發送時刻，不等待回應。

ramp 秒內速率由 0 線性增加到 rate，之後維持 rate。
第 k 筆（k 從 0 起）的發送時刻，是累積請求數 N(t) = ∫rate 剛好達到 k 的時間，
因此加壓開始時速率為 0 也能正確排程。
"""

import asyncio
import math
from typing import Awaitable, Callable, Iterator


def rate_at(t: float, rate: float, ramp: float) -> float:
    if ramp <= 0 or t >= ramp:
        return rate
    return rate * t / ramp


def send_times(rate: float, ramp: float, duration: float) -> Iterator[float]:
    if rate <= 0:
        return
    ramp = max(ramp, 0.0)
    ramp_count = rate * ramp / 2  # 加壓期間送出的總筆數
    k = 0
    while True:
        if k <= ramp_count and ramp > 0:
            t = math.sqrt(2 * k * ramp / rate)
        else:
            t = ramp + (k - ramp_count) / rate
        if t >= duration:
            return
        yield t
        k += 1


async def run_schedule(
    rate: float,
    ramp: float,
    duration: float,
    fire: Callable[[], None],
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    stop: asyncio.Event | None = None,
) -> None:
    """到發送時刻就呼叫 fire()；落後時立即補發，不等待先前的請求。

    最後一筆送出後仍等到 duration 結束才返回，讓連續的階段首尾相接。
    """
    start = clock()
    for t in [*send_times(rate, ramp, duration), duration]:
        if stop is not None and stop.is_set():
            return
        delay = start + t - clock()
        if delay > 0:
            await sleep(delay)
            if stop is not None and stop.is_set():
                return
        if t < duration:
            fire()
