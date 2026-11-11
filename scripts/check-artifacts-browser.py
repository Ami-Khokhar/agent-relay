#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.55.0"]
# ///
"""End-to-end browser check for reviewable artifacts.

Runs the real review page in Chrome: two agents attached, comment mode, the @
picker, an addressed comment, delivery badges, a reply, resolve, and the
Artifact only round trip.

    uv run --script scripts/check-artifacts-browser.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urlrequest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "artifact_server.py"
SERVER_BIN = os.environ.get("ARTIFACT_SERVER_BIN")


def server_command():
    return [SERVER_BIN, "serve"] if SERVER_BIN else [sys.executable, str(SERVER)]
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Demo</title>
<style>body{font:16px/1.6 system-ui;margin:40px}#headline{color:#123}</style></head>
<body><h1 id="headline">Quarterly review</h1><p class="lead">Churn is flat.</p>
<h2>Summary</h2><p>Numbers look flat for the third quarter.</p></body></html>
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def api(base, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urlrequest.Request(base + path, data=data, method=method)
    request.add_header("x-artifacts-client", "1")
    if data is not None:
        request.add_header("content-type", "application/json")
    with urlrequest.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode() or "{}")


def wait_for(predicate, timeout=8.0, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting for {label}")


def main() -> int:
    state = tempfile.mkdtemp(prefix="agent-artifacts-browser-")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AGENT_ARTIFACTS_PORT": str(port),
           "AGENT_ARTIFACTS_CONTENT_PORT": str(free_port()), "AGENT_ARTIFACTS_DIR": state}
    for name in ("AGENT_ARTIFACTS_SESSION", "PI_SESSION_ID", "CLAUDE_SESSION_ID",
                 "CODEX_SESSION_ID"):
        env.pop(name, None)
    server = subprocess.Popen(server_command(), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(lambda: _health(base), label="server")
        page = api(base, "POST", "/api/artifacts",
                   {"html": PAGE, "title": "Quarterly review", "as": "@pi-deepseek"})
        slug = page["slug"]
        api(base, "POST", f"/api/artifacts/{slug}/sessions",
            {"as": "@codex", "session": "s-codex-1"})
        review = f"{base}/a/{slug}"

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, channel="chrome")
            except Exception:  # noqa: BLE001 - fall back to the bundled build
                browser = p.chromium.launch(headless=True)
            tab = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            tab.on("pageerror", lambda error: errors.append(str(error)))
            tab.goto(review, wait_until="networkidle")
            frame = tab.frame_locator("#frame")
            frame.locator("#headline").wait_for()

            # A poll must never reload the stage under the reviewer's scroll.
            frame.locator("body").evaluate("el => { window.__probe = 'alive'; }")
            time.sleep(6)
            assert frame.locator("body").evaluate("() => window.__probe") == "alive", \
                "the artifact frame reloaded on its own while idle"

            # Both agents are listed with their role.
            wait_for(lambda: tab.locator(".sessions .agent").count() == 2, label="session strip")
            chips = tab.locator(".sessions .agent")
            assert chips.nth(0).inner_text().strip() == "@pi-deepseek", chips.nth(0).inner_text()
            assert "origin" in chips.nth(0).get_attribute("class"), chips.nth(0).get_attribute("class")
            assert chips.nth(1).inner_text().strip() == "@codex"

            bar = frame.locator("#__artifacts_toggle")
            bar.wait_for()
            assert bar.inner_text().strip() == "Comment"
            bar.click()
            wait_for(lambda: tab.get_attribute("#mode", "aria-pressed") == "true",
                     label="header button to follow the in-page toggle")

            # Artifact only must never strand the reviewer.
            tab.click("#only")
            wait_for(lambda: tab.locator("aside").is_hidden(), label="panel hidden")
            assert tab.inner_text("#only").strip() == "Back to review"
            assert frame.locator("#__artifacts_toggle").count() == 0, "overlay still injected"
            tab.screenshot(path=os.path.join(state, "artifact-only.png"))
            tab.click("#only")
            frame.locator("#headline").wait_for()
            wait_for(lambda: tab.locator("aside").is_visible(), label="panel back")

            frame.locator("h2").first.hover()
            frame.locator("h2").first.click()
            tab.wait_for_selector("#composer:not([hidden])")
            assert "Summary" in tab.inner_text("#pick-quote"), tab.inner_text("#pick-quote")

            # Typing @ lists the agents with their roles, and Enter picks one.
            tab.click("#input")
            tab.type("#input", "@")
            tab.wait_for_selector("#mentions:not([hidden])")
            options = tab.locator("#mentions button")
            labels = [options.nth(i).inner_text().replace("\n", " ") for i in range(options.count())]
            assert any("@owner" in text for text in labels), labels
            assert any("@codex" in text and "idle" in text for text in labels), labels
            assert any("@pi-deepseek" in text and "publisher" in text for text in labels), labels
            tab.keyboard.press("ArrowDown")   # @owner -> @pi-deepseek
            tab.keyboard.press("ArrowDown")   # -> @codex
            tab.keyboard.press("Enter")
            assert "@codex" in tab.input_value("#input"), tab.input_value("#input")
            assert tab.get_attribute('#targets button:has-text("@codex")', "aria-pressed") == "true"

            tab.type("#input", "split the table out of the summary")
            tab.screenshot(path=os.path.join(state, "composer-mention.png"))
            tab.click("#send")

            comment = wait_for(
                lambda: (api(base, "GET", f"/api/artifacts/{slug}/comments")["comments"] or [None])[0],
                label="comment to be stored",
            )
            assert comment["to"] == ["@codex"], comment["to"]
            assert comment["anchor"]["selector"] == "body > h2:nth-of-type(1)", comment["anchor"]
            assert comment["author"] == "@owner", comment["author"]

            wait_for(lambda: "@codex" in tab.inner_text("#list"), label="delivery badge")
            assert "awaits that agent" in tab.inner_text("#list"), tab.inner_text("#list")
            wait_for(lambda: frame.locator("#__artifacts_layer button").count() == 1,
                     label="comment blob inside the artifact")
            blob = frame.locator("#__artifacts_layer button").first
            assert blob.inner_text().strip() == "1", blob.inner_text()
            tab.screenshot(path=os.path.join(state, "review-open.png"))

            # The addressed agent claims it, and the badge flips to delivered.
            inbox = api(base, "POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
            assert [item["id"] for item in inbox["comments"]] == [comment["id"]], inbox
            wait_for(lambda: "delivered" in tab.inner_text("#list"), label="delivered badge")
            api(base, "POST", f"/api/comments/{comment['id']}/reply",
                {"body": "Moved to its own table in v2.", "author": "@codex"})
            wait_for(lambda: "Moved to its own table" in tab.inner_text("#list"),
                     label="reply in sidebar")

            # A second comment on the same element must not hide or block the first.
            second = api(base, "POST", f"/api/artifacts/{slug}/comments",
                         {"body": "second note on the same heading", "author": "@owner",
                          "version": 1,
                          "anchor": {"selector": "body > h2:nth-of-type(1)", "quote": "Summary",
                                     "x": 0.5, "y": 0.5}})
            wait_for(lambda: frame.locator("#__artifacts_layer button").count() == 2,
                     label="second blob")
            spots = frame.locator("#__artifacts_layer button").evaluate_all(
                "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
            assert len(set(spots)) == 2, f"blobs overlap at the same spot: {spots}"
            frame.locator("#__artifacts_layer button").nth(1).click()
            wait_for(lambda: "second note" in tab.inner_text("#list"), label="blob click")

            # Republishing without that element must show a new version, and the
            # orphaned comment must stay visible instead of vanishing.
            api(base, "POST", "/api/artifacts",
                {"html": PAGE.replace("<h2>Summary</h2>", ""),
                 "title": "Quarterly review", "slug": slug, "as": "@pi-deepseek",
                 "session": "s-pi-deepseek-1"})
            tab.wait_for_selector("#newversion:not([hidden])", timeout=15000)
            assert "v2" in tab.inner_text("#newversion"), tab.inner_text("#newversion")
            tab.click("#newversion")
            frame.locator("#headline").wait_for()
            wait_for(lambda: frame.locator("#__artifacts_layer button").count() == 2,
                     label="blobs after republish")
            positions = frame.locator("#__artifacts_layer button").evaluate_all(
                "els => els.map(e => getComputedStyle(e).position)")
            assert "fixed" in positions, f"orphaned blob is not parked in view: {positions}"
            tab.screenshot(path=os.path.join(state, "review-v2-orphan.png"))

            tab.click("#show-resolved")
            wait_for(lambda: tab.locator(".item").count() >= 2, label="all comments listed")
            tab.click("#show-resolved")

            for _ in range(6):
                open_toggles = tab.locator('.item button[data-act="toggle"]:has-text("Resolve")')
                if open_toggles.count() == 0:
                    break
                open_toggles.first.click()
                time.sleep(0.4)
            wait_for(lambda: all(item["status"] == "resolved"
                                 for item in api(base, "GET", f"/api/artifacts/{slug}/comments")
                                 ["comments"]), label="every comment resolved")
            wait_for(lambda: "0 open" in tab.inner_text("#count"), label="open count")

            tab.reload(wait_until="networkidle")
            frame.locator("#headline").wait_for()
            time.sleep(0.6)
            assert frame.locator("#__artifacts_layer button").count() == 0, "resolved blob still drawn"
            tab.screenshot(path=os.path.join(state, "review-resolved.png"))
            assert not errors, errors
            browser.close()

        print(f"PASS  {slug}: 2 sessions, @picker, addressed to @codex, claimed, replied, resolved")
        print(f"      screenshots in {state}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def _health(base) -> bool:
    try:
        with urlrequest.urlopen(base + "/healthz", timeout=0.5) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
