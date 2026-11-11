#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.55.0"]
# ///
"""Build an evidence pack for reviewing agent-artifacts.

Starts a throwaway server, publishes two fixtures, attaches two agents, posts a
mix of comments, then captures the review page at desktop and mobile widths, in
light and dark mode, plus the artifact-only state, the mention picker and the
index. Writes PNG files and an evidence.json into --out.

    uv run --script scripts/review-harness.py --out /tmp/review/round-1
"""
from __future__ import annotations

import argparse
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
CLI = ROOT / "src" / "artifact_cli.py"

REPORT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quarterly review</title>
<style>
 body{font:16px/1.6 ui-serif,Georgia,serif;margin:0;padding:40px;color:#1a1c1a;background:#fff;
      max-width:900px}
 h1{font-size:2rem;margin:0 0 6px}
 .lede{color:#5d635c;margin:0 0 28px}
 h2{font-size:1.2rem;margin:32px 0 8px;border-top:1px solid #e2e5df;padding-top:16px}
 table{border-collapse:collapse;width:100%;font:13px/1.5 ui-sans-serif,system-ui,sans-serif}
 th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #e2e5df}
 th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#5d635c}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 code{font:13px ui-monospace,Menlo,monospace;background:#f4f5f2;padding:1px 4px;border-radius:3px}
 blockquote{margin:18px 0;padding:10px 14px;background:#f7f8f5;border-left:3px solid #2a5db0}
 .pill{display:inline-block;font:11px ui-sans-serif,system-ui;border:1px solid #d8dbd5;
       border-radius:999px;padding:1px 8px;color:#5d635c}
 footer{margin-top:36px;color:#5d635c;font-size:13px}
</style></head><body>
<h1>Quarterly review</h1>
<p class="lede">Churn is flat for the third quarter, and revenue grew 8%.</p>
<h2 id="headline">Summary</h2>
<p>Numbers look flat because the two biggest accounts renewed late. <span class="pill">draft</span></p>
<blockquote>Lead with the number, not the mood.</blockquote>
<h2 id="table">Accounts</h2>
<table><thead><tr><th>Account</th><th>ARR</th><th>Change</th></tr></thead><tbody>
<tr><td>Northwind</td><td class="num">1,24,000</td><td class="num">+4%</td></tr>
<tr><td>Contoso</td><td class="num">98,400</td><td class="num">-12%</td></tr>
<tr><td>Fabrikam</td><td class="num">76,200</td><td class="num">+31%</td></tr>
<tr><td>Initech</td><td class="num">51,900</td><td class="num">0%</td></tr>
</tbody></table>
<h2 id="notes">Notes</h2>
<p>Published from <code>src/artifact_cli.py</code> with a session attached.</p>
<pre><code>$ python3 src/artifact_cli.py publish report.html --title "Q4 review"
published v1 · quarterly-review
review: http://127.0.0.1:43126/a/quarterly-review</code></pre>
<footer>Prepared by the research agent.</footer>
</body></html>
"""

AUDIT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Component audit</title>
<style>
 body{font:15px/1.55 ui-sans-serif,system-ui,sans-serif;margin:0;padding:32px;background:#fbfbf9;
      color:#191b19;max-width:820px}
 h1{font-size:1.5rem;margin:0 0 18px}
 .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
 .card{background:#fff;border:1px solid #d8dbd5;border-radius:8px;padding:14px}
 .card h3{margin:0 0 6px;font-size:.95rem}
 .muted{color:#5f655d;font-size:.85rem}
 form{margin-top:24px;display:flex;gap:8px;flex-wrap:wrap}
 input,button{font:inherit;padding:7px 10px;border-radius:6px;border:1px solid #d8dbd5}
 button{background:#2a5db0;color:#fff;border-color:#2a5db0}
</style></head><body>
<h1>Component audit</h1>
<div class="grid">
 <div class="card"><h3>Buttons</h3><p class="muted">Three sizes, two weights.</p></div>
 <div class="card"><h3>Inputs</h3><p class="muted">Text, number, date.</p></div>
 <div class="card"><h3>Cards</h3><p class="muted">Elevation and border variants.</p></div>
 <div class="card"><h3>Tables</h3><p class="muted">Dense and comfortable.</p></div>
</div>
<form><input placeholder="Component name"><button type="button">Add</button></form>
</body></html>
"""

SESSION_VARS = ("AGENT_ARTIFACTS_SESSION", "PI_SESSION_ID", "CLAUDE_SESSION_ID",
                "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")


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
    with urlrequest.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode() or "{}")


def wait_health(base, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlrequest.urlopen(base + "/healthz", timeout=0.5) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise RuntimeError("artifact server did not start")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    state = tempfile.mkdtemp(prefix="agent-artifacts-review-")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AGENT_ARTIFACTS_PORT": str(port),
           "AGENT_ARTIFACTS_CONTENT_PORT": str(free_port()), "AGENT_ARTIFACTS_DIR": state}
    for name in SESSION_VARS:
        env.pop(name, None)
    server = subprocess.Popen(server_command(), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    evidence = {"base": base, "state": state, "screenshots": [], "fixtures": {}}
    try:
        wait_health(base)
        report = api(base, "POST", "/api/artifacts",
                     {"html": REPORT, "title": "Quarterly review", "as": "@pi-deepseek",
                      "session": "s-pi-0001", "cwd": "/Users/agent/app"})
        audit = api(base, "POST", "/api/artifacts",
                    {"html": AUDIT, "title": "Component audit", "as": "@codex",
                     "session": "s-codex-0001", "cwd": "/Users/agent/web"})
        slug = report["slug"]
        api(base, "POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex", "session": "s-codex-0001"})
        api(base, "POST", "/api/artifacts/component-audit/sessions",
            {"as": "@pi-deepseek", "session": "s-pi-0001"})

        first = api(base, "POST", f"/api/artifacts/{slug}/comments",
                    {"body": "@codex split the accounts table into its own section",
                     "author": "@owner", "version": 1,
                     "anchor": {"selector": "#table", "quote": "Accounts", "x": 0.5, "y": 0.4}})
        second = api(base, "POST", f"/api/artifacts/{slug}/comments",
                     {"body": "Lead with the ARR change, not the mood.",
                      "author": "@owner", "version": 1,
                      "anchor": {"selector": "#headline", "quote": "Summary", "x": 0.3, "y": 0.5}})
        third = api(base, "POST", f"/api/artifacts/{slug}/comments",
                    {"body": "This footnote is stale.", "author": "@owner", "version": 1,
                     "anchor": {"selector": "body > footer:nth-of-type(1)",
                                "quote": "Prepared by the research agent.", "x": 0.5, "y": 0.5}})
        api(base, "POST", f"/api/comments/{third['id']}/reply",
            {"body": "Removed in v2.", "author": "@pi-deepseek"})
        api(base, "POST", f"/api/comments/{third['id']}/status",
            {"status": "resolved", "author": "@owner"})
        api(base, "POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, channel="chrome")
            except Exception:  # noqa: BLE001
                browser = p.chromium.launch(headless=True)

            def shot(name, page, full=True):
                path = out / f"{name}.png"
                page.screenshot(path=str(path), full_page=full)
                evidence["screenshots"].append(str(path))
                return path

            light = browser.new_context(viewport={"width": 1280, "height": 900})
            page = light.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/", wait_until="networkidle")
            shot("01-index", page)
            page.goto(f"{base}/a/{slug}", wait_until="networkidle")
            page.wait_for_selector(".sessions .agent")
            page.wait_for_timeout(700)
            shot("02-review-desktop", page)
            page.click("#show-resolved")
            page.wait_for_timeout(400)
            shot("03-review-resolved-shown", page)
            page.click("#show-resolved")

            frame = page.frame_locator("#frame")
            page.click("#mode")
            frame.locator("#table").click()
            page.wait_for_selector("#composer:not([hidden])")
            page.click("#input")
            page.type("#input", "@")
            page.wait_for_selector("#mentions:not([hidden])")
            shot("04-mention-picker", page)
            page.keyboard.press("Escape")

            frame.locator("#headline").click()
            page.wait_for_selector("#composer:not([hidden])")
            page.fill("#input", "@codex and @pi-deepseek both check this")
            page.click("#send")
            page.wait_for_timeout(900)
            shot("05-after-send", page)

            page.click("#only")
            page.wait_for_timeout(600)
            shot("06-artifact-only", page)
            page.click("#only")
            page.wait_for_timeout(500)

            mobile = browser.new_context(viewport={"width": 390, "height": 844},
                                         device_scale_factor=2, is_mobile=True,
                                         has_touch=True)
            small = mobile.new_page()
            small.goto(f"{base}/a/{slug}", wait_until="networkidle")
            small.wait_for_selector(".sessions .agent")
            small.wait_for_timeout(600)
            shot("07-review-mobile", small)
            inside = small.frame_locator("#frame").locator("#__artifacts_layer button").evaluate_all(
                "els => els.map(e => { var r = e.getBoundingClientRect();"
                " return [Math.round(r.left), Math.round(r.right), window.innerWidth]; })")
            evidence["mobile_blobs"] = inside
            for left, right, width in inside:
                assert left >= -1 and right <= width + 1, f"blob clipped on mobile: {inside}"
            small.goto(base + "/", wait_until="networkidle")
            shot("08-index-mobile", small)

            dark = browser.new_context(viewport={"width": 1280, "height": 900},
                                       color_scheme="dark")
            dark_page = dark.new_page()
            dark_page.goto(f"{base}/a/{slug}", wait_until="networkidle")
            dark_page.wait_for_selector(".sessions .agent")
            dark_page.wait_for_timeout(600)
            shot("09-review-dark", dark_page)
            dark_page.goto(base + "/", wait_until="networkidle")
            shot("10-index-dark", dark_page)

            evidence["page_errors"] = errors
            evidence["fixtures"] = {
                "report": {"slug": slug, "url": f"{base}/a/{slug}",
                           "comments": [first["id"], second["id"], third["id"]]},
                "audit": {"slug": audit["slug"], "url": f"{base}/a/{audit['slug']}"},
            }
            browser.close()

        evidence["comments"] = api(base, "GET", f"/api/artifacts/{slug}/comments")
        evidence["sessions"] = api(base, "GET", f"/api/artifacts/{slug}/sessions")
        evidence["artifacts"] = api(base, "GET", "/api/artifacts")
        (out / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"evidence written to {out}")
        for path in evidence["screenshots"]:
            print(" ", path)
        print(f"global page errors: {evidence.get('page_errors')}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
