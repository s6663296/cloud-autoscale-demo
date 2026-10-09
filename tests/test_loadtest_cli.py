import argparse

import pytest

from loadtest.__main__ import parse_args, prompt_plan, resolve_targets

CONFIG = {"fixed_url": "http://f", "auto_url": "http://a", "timeout_ms": 10000, "track_interval_ms": 2000}


class Prompts:
    """依序回答互動問題，並記錄輸出與存檔。"""

    def __init__(self, answers, saved=None):
        self.answers = list(answers)
        self.lines = []
        self.saved = saved
        self.fetched = []

    def ask(self, question):
        self.lines.append(question)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def out(self, line=""):
        self.lines.append(line)

    def load(self):
        return self.saved

    def save(self, url):
        self.saved = url

    def fetch(self, web_url):
        self.fetched.append(web_url)
        return CONFIG

    def plan(self):
        return prompt_plan(ask=self.ask, out=self.out, load_saved=self.load, save=self.save, fetch_config=self.fetch)


def test_local_defaults():
    p = Prompts(["", "", "", "", "", ""])
    plan = p.plan()
    assert plan.command == "run"
    assert plan.report_to == "http://localhost:8000"
    assert (plan.fixed, plan.auto) == ("http://f", "http://a")
    assert (plan.rate, plan.ramp, plan.duration) == (8, 20, 120)
    assert p.fetched == ["http://localhost:8000"]


def test_cloud_first_time_saves_url():
    p = Prompts(["2", "https://web-xyz.a.run.app/", "1", "30", "10", "60", "y"])
    plan = p.plan()
    assert plan.report_to == "https://web-xyz.a.run.app"
    assert p.saved == "https://web-xyz.a.run.app"
    assert (plan.rate, plan.ramp, plan.duration) == (30, 10, 60)


def test_cloud_uses_saved_url_and_cloud_defaults():
    p = Prompts(["2", "", "", "", "", "", ""], saved="https://saved.run.app")
    plan = p.plan()
    assert plan.report_to == "https://saved.run.app"
    assert (plan.rate, plan.ramp, plan.duration) == (5, 60, 180)


def test_cloud_not_deployed_yet():
    p = Prompts(["2", ""])
    assert p.plan() is None
    assert any("尚未部署" in line for line in p.lines)
    assert p.fetched == []


def test_invalid_number_asks_again():
    p = Prompts(["1", "1", "abc", "-5", "12", "", "", "y"])
    plan = p.plan()
    assert plan.rate == 12
    assert sum("請輸入大於 0 的數字" in line for line in p.lines) == 2


def test_probe_mode_targets_fixed():
    p = Prompts(["1", "2", "y"])
    plan = p.plan()
    assert plan.command == "probe"
    assert plan.target == "http://f" and plan.name == "fixed"


def test_declined_confirmation():
    assert Prompts(["", "", "", "", "", "n"]).plan() is None


def test_unreachable_web_reports_error():
    p = Prompts(["1"])

    def fail(url):
        raise OSError("connection refused")

    p.fetch = fail
    assert p.plan() is None
    assert any("無法連線" in line for line in p.lines)


def test_end_of_input_cancels():
    assert Prompts([]).plan() is None


# --- 非互動模式：只需 web 網址 ----------------------------------------------


def test_run_without_target_urls_uses_web_config():
    args = parse_args(["run", "--report-to", "http://w", "--duration", "10"])
    resolve_targets(args, CONFIG)
    assert (args.fixed, args.auto) == ("http://f", "http://a")


def test_explicit_target_overrides_config():
    args = parse_args(["run", "--report-to", "http://w", "--duration", "10", "--fixed", "http://x"])
    resolve_targets(args, CONFIG)
    assert (args.fixed, args.auto) == ("http://x", "http://a")


@pytest.mark.parametrize("name, expected", [("fixed", "http://f"), ("auto", "http://a")])
def test_probe_target_from_config(name, expected):
    args = parse_args(["probe", "--report-to", "http://w", "--as", name])
    resolve_targets(args, CONFIG)
    assert args.target == expected


def test_missing_urls_everywhere_is_an_error():
    args = parse_args(["run", "--report-to", "http://w", "--duration", "10"])
    with pytest.raises(SystemExit):
        resolve_targets(args, {"fixed_url": "", "auto_url": ""})
