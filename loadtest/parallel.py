"""多程序壓測：單一 Python 程序的事件迴圈在高 RPS 下會跑滿一個核心，
請求的回應要排隊等迴圈處理，量到的延遲會包含壓測工具自己的排隊時間。
因此把到達速率平均分給數個子程序，各自施壓並直接回報 web（web 端的指標本來就是累加），
主程序只負責合併每秒統計並印出。
"""

import asyncio
import multiprocessing as mp
import os
import queue
import random
import signal
from contextlib import contextmanager
from typing import Callable

from loadtest.orders import OrderFactory
from loadtest.runner import MAX_INFLIGHT, run_load
from loadtest.stats import Window, merge_windows, summary_line, total_lines

DEFAULT_PROCS = max(1, min(4, (os.cpu_count() or 2) // 2))
STALE_TICKS = 2  # 某一秒等了這麼多秒仍缺子程序的統計，就先印出已收到的部分


@contextmanager
def stop_on_signal(stop: asyncio.Event, announce: Callable[[], None] | None = None):
    """第一次 Ctrl+C 設定 stop，第二次強制結束。

    Windows 的 asyncio 不支援 add_signal_handler，改用 signal.signal 轉交給事件迴圈。
    """
    loop = asyncio.get_running_loop()

    def request_stop():
        if announce:
            announce()
        stop.set()

    def on_signal(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(request_stop)

    signals = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    previous = {s: signal.signal(s, on_signal) for s in signals}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


class Merger:
    """依子程序的回報序號（第幾秒）合併統計，全部到齊或逾期時印出一行。"""

    def __init__(self, procs: int, out: Callable[[str], None]):
        self.procs = procs
        self.out = out
        self.pending: dict[int, list[tuple]] = {}

    def add(self, tick: int, elapsed: float, rate: float, windows: dict, inflight: dict, delivering: dict) -> None:
        parts = self.pending.setdefault(tick, [])
        parts.append((elapsed, rate, windows, inflight, delivering))
        if len(parts) >= self.procs:
            self._emit(tick)
        for stale in sorted(t for t in self.pending if t <= tick - STALE_TICKS):
            self._emit(stale)

    def flush(self) -> None:
        for tick in sorted(self.pending):
            self._emit(tick)

    def _emit(self, tick: int) -> None:
        parts = self.pending.pop(tick)
        inflight: dict[str, int] = {}
        delivering: dict[str, int] = {}
        for _, _, _, i, d in parts:
            for name, n in i.items():
                inflight[name] = inflight.get(name, 0) + n
            for name, n in d.items():
                delivering[name] = delivering.get(name, 0) + n
        self.out(summary_line(
            max(p[0] for p in parts), sum(p[1] for p in parts),
            merge_windows([p[2] for p in parts]), inflight, delivering,
        ))


def _child(idx: int, targets: dict, web_url: str, rate: float, ramp: float, duration: float,
           max_inflight: int, map_json: dict, track_interval_s: float, q) -> None:
    async def main():
        stop = asyncio.Event()
        tick = 0

        def on_window(elapsed, rate_now, windows, inflight, delivering):
            nonlocal tick
            tick += 1
            q.put(("window", tick, elapsed, rate_now, windows, inflight, delivering))

        with stop_on_signal(stop):
            totals, abandoned = await run_load(
                targets, web_url, rate=rate, ramp=ramp, duration=duration,
                orders=OrderFactory(map_json, random.Random()), stop=stop, track_interval_s=track_interval_s,
                max_inflight=max_inflight, out=lambda line: q.put(("log", idx, line)),
                on_window=on_window, print_totals=False,
            )
        q.put(("done", idx, totals, abandoned))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def run_parallel(targets: dict[str, str], web_url: str, *, rate: float, ramp: float, duration: float,
                 map_json: dict, track_interval_s: float, procs: int,
                 out: Callable[[str], None] = print) -> int:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    children = [
        ctx.Process(
            target=_child, daemon=True,
            args=(i, targets, web_url, rate / procs, ramp, duration, max(1, MAX_INFLIGHT // procs),
                  map_json, track_interval_s, q),
        )
        for i in range(procs)
    ]
    presses = 0

    # Ctrl+C 會送到同一個主控台的所有程序：子程序各自收尾，主程序只提示一次
    def on_signal(signum, frame):
        nonlocal presses
        presses += 1
        if presses > 1:
            raise KeyboardInterrupt
        out("\n收到中斷，送出最後一筆回報後結束…（再按一次強制結束）")

    signals = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    previous = {s: signal.signal(s, on_signal) for s in signals}
    out(f"使用 {procs} 個程序施壓")
    try:
        for c in children:
            c.start()
        merger = Merger(procs, out)
        totals: list[dict[str, Window]] = []
        abandoned: dict[str, int] = {}
        done = 0
        while done < procs:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                if not any(c.is_alive() for c in children):
                    break
                continue
            kind = msg[0]
            if kind == "window":
                merger.add(*msg[1:])
            elif kind == "log":
                _, idx, line = msg
                # 每個子程序都會印相同的進度訊息，只留第一個；警告全部保留
                if idx == 0 or line.startswith("警告"):
                    out(line)
            elif kind == "done":
                done += 1
                totals.append(msg[2])
                for name, n in msg[3].items():
                    abandoned[name] = abandoned.get(name, 0) + n
        merger.flush()
        for line in total_lines(merge_windows(totals), abandoned):
            out(line)
        return 0 if done == procs else 1
    except KeyboardInterrupt:
        for c in children:
            c.terminate()
        return 130
    finally:
        for c in children:
            c.join(timeout=5)
        for s, handler in previous.items():
            signal.signal(s, handler)
