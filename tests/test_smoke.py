"""Smoke tests: start the dashboard on a free port and hit the HTTP surface.

The speaker is pointed at 127.0.0.1 so status() fails fast and the page still
renders — that is exactly the "Sonos unreachable" path we want to cover.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "soluna_web.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.state = tempfile.mkdtemp(prefix="soluna-test-")
        env = dict(os.environ)
        env.update({
            "SOLUNA_STATE_DIR": cls.state,
            "SOLUNA_SONOS": "127.0.0.1",   # nothing listens here
            "SOLUNA_BRAND": "TestBrand",
            "SOLUNA_MAX_VOL": "35",
            "SOLUNA_WORLD_LABEL": "",
            "SOLUNA_MIX_LABEL": "",
            "SOLUNA_DJ_LABEL": "",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--port", str(cls.port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/", timeout=1) as r:
                    r.read()
                return
            except Exception:
                if cls.proc.poll() is not None:
                    raise RuntimeError(cls.proc.stdout.read())
                time.sleep(0.2)
        cls.proc.kill()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)
        finally:
            if cls.proc.stdout:
                cls.proc.stdout.close()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def test_index_renders_brand(self):
        with self._get("/") as r:
            body = r.read().decode()
        self.assertIn("<title>TestBrand · Sonos</title>", body)
        self.assertNotIn("__BRAND__", body)
        self.assertIn("<html lang=\"en\"", body)
        self.assertIn('data-max="35"', body)  # SOLUNA_MAX_VOL honoured in the UI
        self.assertNotIn("__MAXVOL__", body)

    def test_status_is_json_and_survives_offline_speaker(self):
        with self._get("/api/status") as r:
            data = json.load(r)
        self.assertFalse(data["online"])
        self.assertEqual(data["max_vol"], 35)
        self.assertIn("groovesalad", data["radios"])

    def test_head_index(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers.get("Content-Type", ""))
            self.assertGreater(int(r.headers.get("Content-Length", 0)), 1000)

    def test_head_status_matches_get_length(self):
        with self._get("/api/status") as r:
            get_body = r.read()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/status", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(int(r.headers["Content-Length"]), len(get_body))

    def test_offline_status_has_all_keys(self):
        # the page reads mix/hue/yt unconditionally
        with self._get("/api/status") as r:
            data = json.load(r)
        for key in ("online", "max_vol", "mix", "mix_running", "hue",
                    "yt", "radios", "world_running", "dj_running"):
            self.assertIn(key, data)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/does-not-exist")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_art_proxy_rejects_foreign_host(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/art?u=http://evil.example.com/a.png")
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_art_proxy_rejects_lookalike_domain(self):
        # evilsomafm.com must not pass the allow-list via a suffix match
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/art?u=http://evilsomafm.com/a.png")
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_post_rejects_non_json_content_type(self):
        # a cross-site HTML form can only send these types without preflight
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action",
            data=b'{"cmd":"volume","value":100}',
            headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_post_rejects_cross_origin(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action",
            data=b'{"cmd":"volume","value":100}',
            headers={"Content-Type": "application/json",
                     "Origin": "http://evil.example.com"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_post_accepts_same_origin(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action",
            data=b'{"cmd":"bogus"}',
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{self.port}"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        self.assertIn("error", data)  # reaches the handler, not blocked by CSRF

    # --- regressions found in review round 2 -----------------------------

    def test_offline_status_keeps_optional_keys(self):
        # The page reads mix/hue/yt/radios unconditionally; a missing key threw
        # a TypeError in the browser and blanked the page.
        with self._get("/api/status") as r:
            d = json.loads(r.read())
        self.assertFalse(d["online"])
        for k in ("mix", "mix_running", "radios", "yt", "hue",
                  "world_running", "dj_running", "max_vol"):
            self.assertIn(k, d, f"offline status missing {k}")

    def test_mix_running_true_when_daemon_answers(self):
        # Detection used to be launchd-only, so the mixer console could never
        # appear on Linux even with the daemon running. It must also accept an
        # HTTP answer on SOLUNA_MIX_PORT.
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_GET(self):
                body = b'{"decks":{}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mix_port = srv.server_address[1]
        env = dict(os.environ)
        env.update({
            "SOLUNA_STATE_DIR": tempfile.mkdtemp(prefix="soluna-mix-"),
            "SOLUNA_SONOS": "127.0.0.1",
            "SOLUNA_MIX_PORT": str(mix_port),
            "SOLUNA_MIX_LABEL": "",
            "SOLUNA_WORLD_LABEL": "",
            "SOLUNA_DJ_LABEL": "",
        })
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--port", str(port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                        d = json.loads(r.read())
                    break
                except Exception:
                    if proc.poll() is not None:
                        raise RuntimeError(proc.stdout.read())
                    time.sleep(0.2)
            else:
                self.fail("server did not start")
            self.assertTrue(d["mix_running"],
                            "mixer daemon answering over HTTP must count as running")
            self.assertEqual(d["mix"], {"decks": {}})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
            srv.shutdown()
            srv.server_close()

    def test_page_has_no_external_resources(self):
        with self._get("/") as r:
            body = r.read().decode()
        import re
        self.assertEqual(re.findall(r'(?:src|href)="https?://', body), [],
                         "page must not load third-party scripts, fonts or images")

    def test_backoff_counts_speaker_outage(self):
        # A speaker outage returns 200 with online:false, so the old code reset
        # pollFail every tick and hammered the dead speaker every 2s.
        with self._get("/") as r:
            body = r.read().decode()
        self.assertIn("pollFail=s.online?0:pollFail+1", body)
        self.assertIn("Math.pow(1.5,pollFail)", body)

    def test_unknown_action_is_reported(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action",
            data=json.dumps({"cmd": "bogus"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
