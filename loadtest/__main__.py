"""壓力測試 CLI（README 第 7 節）。

python -m loadtest                     互動模式：選擇本機或雲端、壓測或量測容量
python -m loadtest run --report-to <WEB_URL> --rate 60 --ramp 60 --duration 300
python -m loadtest probe --report-to <WEB_URL>

固定版與擴展版的網址預設取自 web 的 /api/config，只需提供 web 網址。
"""

import argparse
import asyncio
import json
import random
import signal
import sys
from pathlib import Path
from typing import Callable

import httpx

from loadtest.orders import OrderFactory
from loadtest.runner import run_load, run_probe

PROBE_SEED = 20261008
LOCAL_WEB = "http://localhost:8000"
SAVED_FILE = Path(__file__).resolve().parent.parent / ".loadtest.json"
# 本機固定版只有 1 個程序、擴展版 8 個 worker：每秒 8 位新顧客會讓固定版過載、擴展版撐得住
DEFAULTS = {"local": (8, 20, 120), "cloud": (60, 60, 300)}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m loadtest", description="外送快閃壓力測試（不帶參數為互動模式）")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="以固定到達速率同時對兩個 dispatch 施壓")
    run.add_argument("--report-to", required=True, help="web 網址（每秒回報、取得地圖與設定）")
    run.add_argument("--fixed", help="dispatch-fixed 網址（預設取自 web 設定）")
    run.add_argument("--auto", help="dispatch-auto 網址（預設取自 web 設定）")
    run.add_argument("--rate", type=float, default=60, help="每秒新顧客數（預設 60）")
    run.add_argument("--ramp", type=float, default=60, help="由 0 線性加壓到 --rate 的秒數（預設 60）")
    run.add_argument("--duration", type=float, required=True, help="產生新顧客的總秒數（含加壓）")

    probe = sub.add_parser("probe", help="逐步加壓，量測單一 instance 的容量 C")
    probe.add_argument("--report-to", required=True, help="web 網址")
    probe.add_argument("--as", dest="name", choices=["fixed", "auto"], default="fixed", help="量測哪個版本（預設 fixed）")
    probe.add_argument("--target", help="要量測的 dispatch 網址（預設取自 web 設定）")
    probe.add_argument("--step", type=float, default=15, help="每階段秒數（預設 15）")
    probe.add_argument("--threshold", type=float, default=0.05, help="失敗率門檻（預設 0.05）")
    probe.add_argument("--max-rate", type=int, default=50, help="速率上限（預設 50）")
    return parser.parse_args(argv)


def resolve_targets(args: argparse.Namespace, config: dict) -> None:
    """未指定的 dispatch 網址以 web 的 /api/config 補上。"""
    if args.command == "run":
        args.fixed = args.fixed or config.get("fixed_url") or None
        args.auto = args.auto or config.get("auto_url") or None
        if not (args.fixed or args.auto):
            sys.exit("錯誤：web 設定中沒有 dispatch 網址，請以 --fixed / --auto 指定")
    else:
        args.target = args.target or config.get(f"{args.name}_url") or None
        if not args.target:
            sys.exit(f"錯誤：web 設定中沒有 {args.name} 的網址，請以 --target 指定")


# --- 互動模式 -----------------------------------------------------------------


def prompt_plan(
    *,
    ask: Callable[[str], str],
    out: Callable[..., None],
    load_saved: Callable[[], str | None],
    save: Callable[[str], None],
    fetch_config: Callable[[str], dict],
) -> argparse.Namespace | None:
    """逐步詢問壓測設定；取消、輸入結束或無法連線時回傳 None。"""
    try:
        return _prompt(ask, out, load_saved, save, fetch_config)
    except (EOFError, KeyboardInterrupt):
        out("\n已取消")
        return None


def _choice(ask, question: str, options: list[str], default: str) -> str:
    while True:
        answer = ask(question).strip() or default
        if answer in options:
            return answer


def _number(ask, out, question: str, default: float, allow_zero: bool = False) -> float:
    while True:
        answer = ask(f"{question} [{default:g}]: ").strip()
        if not answer:
            return default
        try:
            value = float(answer)
        except ValueError:
            value = -1
        if value > 0 or (allow_zero and value == 0):
            return value
        out("請輸入 0 以上的數字" if allow_zero else "請輸入大於 0 的數字")


