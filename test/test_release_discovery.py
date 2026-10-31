import io
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import re
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_discovery as discovery


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


def _contending_notice_worker(start, results, write_delay):
    start.wait(20)
    if write_delay:
        slowed = discovery._write_cache

        def delayed(*args, **kwargs):
            time.sleep(write_delay)
            return slowed(*args, **kwargs)

        discovery._write_cache = delayed
    results.put(discovery.cached_notice("0.3.0"))


def _refresh_worker(start, results):
    slowed = discovery._read_cache

    def delayed():
        cache = slowed()
        time.sleep(0.9)
        return cache

    discovery._read_cache = delayed
    start.wait(20)
    results.put(discovery.cache_latest_offer())


def _client_worker(start, results):
    slowed = discovery._write_cache

    def delayed(*args, **kwargs):
        time.sleep(0.3)
        return slowed(*args, **kwargs)

    discovery._write_cache = delayed
    start.wait(20)
    results.put(discovery.cached_notice("0.3.0"))


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
        self.assertEqual(discovery.discover("1.0.0-rc.1", releases=[release("1.0.0")])["version"], "1.0.0")
        self.assertIsNone(discovery.discover("1.0.0", releases=[release("1.0.0-rc.1", prerelease=True)]))
        self.assertEqual(discovery.discover("1.0.0-alpha", True,
                                            [release("1.0.0-alpha.1", prerelease=True)])["version"],
                         "1.0.0-alpha.1")
        self.assertIsNone(discovery.discover("1.0.0-alpha", True,
                                             [release("1.0.0-1", prerelease=True)]))

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

    def test_fetch_maps_malformed_http_response(self):
        opener = FakeOpener(FakeResponse(b"[]"))
        opener.response.read = lambda limit: (_ for _ in ()).throw(http.client.IncompleteRead(b"", 10))
        with patch.object(discovery, "build_opener", return_value=opener):
            with self.assertRaisesRegex(discovery.DiscoveryError, "incomplete response"):
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
            version_text = (Path(discovery.__file__).parent.parent / "pyproject.toml").read_text()
            installed = re.search(r'^version\s*=\s*"([^"]+)"', version_text, re.M).group(1)
            install_kind = ("source checkout" if (Path(discovery.__file__).parent.parent / ".git").exists()
                            else "non-git installation; update method unknown")
            installed_line = "Installed version: %s (%s)" % (installed, install_kind)
            self.assertIn(installed_line, first)
            self.assertIn("Available version: 0.4.0", first)
            self.assertIn("hello", first)
            self.assertEqual(output.getvalue(), installed_line + "\n")
            cache = Path(temp) / "agent-relay" / "release-notices.json"
            cached = json.loads(cache.read_text())
            self.assertEqual(cached["notifiedVersion"], "0.4.0")
            self.assertEqual(cached["offer"]["version"], "0.4.0")
            if os.name == "posix":
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)

    def test_cache_path_ignores_empty_and_relative_xdg_values(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            for value in ("", "relative-cache"):
                with self.subTest(value=value), patch.dict(os.environ, {"XDG_CACHE_HOME": value}), \
                        patch.object(Path, "home", return_value=home):
                    self.assertEqual(discovery._cache_path(), home / ".cache" / "agent-relay" / "release-notices.json")

    def test_notice_write_skips_posix_modes_on_non_posix(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(discovery, "_IS_POSIX", False), \
                patch.object(discovery.os, "chmod", side_effect=AssertionError("chmod called")), \
                patch.object(discovery.os, "fchmod", side_effect=AssertionError("fchmod called"), create=True), \
                patch.object(Path, "home", return_value=Path(temp)), \
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            self.assertTrue(discovery._notify_once({"version": "1.2.3"}))

    def test_cli_reports_non_semver_installed_version_actionably(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(Path, "read_text", return_value='version = "0.3.0.dev1"'), \
                patch("sys.stdout", output), patch("sys.stderr", error):
            self.assertEqual(discovery.main([]), 1)
        self.assertIn("installed version is not semantic versioning", error.getvalue())

    def test_malformed_cache_is_replaced_and_cache_write_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "agent-relay" / "release-notices.json"
            cache.parent.mkdir(mode=0o700)
            cache.write_text("[]")
            offer = {"version": "0.4.0"}
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                self.assertTrue(discovery._notify_once(offer))
                self.assertEqual(json.loads(cache.read_text())["notifiedVersion"], "0.4.0")
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

    def test_cached_notice_validates_fresh_canonical_offer_and_marks_only_version(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                offer = {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                         "notes": "notes are not included in the notice", "checkedAt": time.time()}
                discovery._write_cache(offer)
                self.assertEqual(discovery.cached_notice("0.3.0"),
                                 {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0"})
                cache_path = Path(temp) / "agent-relay" / "release-notices.json"
                self.assertEqual(json.loads(cache_path.read_text())["notifiedVersion"], "0.4.0")
                self.assertIsNone(discovery.cached_notice("0.3.0"))
                cache = json.loads(cache_path.read_text())
                self.assertEqual(cache["notifiedVersion"], "0.4.0")
                self.assertEqual(cache["offer"]["notes"], "notes are not included in the notice")

    def test_cached_notice_ignores_untrusted_or_expired_cache_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent-relay" / "release-notices.json"
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                path.parent.mkdir(mode=0o700)
                offer = {"version": "0.4.0", "url": "https://attacker.invalid/release",
                         "notes": "x", "checkedAt": time.time()}
                path.write_text(json.dumps({"offer": offer, "notifiedVersion": None}))
                os.chmod(path, 0o600)
                self.assertIsNone(discovery.cached_notice("0.3.0"))
                offer["url"] = discovery.RELEASE_URL + "0.4.0"
                offer["checkedAt"] = time.time() - discovery.OFFER_MAX_AGE - 1
                path.write_text(json.dumps({"offer": offer, "notifiedVersion": None}))
                self.assertIsNone(discovery.cached_notice("0.3.0"))
                if os.name == "posix":
                    os.chmod(path, 0o644)
                    self.assertIsNone(discovery.cached_notice("0.3.0"))

    def test_scheduled_refresh_preserves_notice_history_and_caches_no_offer(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
                first = {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                         "notes": "old", "checkedAt": time.time()}
                discovery._write_cache(first)
                self.assertEqual(discovery.cached_notice("0.3.0")["version"], "0.4.0")
                with patch.object(discovery, "build_opener",
                                  return_value=fake_api([release("0.5.0")])):
                    installed, offer = discovery.cache_latest_offer()
                self.assertEqual(installed, "0.3.0")
                self.assertEqual(offer["version"], "0.5.0")
                cache = json.loads((Path(temp) / "agent-relay" / "release-notices.json").read_text())
                self.assertEqual(cache["notifiedVersion"], "0.4.0")
                self.assertEqual(discovery.cached_notice(installed)["version"], "0.5.0")
                with patch.object(discovery, "build_opener", return_value=fake_api([])):
                    installed, offer = discovery.cache_latest_offer()
                self.assertEqual(installed, "0.3.0")
                self.assertIsNone(offer)
                cache = json.loads((Path(temp) / "agent-relay" / "release-notices.json").read_text())
                self.assertIsNone(cache["offer"])
                self.assertEqual(cache["notifiedVersion"], "0.5.0")

    def test_maximum_astral_notes_survive_cache_round_trip_and_deliver_once(self):
        notes = "\U0001F680" * discovery.MAX_NOTES
        offer = discovery.discover("0.3.0", releases=[release("0.4.0", notes=notes)])
        self.assertEqual(offer["notes"], notes)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            with patch.object(discovery, "build_opener",
                              return_value=fake_api([release("0.4.0", notes=notes)])):
                installed, fetched = discovery.cache_latest_offer()
            self.assertEqual(installed, "0.3.0")
            self.assertEqual(fetched["notes"], notes)
            cache_path = Path(temp) / "agent-relay" / "release-notices.json"
            self.assertLessEqual(cache_path.stat().st_size, discovery.MAX_CACHE)
            self.assertEqual(discovery.cached_notice("0.3.0"),
                             {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0"})
            self.assertIsNone(discovery.cached_notice("0.3.0"))
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["offer"]["notes"], notes)
            self.assertEqual(len(saved["offer"]["notes"]), discovery.MAX_NOTES)
            self.assertEqual(saved["notifiedVersion"], "0.4.0")

    @unittest.skipUnless(os.name == "posix", "cross-process cache locking is a POSIX guarantee")
    def test_concurrent_cached_notice_calls_deliver_exactly_one_notice(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            discovery._write_cache({"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                                    "notes": "notes", "checkedAt": time.time()})
            workers = 4
            context = multiprocessing.get_context("fork")
            start = context.Barrier(workers)
            results = context.Queue()
            processes = [context.Process(target=_contending_notice_worker,
                                         args=(start, results, 0.2)) for _ in range(workers)]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(30)
                self.assertEqual([process.exitcode for process in processes], [0] * workers)
                notices = [results.get(timeout=5) for _ in range(workers)]
            finally:
                for process in processes:
                    if process.exitcode is None:
                        process.terminate()
                        process.join(5)
            self.assertEqual(sum(1 for notice in notices if notice is not None), 1)
            cache = json.loads((Path(temp) / "agent-relay" / "release-notices.json").read_text())
            self.assertEqual(cache["notifiedVersion"], "0.4.0")

    @unittest.skipUnless(os.name == "posix", "cross-process cache locking is a POSIX guarantee")
    def test_scheduled_refresh_cannot_redeliver_a_delivered_version(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            discovery._write_cache({"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                                    "notes": "first", "checkedAt": time.time()})
            context = multiprocessing.get_context("fork")
            start = context.Barrier(2)
            client_results = context.Queue()
            refresh_results = context.Queue()
            with patch.object(discovery, "build_opener",
                              return_value=fake_api([release("0.4.0", notes="refreshed")])):
                client = context.Process(target=_client_worker, args=(start, client_results))
                refresh = context.Process(target=_refresh_worker, args=(start, refresh_results))
                try:
                    client.start()
                    refresh.start()
                    client.join(30)
                    refresh.join(30)
                    self.assertEqual(client.exitcode, 0)
                    self.assertEqual(refresh.exitcode, 0)
                    notice = client_results.get(timeout=5)
                    installed, offer = refresh_results.get(timeout=5)
                finally:
                    for process in (client, refresh):
                        if process.exitcode is None:
                            process.terminate()
                            process.join(5)
            self.assertEqual(notice["version"], "0.4.0")
            self.assertEqual(installed, "0.3.0")
            self.assertEqual(offer["version"], "0.4.0")
            self.assertIsNone(discovery.cached_notice("0.3.0"))
            cache = json.loads((Path(temp) / "agent-relay" / "release-notices.json").read_text())
            self.assertEqual(cache["offer"]["notes"], "refreshed")
            self.assertEqual(cache["notifiedVersion"], "0.4.0")
            self.assertFalse(discovery._notify_once(
                {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                 "notes": "again", "checkedAt": time.time()}))

    @unittest.skipUnless(os.name == "posix", "the notice cache lock is a POSIX flock")
    def test_lock_failure_is_reported_instead_of_claiming_delivery(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            offer = {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                     "notes": "notes", "checkedAt": time.time()}
            discovery._write_cache(offer)
            blocked = patch.object(discovery.fcntl, "flock",
                                   side_effect=BlockingIOError(11, "Resource temporarily unavailable"))
            with patch.object(discovery, "LOCK_TIMEOUT", 0.05), blocked, \
                    patch.object(discovery, "build_opener", return_value=fake_api([release("0.4.0")])):
                self.assertIsNone(discovery.cached_notice("0.3.0"))
                with self.assertRaisesRegex(discovery.DiscoveryError, "lock"):
                    discovery._notify_once(offer)
                error = io.StringIO()
                with patch("sys.stderr", error), patch("sys.stdout", io.StringIO()):
                    self.assertEqual(discovery.main(["--notify"]), 1)
                self.assertIn("Release notice failed", error.getvalue())
                self.assertIn("lock", error.getvalue())
            self.assertEqual(discovery.cached_notice("0.3.0")["version"], "0.4.0")
            self.assertIsNone(discovery.cached_notice("0.3.0"))

    @unittest.skipUnless(os.name == "posix", "the notice cache lock is a POSIX flock")
    def test_untrusted_cache_lock_is_refused_not_followed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            directory = Path(temp) / "agent-relay"
            directory.mkdir(mode=0o700)
            decoy = Path(temp) / "elsewhere"
            decoy.write_text("untouched")
            (directory / "release-notices.lock").symlink_to(decoy)
            offer = {"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                     "notes": "notes", "checkedAt": time.time()}
            with self.assertRaisesRegex(discovery.DiscoveryError, "lock"):
                discovery._write_cache(offer)
            self.assertIsNone(discovery.cached_notice("0.3.0"))
            self.assertEqual(decoy.read_text(), "untouched")
            (directory / "release-notices.lock").unlink()
            (directory / "release-notices.lock").mkdir()
            with self.assertRaisesRegex(discovery.DiscoveryError, "lock"):
                discovery._write_cache(offer)
            self.assertIsNone(discovery.cached_notice("0.3.0"))

    def test_oversize_and_symlinked_cache_files_stay_untrusted(self):
        """Guard: oversize and symlinked cache reads stay untrusted on every write policy."""
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            path = Path(temp) / "agent-relay" / "release-notices.json"
            path.parent.mkdir(mode=0o700)
            payload = json.dumps({"offer": {"version": "0.4.0",
                                            "url": discovery.RELEASE_URL + "0.4.0",
                                            "notes": "x", "checkedAt": time.time()},
                                  "notifiedVersion": None}).encode("utf-8")
            path.write_bytes(b" " * (discovery.MAX_CACHE + 1 - len(payload)) + payload)
            path.chmod(0o600)
            self.assertIsNone(discovery.cached_notice("0.3.0"))
            path.unlink()
            decoy = path.parent / "decoy"
            decoy.write_text("untouched")
            decoy.chmod(0o600)
            path.symlink_to(decoy)
            self.assertIsNone(discovery.cached_notice("0.3.0"))
            self.assertEqual(decoy.read_text(), "untouched")

    def test_cached_notice_reads_only_the_local_cache(self):
        """Guard: a cached notice never fetches release metadata over the network."""
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CACHE_HOME": temp}):
            discovery._write_cache({"version": "0.4.0", "url": discovery.RELEASE_URL + "0.4.0",
                                    "notes": "notes", "checkedAt": time.time()})

            def no_network(*args, **kwargs):
                raise AssertionError("cached_notice must not touch the network")

            with patch.object(discovery, "build_opener", side_effect=no_network), \
                    patch.object(discovery, "fetch_releases", side_effect=no_network), \
                    patch.object(discovery, "discover", side_effect=no_network):
                self.assertEqual(discovery.cached_notice("0.3.0")["version"], "0.4.0")


if __name__ == "__main__":
    unittest.main()
