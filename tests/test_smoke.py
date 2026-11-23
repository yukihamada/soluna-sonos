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
