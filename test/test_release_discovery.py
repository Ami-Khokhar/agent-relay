import io
import json
import os
from pathlib import Path
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from src import release_discovery as discovery


def release(tag, *, prerelease=False, draft=False, url=None, notes="release notes"):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft,
            "html_url": url or discovery.RELEASE_URL + tag, "body": notes}


class FakeResponse:
    def __init__(self, body, url=discovery.API_URL):
        self.body, self.url = body, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body[:limit]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.assertions = []

    def open(self, request, timeout):
        self.assertions.append((request.full_url, timeout))
        if self.error:
            raise self.error
        return self.response


def fake_api(releases):
    return FakeOpener(FakeResponse(json.dumps(releases).encode("utf-8")))


class ReleaseDiscoveryTests(unittest.TestCase):
    def test_selects_highest_newer_stable_release_and_bounds_notes(self):
        offer = discovery.discover("1.0.0", releases=[
            release("1.1.0", notes="n" * (discovery.MAX_NOTES + 10)),
            release("1.2.0"), release("2.0.0-rc.1", prerelease=True),
            release("9.0.0", draft=True),
            release("8.0.0", url="https://evil.example/release"),
            {"tag_name": "not-semver", "html_url": discovery.RELEASE_URL + "not-semver"},
            release("4.0.0-"),
            {"tag_name": "7.0.0", "html_url": discovery.RELEASE_URL + "7.0.0"},
            release("1.3.0", notes="red\x1b[31m text\x00"),
        ])
        self.assertEqual(offer["version"], "1.3.0")
        self.assertEqual(offer["url"], discovery.RELEASE_URL + "1.3.0")
        self.assertNotIn("\x1b", offer["notes"])
        self.assertNotIn("\x00", offer["notes"])
        self.assertEqual(len(discovery.discover("1.0.0", releases=[release("1.1.0", notes="x" * 5000)])[
            "notes"]), discovery.MAX_NOTES)

    def test_equal_and_older_are_no_update_and_prereleases_are_opt_in(self):
        releases = [release("1.0.0"), release("0.9.9"), release("1.1.0-rc.1", prerelease=True)]
        self.assertIsNone(discovery.discover("1.0.0", releases=releases))
        self.assertEqual(discovery.discover("1.0.0", True, releases)["version"], "1.1.0-rc.1")
        self.assertTrue(discovery._newer("1.0.0", "1.0.0-rc.1"))
        self.assertFalse(discovery._newer("1.0.0-rc.1", "1.0.0"))
        self.assertTrue(discovery._newer("1.0.0-alpha.1", "1.0.0-alpha"))
        self.assertFalse(discovery._newer("1.0.0-1", "1.0.0-alpha"))

    def test_selects_prerelease_versions_with_semver_precedence(self):
        candidates = [release("1.2.0-rc.2", prerelease=True),
                      release("1.2.0-rc.10", prerelease=True),
                      release("1.2.0")]
        self.assertEqual(discovery.discover("1.1.0", True, candidates)["version"], "1.2.0")
        self.assertEqual(discovery.discover("1.1.0-rc.1", True, candidates[:2])["version"], "1.2.0-rc.10")

    def test_stable_channel_ignores_prerelease_tag_when_flag_is_false(self):
        self.assertIsNone(discovery.discover(
            "1.0.0", releases=[release("1.1.0-rc.1", prerelease=False)]))

    def test_empty_release_list_is_normal_no_update(self):
        self.assertIsNone(discovery.discover("0.3.0", releases=[]))

    def test_fetch_rejects_redirects_oversize_timeout_and_unavailable_service(self):
        opener = FakeOpener(FakeResponse(b"[]"))
        with patch.object(discovery, "build_opener", return_value=opener):
            self.assertEqual(discovery.fetch_releases(), [])
        self.assertEqual(opener.assertions, [(discovery.API_URL, discovery.TIMEOUT)])
        bad_url = FakeOpener(FakeResponse(b"[]", "https://attacker.invalid/"))
        with patch.object(discovery, "build_opener", return_value=bad_url):
            with self.assertRaisesRegex(discovery.DiscoveryError, "non-canonical"):
                discovery.fetch_releases()
        huge = FakeOpener(FakeResponse(b" " * (discovery.MAX_RESPONSE + 1)))
        with patch.object(discovery, "build_opener", return_value=huge):
            with self.assertRaisesRegex(discovery.DiscoveryError, "size limit"):
                discovery.fetch_releases()
        for error in (TimeoutError("timed out"), URLError("offline")):
            with patch.object(discovery, "build_opener", return_value=FakeOpener(error=error)):
                with self.assertRaisesRegex(discovery.DiscoveryError, "(unavailable|timed out)"):
                    discovery.fetch_releases()

    def test_real_redirect_response_is_refused_without_requesting_target(self):
        requested = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                if self.path == "/api":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            endpoint = "http://127.0.0.1:%d/api" % server.server_port
            with patch.object(discovery, "API_URL", endpoint):
                with self.assertRaisesRegex(discovery.DiscoveryError, "redirected"):
                    discovery.fetch_releases()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(requested, ["/api"])

    def test_cli_notice_is_private_and_deduplicated_by_version(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            opener = fake_api([release("0.4.0", notes="hello")])
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}), \
                    patch.object(discovery, "build_opener", return_value=opener), \
                    patch("sys.stdout", output):
                self.assertEqual(discovery.main(["--notify"]), 0)
                first = output.getvalue()
                output.seek(0)
                output.truncate()
                self.assertEqual(discovery.main(["--notify"]), 0)
            self.assertIn("Installed version: 0.3.0 (source checkout)", first)
            self.assertIn("Available version: 0.4.0", first)
            self.assertIn("hello", first)
            self.assertEqual(output.getvalue(), "Installed version: 0.3.0 (source checkout)\n")
            cache = Path(temp) / "agent-relay" / "release-notices.json"
            self.assertEqual(json.loads(cache.read_text()), {"version": "0.4.0"})
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)

    def test_malformed_cache_is_replaced_and_cache_write_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "agent-relay" / "release-notices.json"
            cache.parent.mkdir(mode=0o700)
            cache.write_text("[]")
            offer = {"version": "0.4.0"}
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                self.assertTrue(discovery._notify_once(offer))
                self.assertEqual(json.loads(cache.read_text()), {"version": "0.4.0"})
                output, error = io.StringIO(), io.StringIO()
                opener = fake_api([release("0.5.0")])
                with patch.object(discovery, "build_opener", return_value=opener), \
                        patch.object(discovery.os, "replace", side_effect=OSError("private path")), \
                        patch("sys.stdout", output), patch("sys.stderr", error):
                    self.assertEqual(discovery.main(["--notify"]), 1)
                self.assertIn("Release notice failed: cannot save the private notice cache", error.getvalue())
                self.assertNotIn("private path", error.getvalue())
            self.assertFalse(list(cache.parent.glob("release-notice-*")))

    def test_cli_empty_result_reports_no_release_and_does_not_create_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            opener = fake_api([])
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}), \
                    patch.object(discovery, "build_opener", return_value=opener), \
                    patch("sys.stdout", output):
                self.assertEqual(discovery.main(["--notify"]), 0)
            self.assertIn("No newer published stable release", output.getvalue())
            self.assertFalse((Path(temp) / "agent-relay").exists())

    def test_cli_service_failure_is_reported_without_creating_notice_state(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            error = io.StringIO()
            opener = FakeOpener(error=URLError("https://user:password@proxy.invalid/private"))
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}), \
                    patch.object(discovery, "build_opener", return_value=opener), \
                    patch("sys.stdout", output), patch("sys.stderr", error):
                self.assertEqual(discovery.main(["--notify"]), 1)
            self.assertIn("Release check failed: release service unavailable", error.getvalue())
            self.assertNotIn("proxy.invalid", error.getvalue())
            self.assertNotIn("password", error.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse((Path(temp) / "agent-relay").exists())


if __name__ == "__main__":
    unittest.main()
