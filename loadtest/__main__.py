"""壓力測試 CLI（README 第 7 節）。

python -m loadtest run --fixed <URL> --auto <URL> --report-to <HUB_URL> --rate 60 --ramp 60 --duration 300
python -m loadtest probe --target <URL> --report-to <HUB_URL>
"""

import argparse
import asyncio
import random
import signal
import sys

import httpx

from loadtest.orders import OrderFactory
from loadtest.runner import run_load, run_probe

PROBE_SEED = 20261008


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m loadtest", description="外送快閃壓力測試")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="以固定到達速率同時對兩個 dispatch 施壓")
    run.add_argument("--fixed", help="dispatch-fixed 網址")
    run.add_argument("--auto", help="dispatch-auto 網址")
    run.add_argument("--report-to", required=True, help="web 網址（每秒回報、取得 /api/map）")
    run.add_argument("--rate", type=float, default=60, help="目標速率 rps（預設 60）")
    run.add_argument("--ramp", type=float, default=60, help="由 0 線性加壓到 --rate 的秒數（預設 60）")
    run.add_argument("--duration", type=float, required=True, help="總秒數（含加壓）")

    probe = sub.add_parser("probe", help="逐步加壓，量測單一 instance 的容量 C")
    probe.add_argument("--target", required=True, help="要量測的 dispatch 網址")
    probe.add_argument("--report-to", required=True, help="web 網址")
    probe.add_argument("--as", dest="name", choices=["fixed", "auto"], default="fixed", help="回報 web 時的目標名稱")
    probe.add_argument("--step", type=float, default=15, help="每階段秒數（預設 15）")
    probe.add_argument("--threshold", type=float, default=0.05, help="失敗率門檻（預設 0.05）")
    probe.add_argument("--max-rate", type=int, default=50, help="速率上限（預設 50）")

    args = parser.parse_args(argv)
    if args.command == "run" and not (args.fixed or args.auto):
        parser.error("run 至少需要 --fixed 或 --auto 其中之一")
    return args


def fetch_json(web_url: str, path: str) -> dict:
    res = httpx.get(f"{web_url.rstrip('/')}{path}", timeout=10)
    res.raise_for_status()
    return res.json()


async def _main(args: argparse.Namespace, map_json: dict, track_interval_s: float) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop():
        print("\n收到中斷，送出最後一筆回報後結束…（再按一次強制結束）", flush=True)
        stop.set()

    def on_signal(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(request_stop)

    # Windows 的 asyncio 不支援 add_signal_handler，改用 signal.signal 轉交給事件迴圈
    signals = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    previous = {s: signal.signal(s, on_signal) for s in signals}
    try:
        if args.command == "run":
            targets = {name: url for name, url in (("fixed", args.fixed), ("auto", args.auto)) if url}
            await run_load(
                targets, args.report_to, rate=args.rate, ramp=args.ramp, duration=args.duration,
                orders=OrderFactory(map_json, random.Random()), stop=stop, track_interval_s=track_interval_s,
                out=lambda line: print(line, flush=True),
            )
            return 0
        capacity = await run_probe(
            args.target, args.report_to, name=args.name, step_s=args.step, threshold=args.threshold,
            max_rate=args.max_rate, orders=OrderFactory(map_json, random.Random(PROBE_SEED), seed=PROBE_SEED),
            track_interval_s=track_interval_s,
            stop=stop, out=lambda line: print(line, flush=True),
        )
        return 0 if capacity is not None else 1
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        map_json = fetch_json(args.report_to, "/api/map")
        track_interval_s = fetch_json(args.report_to, "/api/config").get("track_interval_ms", 2000) / 1000
    except httpx.HTTPError as e:
        print(f"無法從 web 取得地圖或設定：{e}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_main(args, map_json, track_interval_s))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
