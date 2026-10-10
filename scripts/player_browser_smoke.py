"""Read-only real-Chrome smoke test of a running Player. Never deletes, favourites
or changes a setting.

Runs inside the official Playwright image (Google Chrome: the bundled Chromium
cannot decode H.264). Start it with scripts/player_browser_smoke.sh, which passes
the Player access secret and an output directory.

Checks: login; feed start-up and seeks; the settings read-mode label; a long video
start and uncached seeks; every long-list cover while its tile is on screen.
Prints one JSON line per step and writes report.json (and screenshots without
media frames) to /out.
"""
from __future__ import annotations

from collections import Counter
import json
import os
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PLAYER_URL", "http://127.0.0.1:8790")
OUT = "/out"
report: dict[str, object] = {"steps": []}
PLAYING = """(selector) => { const v = document.querySelector(selector);
    return !!v && !v.paused && v.readyState >= 3 && v.currentTime > 0; }"""


def step(name: str, **fields: object) -> None:
    report["steps"].append({"step": name, **fields})  # type: ignore[union-attr]
    print(json.dumps({"step": name, **fields}, ensure_ascii=False), flush=True)


def timed_play(page, selector: str, timeout_s: int = 30) -> float:
    started = time.monotonic()
    page.wait_for_function(PLAYING, arg=selector, timeout=timeout_s * 1000)
    return round(time.monotonic() - started, 2)


def seek_and_time(page, selector: str, fraction: float) -> float:
    page.evaluate(
        "([s, f]) => { const v = document.querySelector(s); v.currentTime = v.duration * f; v.play(); }",
        [selector, fraction],
    )
    page.wait_for_timeout(50)
    return timed_play(page, selector)


def feed(page) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.fill("#player-access-secret", os.environ["PLAYER_SECRET"])
    started = time.monotonic()
    page.click("button.login-submit")
    page.wait_for_selector("video.is-current", state="attached", timeout=30000)
    step("login", seconds=round(time.monotonic() - started, 2))
    page.evaluate("() => { const v = document.querySelector('video.is-current'); v.muted = true; v.play(); }")
    step("feed_start", seconds=timed_play(page, "video.is-current"))
    for fraction in (0.4, 0.7):
        step("feed_seek", to=fraction, seconds=seek_and_time(page, "video.is-current", fraction))


def settings(page) -> None:
    page.click("button.topbar-settings")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")
    step("settings", read_mode_direct="115 直连" in text, read_mode_webdav="网盘 · 稳定" in text,
         watched_window=next((line for line in text.splitlines() if "重新算作没看过" in line or line.startswith("永不")), None))
    page.screenshot(path=f"{OUT}/settings.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def open_long_list(page) -> None:
    page.click('nav.bottom-nav button[data-action="long"]')
    page.wait_for_selector("article.cover-tile", timeout=20000)


def long_video(page) -> None:
    open_long_list(page)
    tiles = page.query_selector_all("article.cover-tile button.cover-tile-play")
    tiles[min(len(tiles) - 1, 7)].click()
    page.wait_for_selector("video.large-video", state="attached", timeout=20000)
    page.evaluate("() => { document.querySelector('video.large-video').muted = true; }")
    page.wait_for_timeout(800)
    # A fresh browser first sees the one-time gesture guide, then a privacy cover.
    if page.get_by_text("点击任意位置关闭").is_visible():
        page.get_by_text("点击任意位置关闭").click()
        page.wait_for_timeout(800)
    if page.is_visible(".large-privacy-play"):
        page.click(".large-privacy-play")
    step("long_start", seconds=timed_play(page, "video.large-video"))
    for fraction in (0.33, 0.66, 0.5):
        step("long_seek", to=fraction, seconds=seek_and_time(page, "video.large-video", fraction))


def covers(page) -> None:
    """Load every page first (no later rebuild), then check each cover on screen."""
    page.goto(BASE + "/", wait_until="domcontentloaded")  # the session cookie keeps the login
    page.wait_for_selector("video.is-current", state="attached", timeout=30000)
    open_long_list(page)
    last = -1
    for _ in range(30):
        count = page.evaluate("() => document.querySelectorAll('article.cover-tile').length")
        more = page.get_by_text("加载更多")
        if count == last and not more.is_visible():
            break
        last = count
        if more.is_visible():
            more.click()
        page.wait_for_timeout(1500)
    count = page.evaluate("() => document.querySelectorAll('article.cover-tile').length")
    states = []
    for index in range(count):
        page.evaluate(
            "(i) => document.querySelectorAll('article.cover-tile')[i].scrollIntoView({block: 'center'})", index
        )
        page.wait_for_timeout(1000)
        states.append(page.evaluate("""(i) => { const t = document.querySelectorAll('article.cover-tile')[i];
            const img = t.querySelector('img');
            return {state: t.dataset.coverState || '', shown: !!(img && img.complete && img.naturalWidth > 0)}; }""",
            index))
    step("covers", tiles=count, states=Counter(s["state"] for s in states),
         not_shown=[i for i, s in enumerate(states) if not s["shown"]])


CHECKS = {"feed": feed, "settings": settings, "long": long_video, "covers": covers}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
    page = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                               is_mobile=True, has_touch=True).new_page()
    statuses: list[int] = []
    page.on("response", lambda r: statuses.append(r.status) if "/stream" in r.url else None)
    wanted = os.environ.get("SMOKE_CHECKS", "feed,settings,long").split(",")
    if wanted[0] != "feed":
        wanted.insert(0, "feed")  # every check needs the login the feed step does
    for name in wanted:
        try:
            CHECKS[name](page)
        except Exception as exc:  # noqa: BLE001 - report and go on with the next check
            step(name, error=f"{type(exc).__name__}: {str(exc)[:120]}")
    report["stream_statuses"] = dict(Counter(statuses))
    browser.close()

with open(f"{OUT}/report.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, ensure_ascii=False, indent=2)
print(json.dumps({"stream_statuses": report["stream_statuses"]}))