def _prompt(ask, out, load_saved, save, fetch_config) -> argparse.Namespace | None:
    out("外送快閃 壓力測試")
    out("目標環境：")
    out(f"  1) 本機（run_local.bat 啟動的服務，web {LOCAL_WEB}）")
    out("  2) 雲端（Cloud Run）")
    env = "local" if _choice(ask, "請選擇 [1]: ", ["1", "2"], "1") == "1" else "cloud"

    if env == "cloud":
        saved = load_saved()
        hint = f" [{saved}]" if saved else "（部署後由 deploy.sh 印出）"
        web = (ask(f"web 網址{hint}: ").strip() or saved or "").rstrip("/")
        if not web:
            out("雲端尚未部署：請先完成 P7 部署，或貼上 web 的網址。")
            return None
    else:
        web = LOCAL_WEB

    try:
        config = fetch_config(web)
    except Exception as e:  # 連線錯誤、HTTP 錯誤或格式錯誤都在這裡提示
        tip = "請先雙擊 run_local.bat 啟動本機服務。" if env == "local" else "請確認網址與服務狀態。"
        out(f"無法連線到 {web}：{e}。{tip}")
        return None
    if env == "cloud" and web != load_saved():
        save(web)
    out(f"固定容量版：{config.get('fixed_url') or '（未設定）'}")
    out(f"自動擴展版：{config.get('auto_url') or '（未設定）'}")

    out("模式：")
    out("  1) 壓測（同時對兩個版本施壓）")
    out("  2) 量測容量（probe，只測固定版）")
    mode = _choice(ask, "請選擇 [1]: ", ["1", "2"], "1")

    if mode == "2":
        plan = argparse.Namespace(
            command="probe", report_to=web, name="fixed", target=config.get("fixed_url"),
            step=15, threshold=0.05, max_rate=50, config=config,
        )
        summary = "量測固定版容量：從每秒 1 位新顧客開始，每 15 秒加 1"
    else:
        rate_d, ramp_d, duration_d = DEFAULTS[env]
        rate = _number(ask, out, "每秒新顧客數", rate_d)
        ramp = _number(ask, out, "加壓秒數（由 0 線性增加）", ramp_d, allow_zero=True)
        duration = _number(ask, out, "產生新顧客的總秒數", duration_d)
        plan = argparse.Namespace(
            command="run", report_to=web, fixed=config.get("fixed_url"), auto=config.get("auto_url"),
            rate=rate, ramp=ramp, duration=duration, config=config,
        )
        summary = f"每秒 {rate:g} 位新顧客、加壓 {ramp:g} 秒、持續 {duration:g} 秒（之後等配送完成）"

    out(summary)
    if ask("確認開始？[Y/n]: ").strip().lower() not in ("", "y", "yes"):
        out("已取消")
        return None
    return plan


def _load_saved() -> str | None:
    try:
        return json.loads(SAVED_FILE.read_text(encoding="utf-8")).get("cloud_web_url")
    except (OSError, ValueError):
        return None


def _save(url: str) -> None:
    SAVED_FILE.write_text(json.dumps({"cloud_web_url": url}, ensure_ascii=False, indent=2), encoding="utf-8")


# --- 執行 ---------------------------------------------------------------------


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
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        args = prompt_plan(
            ask=input, out=print, load_saved=_load_saved, save=_save,
            fetch_config=lambda web: fetch_json(web, "/api/config"),
        )
        if args is None:
            return 1
        config = args.config
    else:
        args = parse_args(argv)
        try:
            config = fetch_json(args.report_to, "/api/config")
        except httpx.HTTPError as e:
            print(f"無法從 web 取得設定：{e}", file=sys.stderr)
            return 1
        resolve_targets(args, config)
    try:
        map_json = fetch_json(args.report_to, "/api/map")
    except httpx.HTTPError as e:
        print(f"無法從 web 取得地圖：{e}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_main(args, map_json, config.get("track_interval_ms", 2000) / 1000))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
