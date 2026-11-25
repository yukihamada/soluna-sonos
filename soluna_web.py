#!/usr/bin/env python3
"""Web dashboard for a Sonos speaker (plus optional extras).

Serves a single page that lets you control a Sonos speaker from any browser on
your network: play/pause, radio stations, line-in, volume/bass/treble/loudness,
mute, play mode, crossfade, and a live view of the current source and health.

Optional integrations (each one degrades gracefully when absent):

  * a local "WORLD" stream daemon (a second HTTP server on WORLD_PORT) that can
    take the speaker over; a hold-file protocol keeps it from stealing playback
  * a local 4-deck mixer daemon (MIX_PORT) exposing /mix/* endpoints
  * a DDJ-FLX4 MIDI bridge (only its launchd label is probed, never driven)
  * Philips Hue lights
  * yt-dlp + ffmpeg for YouTube audio

Everything site-specific is configuration, not code: see README.md.

Run:
  soluna-web                          # http://127.0.0.1:8100/
  soluna-web --host 0.0.0.0           # expose on the LAN (no auth — be careful)
  soluna-web --sonos 192.168.1.50     # or omit and let SSDP discovery find it
"""
import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    import soco
    from soco import SoCo
    import soco.config as _soco_config
    _soco_config.REQUEST_TIMEOUT = 3
except ImportError:
    sys.exit("soco is required: pip install -r requirements.txt")


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _state_dir():
    """Directory for hold files / caches. XDG-aware, overridable."""
    d = _env("SOLUNA_STATE_DIR")
    if d:
        return Path(d).expanduser()
    base = _env("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "soluna-web"


STATE_DIR = _state_dir()
STATE_DIR.mkdir(parents=True, exist_ok=True)

# --- configuration (env var -> default) -------------------------------------
# Sonos speaker. Empty means "discover over SSDP on first use".
SONOS_IP = _env("SOLUNA_SONOS")
WEB_PORT = int(_env("SOLUNA_WEB_PORT", "8100"))
WORLD_PORT = int(_env("SOLUNA_WORLD_PORT", "8099"))
MIX_PORT = int(_env("SOLUNA_MIX_PORT", "8110"))
MAX_VOL = int(_env("SOLUNA_MAX_VOL", "40"))  # hard ceiling so a mis-drag can't blast the room
BRAND = _env("SOLUNA_BRAND", "SOLUNA")

# launchd labels of the optional helper daemons. Empty = "not installed", and
# the UI simply reports the feature as stopped.
WORLD_LABEL = _env("SOLUNA_WORLD_LABEL", "")
MIX_LABEL = _env("SOLUNA_MIX_LABEL", "")
DJ_LABEL = _env("SOLUNA_DJ_LABEL", "")

# Hold-file protocol shared with the WORLD daemon (all inside STATE_DIR).
# The prefix lets an existing daemon keep its filenames, e.g.
#   SOLUNA_STATE_DIR=/tmp SOLUNA_WORLD_PREFIX=soluna-world-
# reproduces /tmp/soluna-world-hold, /tmp/soluna-world-pause, ...
WORLD_PREFIX = _env("SOLUNA_WORLD_PREFIX", "world-")
HOLD = str(STATE_DIR / f"{WORLD_PREFIX}hold")
PAUSE = str(STATE_DIR / f"{WORLD_PREFIX}pause")
NOW = str(STATE_DIR / f"{WORLD_PREFIX}now")
WORLD_CURRENT = str(STATE_DIR / f"{WORLD_PREFIX}current.json")
YT_CACHE = Path(_env("SOLUNA_YT_CACHE", str(STATE_DIR / "yt")))

# Philips Hue pairing config: {"bridge": "ip", "username": "key", "lights": ["1"]}
HUE_CFG_PATH = Path(_env("SOLUNA_HUE_CONFIG", str(Path.home() / ".config" / "soluna-web" / "hue.json"))).expanduser()

RADIOS = {
    "groovesalad": ("Groove Salad", "http://ice1.somafm.com/groovesalad-128-mp3", "https://somafm.com/logos/256/groovesalad256.png"),
    "dronezone": ("Drone Zone", "http://ice1.somafm.com/dronezone-128-mp3", "https://somafm.com/logos/256/dronezone256.png"),
    "secretagent": ("Secret Agent", "http://ice1.somafm.com/secretagent-128-mp3", "https://somafm.com/logos/256/secretagent256.png"),
    "lush": ("Lush", "http://ice1.somafm.com/lush-128-mp3", "https://somafm.com/logos/256/lush256.png"),
}

HUE = {"bridge": None, "username": None, "lights": []}

# Cap on proxied artwork so one huge image cannot exhaust memory.
ART_MAX_BYTES = int(_env("SOLUNA_ART_MAX_BYTES", str(3 * 1024 * 1024)))


def _hue_load():
    try:
        c = json.loads(HUE_CFG_PATH.read_text())
        HUE["bridge"] = c.get("bridge")
        HUE["username"] = c.get("username")
        HUE["lights"] = [str(x) for x in (c.get("lights") or [])]
    except Exception:
        pass


_hue_load()
_HUE_CACHE = {"t": 0.0, "v": None}


def _hue_url(path):
    if not HUE["bridge"] or not HUE["username"]:
        return None
    return f"http://{HUE['bridge']}/api/{HUE['username']}{path}"


def _hue_get(path, timeout=2.0):
    u = _hue_url(path)
    if not u:
        return None
    try:
        with urllib.request.urlopen(u, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _hue_put(light, state, timeout=2.5):
    u = _hue_url(f"/lights/{light}/state")
    if not u:
        return {"error": "Hue is not configured"}
    try:
        req = urllib.request.Request(
            u, data=json.dumps(state).encode(),
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def hue_status():
    if not HUE["bridge"] or not HUE["username"] or not HUE["lights"]:
        return {"configured": False}
    now = time.time()
    if _HUE_CACHE["v"] and now - _HUE_CACHE["t"] < 1.5:
        return _HUE_CACHE["v"]
    states = []
    for lid in HUE["lights"]:
        d = _hue_get(f"/lights/{lid}")
        if d and "state" in d:
            states.append(d["state"])
    if not states:
        res = {"configured": True, "online": False, "bridge": HUE["bridge"],
               "lights": HUE["lights"], "reachable_count": 0}
    else:
        # Only lights that actually answer count as reachable; a paired but
        # unplugged lamp must not be reported as connected.
        reach = [s for s in states if s.get("reachable")]
        src = (reach or states)[0]
        on_n = sum(1 for s in reach if s.get("on"))
        res = {"configured": True, "online": True, "bridge": HUE["bridge"],
               "lights": HUE["lights"], "total": len(HUE["lights"]),
               "reachable_count": len(reach),
               "on": on_n > 0 if reach else bool(src.get("on")),
               "bri": src.get("bri"), "ct": src.get("ct"), "sat": src.get("sat"),
               "reachable": len(reach) > 0}
    _HUE_CACHE["t"] = now
    _HUE_CACHE["v"] = res
    return res


def hue_apply(payload):
    if not HUE["lights"]:
        return {"error": "No Hue lights are configured"}
    state = {}
    if "on" in payload:
        state["on"] = bool(payload["on"])
    if "bri" in payload:
        state["bri"] = max(1, min(254, int(payload["bri"])))
    if "ct" in payload:
        state["ct"] = max(153, min(500, int(payload["ct"])))
    if "sat" in payload:
        state["sat"] = max(0, min(254, int(payload["sat"])))
    if not state:
        return {"error": "Nothing to change"}
    ok = 0
    unreachable = []
    for lid in HUE["lights"]:
        # Hue queues commands for unplugged lamps and still answers "success",
        # so check reachability separately instead of trusting the PUT reply.
        info = _hue_get(f"/lights/{lid}", timeout=1.5)
        if not (info and info.get("state", {}).get("reachable")):
            unreachable.append(lid)
        r = _hue_put(lid, state)
        if isinstance(r, list) and any("success" in x for x in r):
            ok += 1
    _HUE_CACHE["v"] = None  # force re-read next status
    res = {"ok": ok > 0, "applied": ok, "total": len(HUE["lights"])}
    if unreachable:
        res["unreachable"] = unreachable
        res["note"] = f"{len(unreachable)} light(s) did not respond; change is pending"
    return res

YT_CACHE.mkdir(parents=True, exist_ok=True)
YT_STATE = {"id": None, "title": "", "status": "idle", "url": "", "error": ""}
YT_LOCK = threading.Lock()


def _find_ytdlp():
    """Locate yt-dlp: explicit env var, then PATH."""
    cands = []
    if _env("SOLUNA_YTDLP"):
        cands.append(_env("SOLUNA_YTDLP"))
    cands.append(shutil.which("yt-dlp"))
    for c in cands:
        if not c:
            continue
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=8, check=True)
            return c
        except Exception:
            continue
    return None


YTDLP = _find_ytdlp()
# Directory holding ffmpeg, needed by yt-dlp for mp3 extraction.
FFMPEG_DIR = _env("SOLUNA_FFMPEG_DIR") or (
    str(Path(shutil.which("ffmpeg")).parent) if shutil.which("ffmpeg") else None)


def _lan_ip():
    """Best-effort LAN address, used to build URLs the speaker can fetch."""
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


LAN_IP = _env("SOLUNA_PUBLIC_HOST") or _lan_ip()


def _qr_svg(text):
    """Render `text` as a QR code SVG, or None when qrencode is unavailable.

    The QR is generated on the server so the phone never needs an external
    image host, and the URL always matches the address this process is
    actually reachable on.
    """
    exe = shutil.which("qrencode")
    if not exe:
        # launchd starts us with a minimal PATH, so check the usual Homebrew
        # locations before giving up.
        for cand in ("/opt/homebrew/bin/qrencode", "/usr/local/bin/qrencode",
                     "/usr/bin/qrencode"):
            if os.access(cand, os.X_OK):
                exe = cand
                break
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-t", "SVG", "-s", "6", "-m", "2", "--foreground=000000",
             "--background=FFFFFF", "-o", "-", text],
            capture_output=True, timeout=5, check=True)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def _phone_url():
    return f"http://{LAN_IP}:{WEB_PORT}/"


def _mix_post(path, payload, timeout=4.0):
    """POST JSON to the local mixer control API."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{MIX_PORT}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def _mix_status():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{MIX_PORT}/mix/status", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _mix_clock():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{MIX_PORT}/mix/clock", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _mix_level():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{MIX_PORT}/mix/level", timeout=1.5) as r:
            return json.load(r)
    except Exception:
        return None


def _rel_seconds(pos):
    """'0:07:01' -> 421.0. Returns None for anything unparseable."""
    try:
        h, m, sec = (pos or "").split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except Exception:
        return None


# The speaker buffers several seconds before it makes a sound. Measured
# against the mixer's first client connection it is steady at ~5.1s here, but
# it differs per model and network, so it is measured, not assumed.
SPEAKER_LAG_DEFAULT = 5.1


def mix_sync_info():
    """Everything a phone needs to start in step with the speaker.

    Returns the wall-clock point in the shared stream the speaker is playing
    right now, plus the offset to apply. The phone opens /mix/sync.mp3?at=...
    so it hears the same second as the room instead of running ahead by the
    speaker's buffer.
    """
    clk = _mix_clock()
    if not clk:
        return {"ok": False, "error": "mixer is not running"}
    try:
        s = sonos()
        track = s.get_current_track_info()
        uri = track.get("uri") or ""
        if f":{MIX_PORT}/mix.mp3" not in uri:
            return {"ok": False, "error": "Sonos is not playing the mix",
                    "source_uri": uri}
        rel = _rel_seconds(track.get("position"))
        if rel is None:
            return {"ok": False, "error": "speaker position unavailable"}
        now = time.time()
        fc = clk.get("first_client")
        # first_client is when the speaker opened the stream; RelTime is how
        # long it has been playing. The gap between them is its buffer.
        lag = SPEAKER_LAG_DEFAULT
        if fc:
            measured = (now - fc) - rel
            if 0.0 <= measured <= 30.0:
                lag = measured
        at = now - lag
        oldest = clk.get("oldest")
        if oldest and at < oldest:
            # We no longer hold audio that old; start at the oldest we have and
            # report the residual offset rather than pretending it is exact.
            at = oldest
            residual = (now - lag) - oldest
        else:
            residual = 0.0
        return {
            "ok": True,
            "at": round(at, 3),
            "lag": round(lag, 2),
            "residual": round(residual, 2),
            "now": round(now, 3),
            "history_seconds": clk.get("history_seconds"),
            "url": f"/mix/sync.mp3?at={at:.3f}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def mixer_running(mix):
    """True if the mixer daemon is usable, given an already-fetched status.

    Two independent signals, because neither works everywhere: the HTTP
    endpoint answers on any OS, while the launchd label only exists on macOS.
    Either one is enough to show the console. `mix` is passed in so the status
    poll does not hit the daemon twice per tick.
    """
    return mix is not None or daemon_running(MIX_LABEL)


def deck_load(data):
    """Load a source into a mixer deck (youtube/radio/file)."""
    deck = (data.get("deck") or "").upper()
    if deck not in ("A", "B", "C", "D"):
        return {"error": "deck must be A/B/C/D"}
    kind = data.get("kind")
    if kind == "radio":
        key = data.get("key")
        if key not in RADIOS:
            return {"error": "unknown radio"}
        name, uri, _art = RADIOS[key]
        r = _mix_post("/mix/deck", {"deck": deck, "action": "load",
                                    "src": uri, "title": name, "live": True})
        if isinstance(r, dict) and r.get("ok"):
            _mix_post("/mix/deck", {"deck": deck, "action": "play"})
        return r
    if kind == "file":
        path = data.get("path", "")
        if not path or not os.path.exists(path):
            return {"error": "File not found"}
        r = _mix_post("/mix/deck", {"deck": deck, "action": "load",
                                    "src": path, "title": data.get("title") or os.path.basename(path),
                                    "live": False})
        if isinstance(r, dict) and r.get("ok"):
            _mix_post("/mix/deck", {"deck": deck, "action": "play"})
        return r
    if kind == "youtube":
        url = (data.get("url") or "").strip()
        if not url:
            return {"error": "No YouTube URL given"}

        def _bg():
            try:
                vid, title = yt_download(url)
                _mix_post("/mix/deck", {"deck": deck, "action": "load",
                                        "src": str(_yt_file(vid)), "title": title or vid,
                                        "live": False})
                _mix_post("/mix/deck", {"deck": deck, "action": "play"})
            except Exception:
                pass
        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "note": "Loading YouTube into DECK " + deck + "…"}
    return {"error": "Invalid kind"}


def radio_now(key):
    """Return (title, artist) of the currently playing SomaFM song, or ('','')."""
    try:
        url = f"https://somafm.com/songs/{key}.json"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        songs = data.get("songs") or []
        if songs:
            return songs[0].get("title", ""), songs[0].get("artist", "")
    except Exception:
        pass
    return "", ""


# Cache radio now-playing so status() never blocks on the external SomaFM API.
_RADIO_CACHE = {}   # key -> (title, artist, fetched_at)
_RADIO_TTL = 12.0
_RADIO_INFLIGHT = set()


def radio_now_cached(key):
    """Non-blocking: return cached (title, artist); refresh in background if stale."""
    now = time.time()
    hit = _RADIO_CACHE.get(key)
    if hit and now - hit[2] < _RADIO_TTL:
        return hit[0], hit[1]
    if key not in _RADIO_INFLIGHT:
        _RADIO_INFLIGHT.add(key)

        def _bg():
            try:
                t, a = radio_now(key)
                if t:
                    _RADIO_CACHE[key] = (t, a, time.time())
            finally:
                _RADIO_INFLIGHT.discard(key)

        _BG_POOL.submit(_bg)
    return (hit[0], hit[1]) if hit else ("", "")


def _yt_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _yt_meta(url):
    """Return (id, title) via yt-dlp, or (None, '')."""
    if not YTDLP:
        return None, ""
    try:
        out = subprocess.run(
            [YTDLP, "--no-playlist", "--print", "%(id)s\t%(title)s", "--skip-download", url],
            capture_output=True, text=True, timeout=25)
        line = (out.stdout or "").strip().splitlines()
        if line:
            parts = line[0].split("\t", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
    except Exception:
        pass
    return _yt_id(url), ""


def _yt_file(vid):
    return YT_CACHE / f"{vid}.mp3"


def yt_download(url):
    """Download audio to mp3 cache. Returns (vid, title) or raises."""
    if not YTDLP:
        raise RuntimeError("yt-dlp not found (install it or set SOLUNA_YTDLP)")
    vid, title = _yt_meta(url)
    if not vid:
        raise RuntimeError("Could not parse that YouTube URL")
    dest = _yt_file(vid)
    if not dest.exists() or dest.stat().st_size < 1024:
        outtmpl = str(YT_CACHE / f"{vid}.%(ext)s")
        cmd = [YTDLP, "--no-playlist"]
        if FFMPEG_DIR:
            cmd += ["--ffmpeg-location", FFMPEG_DIR]
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                "-o", outtmpl, url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if out.returncode != 0 or not dest.exists():
            raise RuntimeError((out.stderr or "download failed").strip().splitlines()[-1][:160])
    return vid, title


def yt_status():
    with YT_LOCK:
        return dict(YT_STATE)


def didl(uri, title="stream", cls="object.item.audioItem.audioBroadcast", art=""):
    artxml = f"<upnp:albumArtURI>{html.escape(art)}</upnp:albumArtURI>" if art else ""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="-1" parentID="0" restricted="1">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        f"<upnp:class>{cls}</upnp:class>"
        f"{artxml}"
        f'<res protocolInfo="http-get:*:audio/mpeg:*">{html.escape(uri)}</res>'
        "</item></DIDL-Lite>"
    )


def sonos():
    """Return a SoCo handle for the configured speaker (or discover one).

    The request timeout is set once on soco.config at import time; we do not
    touch socket.setdefaulttimeout() here because that would change the
    default for every socket in the process.
    """
    if SONOS_IP:
        return SoCo(SONOS_IP)
    dev = soco.discovery.any_soco()
    if dev is None:
        raise RuntimeError("No Sonos found (set SOLUNA_SONOS or pass --sonos)")
    return dev


def daemon_running(label):
    """True if a helper daemon with this launchd label has a live PID.

    Only meaningful on macOS; on other platforms (or when no label is
    configured) this reports False so the UI shows the feature as stopped.
    """
    if not label:
        return False
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True, timeout=2)
        for line in out.splitlines():
            if line.endswith(label):
                parts = line.split()
                return parts[0] != "-"  # has a PID
    except Exception:
        pass
    return False


def _mix_keys():
    """mix/mix_running from a single daemon probe."""
    mix = _mix_status()
    return {"mix": mix, "mix_running": mixer_running(mix)}


def _offline_status(error):
    """The full status shape for an unreachable speaker.

    The page reads mix/hue/yt unconditionally, so these keys must be present
    even when the speaker is down.
    """
    return {
        "online": False, "error": error, "max_vol": MAX_VOL,
        "source": None, "radio_key": None,
        "world_hold": os.path.exists(HOLD),
        "dj_running": daemon_running(DJ_LABEL),
        "world_running": daemon_running(WORLD_LABEL),
        **_mix_keys(),
        "radios": {k: v[0] for k, v in RADIOS.items()},
        "yt": yt_status(),
        "hue": hue_status(),
    }


def status():
    """Snapshot of everything the page renders.

    Never raises for an unreachable speaker: returns {"online": False, ...}
    with the last-known values so the UI can keep showing state.
    """
    try:
        s = sonos()
        info = s.get_speaker_info()
        track = s.get_current_track_info()
        uri = track.get("uri") or ""
        if uri.startswith("x-rincon-stream:"):
            source = "linein"
        elif f":{WORLD_PORT}/track/" in uri:
            source = "world"
        elif f":{MIX_PORT}/mix.mp3" in uri:
            source = "mix"
        elif f":{WEB_PORT}/yt/" in uri:
            source = "youtube"
        elif uri.startswith("http"):
            source = "radio"
        else:
            source = "other"
        radio_key = None
        for k, (_, ruri, _art) in RADIOS.items():
            if ruri and ruri in uri:
                radio_key = k
                break
        # For WORLD, surface the track name the sync daemon publishes.
        title = track.get("title", "")
        artist = track.get("artist", "")
        if source == "world":
            try:
                with open(WORLD_CURRENT) as f:
                    wc = json.load(f)
                if wc.get("name"):
                    title = wc["name"]
            except Exception:
                pass
        elif source == "radio" and radio_key:
            rt, ra = radio_now_cached(radio_key)
            if rt:
                title, artist = rt, ra
        artwork = track.get("album_art", "") or ""
        if source == "radio" and radio_key:
            artwork = RADIOS[radio_key][2]
        elif source == "world":
            try:
                with open(WORLD_CURRENT) as f:
                    wc = json.load(f)
                wid = wc.get("id")
                if wid:
                    artwork = f"http://{LAN_IP}:{WORLD_PORT}/cover/{wid}.png"
            except Exception:
                pass
        if source == "youtube":
            ys = yt_status()
            if ys.get("title"):
                title = ys["title"]
            vid = None
            mm = re.search(rf":{WEB_PORT}/yt/([A-Za-z0-9_-]+)\.mp3", uri)
            if mm:
                vid = mm.group(1)
                artwork = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        rc = s.renderingControl
        return {
            "online": True,
            "name": s.player_name,
            "ip": s.ip_address,
            "model": info.get("model_name"),
            "state": s.get_current_transport_info()["current_transport_state"],
            "volume": int(s.volume),
            "mute": bool(s.mute),
            "bass": int(rc.GetBass([("InstanceID", 0), ("Channel", "Master")])["CurrentBass"]),
            "treble": int(rc.GetTreble([("InstanceID", 0), ("Channel", "Master")])["CurrentTreble"]),
            "loudness": int(rc.GetLoudness([("InstanceID", 0), ("Channel", "Master")])["CurrentLoudness"]),
            "title": title,
            "artist": artist,
            "artwork": artwork,
            "position": track.get("position", ""),
            "duration": track.get("duration", ""),
            "seekable": source in ("youtube", "radio"),
            "play_mode": s.play_mode,
            "crossfade": bool(s.cross_fade),
            "source": source,
            "radio_key": radio_key,
            "world_hold": os.path.exists(HOLD),
            "max_vol": MAX_VOL,
            "dj_running": daemon_running(DJ_LABEL),
            "world_running": daemon_running(WORLD_LABEL),
            **_mix_keys(),
            "radios": {k: v[0] for k, v in RADIOS.items()},
            "yt": yt_status(),
            "hue": hue_status(),
        }
    except Exception as e:
        # A source transition can make one soco call throw. Keep the UI on the
        # last known-good state rather than flashing "offline" mid-switch.
        if _LAST_GOOD["v"]:
            d = dict(_LAST_GOOD["v"])
            d["stale"] = True
            return d
        return _offline_status(str(e))


def set_uri(uri, title, cls="object.item.audioItem.audioBroadcast", art=""):
    s = sonos()
    s.avTransport.SetAVTransportURI([("InstanceID", 0), ("CurrentURI", uri),
                                     ("CurrentURIMetaData", didl(uri, title, cls, art))])
    s.play()


def _is_world(s, track):
    return f":{WORLD_PORT}/track/" in (track.get("uri") or "")


def _mix_pause_all():
    """Pause every deck on the mixer.

    The speaker keeps pulling /mix.mp3 while any deck is playing, so pausing
    only the speaker makes it reconnect and resume a moment later. Silencing
    the mix first is what actually stops the sound.
    """
    out = {}
    for d in ("A", "B", "C", "D"):
        try:
            out[d] = _mix_post("/mix/deck", {"deck": d, "action": "pause"}, timeout=2.0)
        except Exception as e:
            out[d] = {"error": str(e)}
    return out


def yt_play(url):
    """Kick off a background download, then point Sonos at the local relay."""
    def _bg():
        with YT_LOCK:
            YT_STATE.update({"status": "downloading", "title": YT_STATE.get("title") or "YouTube", "error": ""})
        try:
            vid, title = yt_download(url)
            relay = f"http://{LAN_IP}:{WEB_PORT}/yt/{vid}.mp3"
            with YT_LOCK:
                YT_STATE.update({"id": vid, "title": title, "status": "playing", "url": relay, "error": ""})
            # Hold WORLD so the sync daemon doesn't steal the speaker back.
            with open(HOLD, "w") as f:
                f.write("youtube")
            if os.path.exists(PAUSE):
                os.remove(PAUSE)
            set_uri(relay, title or "YouTube", "object.item.audioItem.musicTrack")
        except Exception as e:
            with YT_LOCK:
                YT_STATE.update({"status": "error", "error": str(e)})
    threading.Thread(target=_bg, daemon=True).start()
    with YT_LOCK:
        YT_STATE.update({"status": "downloading", "title": "YouTube", "error": ""})
    return {"ok": True, "note": "Loading YouTube…"}


def action(cmd, data):
    """Execute one UI command and return {"ok": True, ...} or {"error": ...}.

    `cmd` is the command name, `data` the full request body (it carries the
    command's arguments, e.g. {"cmd": "volume", "value": 30}).
    """
    s = sonos()
    if cmd == "play":
        if os.path.exists(PAUSE):
            os.remove(PAUSE)
        s.play()
    elif cmd == "pause":
        ti = s.get_current_track_info()
        if _is_world(s, ti):
            with open(PAUSE, "w") as f:
                f.write("pause")
        s.pause()
    elif cmd == "toggle":
        st = s.get_current_transport_info()["current_transport_state"]
        # TRANSITIONING/BUFFERING are "on the way to playing"; treating them as
        # not-playing made the pause button start playback instead of stopping.
        if st in ("PLAYING", "TRANSITIONING", "BUFFERING"):
            ti = s.get_current_track_info()
            if _is_world(s, ti):
                with open(PAUSE, "w") as f:
                    f.write("pause")
            # Stop the source of the audio as well. Pausing the speaker alone
            # left the mix running, and the speaker resumed on its own.
            mix = _mix_pause_all()
            s.pause()
            return {"ok": True, "note": "停止しました", "mix": mix}
        else:
            if os.path.exists(PAUSE):
                os.remove(PAUSE)
            s.play()
    elif cmd == "next":
        try:
            s.next()
        except Exception:
            return {"ok": True, "note": "This source does not support skipping"}
    elif cmd == "previous":
        try:
            s.previous()
        except Exception:
            return {"ok": True, "note": "This source does not support skipping"}
    elif cmd == "volume":
        s.volume = max(0, min(MAX_VOL, int(data.get("value", 50))))
    elif cmd == "bass":
        v = max(-10, min(10, int(data.get("value", 0))))
        s.renderingControl.SetBass([("InstanceID", 0), ("Channel", "Master"), ("DesiredBass", v)])
    elif cmd == "treble":
        v = max(-10, min(10, int(data.get("value", 0))))
        s.renderingControl.SetTreble([("InstanceID", 0), ("Channel", "Master"), ("DesiredTreble", v)])
    elif cmd == "loudness":
        if "value" in data:
            want = bool(data.get("value"))
        else:
            cur = int(s.renderingControl.GetLoudness([("InstanceID", 0), ("Channel", "Master")])["CurrentLoudness"])
            want = not bool(cur)
        s.renderingControl.SetLoudness([("InstanceID", 0), ("Channel", "Master"),
                                        ("DesiredLoudness", want)])
    elif cmd == "mute":
        s.mute = bool(data.get("value", not s.mute))
    elif cmd == "radio":
        key = data.get("key")
        if key not in RADIOS:
            return {"error": "unknown radio"}
        if os.path.exists(PAUSE):
            os.remove(PAUSE)
        with open(HOLD, "w") as f:
            f.write("radio")
        name, uri, art = RADIOS[key]
        set_uri(uri, name, art=art)
    elif cmd == "world":
        if os.path.exists(HOLD):
            os.remove(HOLD)
        if os.path.exists(PAUSE):
            os.remove(PAUSE)
        # Nudge the sync daemon to switch to WORLD immediately.
        try:
            with open(NOW, "w") as f:
                f.write("now")
        except Exception:
            pass
    elif cmd == "linein":
        if os.path.exists(PAUSE):
            os.remove(PAUSE)
        with open(HOLD, "w") as f:
            f.write("linein")
        set_uri("x-rincon-stream:" + s.uid, "Line-in", "object.item.audioItem")
    elif cmd == "seek":
        secs = int(data.get("value", 0))
        secs = max(0, secs)
        h, rem = divmod(secs, 3600)
        m, sec = divmod(rem, 60)
        s.seek(f"{h}:{m:02d}:{sec:02d}")
    elif cmd == "playmode":
        mode = data.get("mode", "NORMAL")
        if mode not in ("NORMAL", "SHUFFLE", "SHUFFLE_NOREPEAT", "REPEAT_ALL", "REPEAT_ONE"):
            return {"error": "unknown mode"}
        s.play_mode = mode
    elif cmd == "crossfade":
        if "value" in data:
            want = bool(data.get("value"))
        else:
            want = not bool(s.cross_fade)
        s.cross_fade = want
    elif cmd == "youtube":
        url = (data.get("url") or "").strip()
        if not url:
            return {"error": "No YouTube URL given"}
        return yt_play(url)
    elif cmd == "mix":
        # Switch Sonos to the 2-deck live mix stream.
        if os.path.exists(PAUSE):
            os.remove(PAUSE)
        with open(HOLD, "w") as f:
            f.write("mix")
        set_uri(f"http://{LAN_IP}:{MIX_PORT}/mix.mp3", f"{BRAND} Mix",
                "object.item.audioItem.audioBroadcast")
    elif cmd == "stopall":
        # One button that really silences the room: decks, then the speaker.
        mix = _mix_pause_all()
        try:
            with open(PAUSE, "w") as f:
                f.write("pause")
        except Exception:
            pass
        try:
            s.pause()
        except Exception as e:
            return {"ok": False, "error": str(e), "mix": mix}
        return {"ok": True, "note": "停止しました", "mix": mix}
    elif cmd == "deck":
        return deck_load(data)
    elif cmd == "deckctl":
        deck = (data.get("deck") or "").upper()
        act = data.get("act")
        if deck not in ("A", "B", "C", "D") or act not in ("play", "pause", "seek", "volume", "tempo"):
            return {"error": "Invalid deck/act"}
        payload = {"deck": deck, "action": act}
        if act in ("seek", "volume", "tempo"):
            payload["value"] = data.get("value", 0)
        return _mix_post("/mix/deck", payload)
    elif cmd == "pfl":
        return _mix_post("/mix/pfl", {"deck": (data.get("deck") or "").upper(),
                                      **({"value": bool(data["value"])} if "value" in data else {})})
    elif cmd == "bank":
        return _mix_post("/mix/bank", {} if "value" not in data else {"value": int(data["value"])})
    elif cmd == "crossfader":
        return _mix_post("/mix/crossfader", {"value": float(data.get("value", 0.5))})
    elif cmd == "mixauto":
        return _mix_post("/mix/auto", {"value": bool(data.get("value", True))})
    elif cmd == "cue":
        return _mix_post("/mix/deck", {"deck": (data.get("deck") or "").upper(),
                                       "action": "cue", "slot": data.get("slot"),
                                       "op": data.get("op", "goto"),
                                       "value": data.get("value")})
    elif cmd == "mixmaster":
        return _mix_post("/mix/master", {"value": float(data.get("value", 1.0))})
    elif cmd == "pad":
        deck = (data.get("deck") or "").upper()
        if deck not in ("A", "B", "C", "D"):
            return {"error": "Invalid deck"}
        op = data.get("op", "hit")
        payload = {"deck": deck, "slot": int(data.get("slot", 0)), "op": op}
        if op == "load":
            payload["src"] = data.get("src") or ""
        elif op == "hit":
            payload["gain"] = float(data.get("gain", 1.0))
        return _mix_post("/mix/pad", payload)
    elif cmd == "loop":
        deck = (data.get("deck") or "").upper()
        if deck not in ("A", "B", "C", "D"):
            return {"error": "Invalid deck"}
        return _mix_post("/mix/loop", {"deck": deck, "op": data.get("op", "play")})
    elif cmd == "hue":
        return hue_apply(data)
    else:
        return {"error": f"unknown command {cmd}"}
    return {"ok": True}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # surface the 3xx instead of following it


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wave(self):
        from urllib.parse import urlparse as _up, parse_qs
        q = parse_qs(_up(self.path).query)
        deck = (q.get("deck", ["A"])[0] or "A").upper()
        n = q.get("n", ["240"])[0]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{MIX_PORT}/mix/wave?deck={deck}&n={n}",
                timeout=45) as r:
                return json.load(r)
        except Exception as e:
            return {"deck": deck, "peaks": None, "error": str(e)}

    def _serve_yt(self, vid):
        f = _yt_file(vid)
        if not f.exists():
            self.send_error(404)
            return
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mm:
                if mm.group(1):
                    start = int(mm.group(1))
                if mm.group(2):
                    end = int(mm.group(2))
        start = max(0, min(start, size - 1))
        end = max(start, min(end, size - 1))
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def _proxy_json(self, path):
        """Pass a mixer JSON endpoint through this origin."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{MIX_PORT}{path}", timeout=2) as r:
                body = r.read()
        except Exception as e:
            self._json({"error": str(e)}, code=502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_sync(self):
        """Stream the mixer's delayed audio through this origin.

        The phone only ever talks to this server, so it needs no knowledge of
        the mixer port and the mixer can stay loopback-only.
        """
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        url = f"http://127.0.0.1:{MIX_PORT}/mix/sync.mp3"
        if qs:
            url += "?" + qs
        try:
            upstream = urllib.request.urlopen(url, timeout=15)
        except Exception as e:
            self.send_error(502, str(e))
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for h in ("X-Stream-Start", "X-Stream-Now"):
            v = upstream.headers.get(h)
            if v:
                self.send_header(h, v)
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                data = upstream.read(4096)
                if not data:
                    break
                self.wfile.write(data)
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _serve_art(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        u = (q.get("u") or [""])[0]
        try:
            p = urlparse(u)
        except Exception:
            self.send_error(400)
            return
        if p.scheme not in ("http", "https"):
            self.send_error(400)
            return
        host = (p.hostname or "").lower()

        def _host_in(domain):
            # "somafm.com" matches somafm.com and a.somafm.com, never evilsomafm.com
            return host == domain or host.endswith("." + domain)

        allowed = (
            _host_in("somafm.com")
            or _host_in("ytimg.com")
            or _host_in("ggpht.com")
            or _host_in("googleusercontent.com")
            or host == LAN_IP
            or (bool(SONOS_IP) and host == SONOS_IP)
        )
        if not allowed:
            self.send_error(403)
            return
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "soluna-web/1.0"})
            # Do not follow redirects: an allow-listed host could otherwise
            # bounce us to an internal address (SSRF).
            with _NO_REDIRECT.open(req, timeout=5) as r:
                declared = r.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > ART_MAX_BYTES:
                    self.send_error(502, "artwork too large")
                    return
                data = r.read(ART_MAX_BYTES + 1)
                if len(data) > ART_MAX_BYTES:
                    self.send_error(502, "artwork too large")
                    return
                ctype = r.headers.get("Content-Type", "image/jpeg")
        except Exception:
            self.send_error(502)
            return
        if not ctype.startswith("image/"):
            self.send_error(415)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            self._json(call_with_timeout(status, 4.0))
            return
        if self.path.startswith("/api/wave"):
            self._json(self._wave())
            return
        if self.path == "/api/sync":
            self._json(mix_sync_info())
            return
        if self.path == "/api/level":
            self._json(_mix_level() or {"level": 0.0})
            return
        if self.path.startswith("/qr.svg"):
            svg = _qr_svg(_phone_url())
            if svg is None:
                self.send_error(503, "qrencode not installed")
                return
            body = svg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/phone":
            self._json({"url": _phone_url(), "lan_ip": LAN_IP, "port": WEB_PORT})
            return
        if self.path.startswith("/mix/sync.mp3"):
            # Proxy the mixer's delayed stream so the phone only needs this
            # origin (and it works when the mixer port is firewalled).
            return self._proxy_sync()
        if self.path.startswith("/mix/clock"):
            return self._proxy_json("/mix/clock")
        if self.path.startswith("/mix/level"):
            return self._proxy_json("/mix/level")
        if self.path.startswith("/api/art"):
            self._serve_art()
            return
        m = re.match(r"^/yt/([A-Za-z0-9_-]+)\.mp3$", self.path.split("?")[0])
        if m:
            self._serve_yt(m.group(1))
            return
        self.send_error(404)

    def do_HEAD(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        if self.path == "/api/status":
            # Same body as GET so Content-Length matches the real response.
            body = json.dumps(call_with_timeout(status, 4.0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        m = re.match(r"^/yt/([A-Za-z0-9_-]+)\.mp3$", self.path.split("?")[0])
        if m:
            f = _yt_file(m.group(1))
            if not f.exists():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.end_headers()
            return
        self.send_error(404)

    def _origin_ok(self):
        """Reject cross-site writes.

        A LAN appliance with no auth is still reachable from any page the
        browser loads, so a plain HTML form (no preflight) could otherwise
        change the volume or source. Require same-origin, and require the
        request to actually be JSON.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            # Same-origin fetch() may omit Origin; fall back to Referer.
            ref = self.headers.get("Referer")
            if ref is None:
                return True  # non-browser client (curl, tests)
            origin = ref
        try:
            from urllib.parse import urlparse
            o = urlparse(origin)
        except Exception:
            return False
        if not o.netloc:
            return False
        return o.netloc.lower() == (self.headers.get("Host") or "").lower()

    def do_POST(self):
        if self.path != "/api/action":
            self.send_error(404)
            return
        if not self._origin_ok():
            self._json({"error": "cross-origin request rejected"}, 403)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "invalid request"}, 400)
            return
        try:
            self._json(call_with_timeout(lambda: action(data.get("cmd", ""), data), 6.0))
        except Exception as e:
            self._json({"error": str(e)}, 500)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# Two pools: status/action calls must never be starved by background radio
# lookups, which can take seconds against the SomaFM API.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="soluna-cmd")
_BG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="soluna-bg")
_LAST_GOOD = {"v": None}


def call_with_timeout(fn, timeout=4.0):
    """Run fn in the shared pool, returning {"error": "timeout"} if it hangs."""
    fut = _POOL.submit(fn)
    try:
        res = fut.result(timeout=timeout)
        if res.get("online") and not res.get("stale"):
            _LAST_GOOD["v"] = res
        return res
    except FTimeout:
        # During a source transition Sonos can briefly stall. Don't flash the
        # whole UI to "offline" while music is still playing — show the last
        # known-good state instead, flagged as stale.
        if _LAST_GOOD["v"]:
            d = dict(_LAST_GOOD["v"])
            d["stale"] = True
            return d
        return _offline_status("timeout")


PAGE = r"""<!doctype html>
<html lang="ja" data-theme="dark" data-size="md">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08090d">
<title>__BRAND__ · Sonos</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%2305060a'/%3E%3Ccircle cx='16' cy='16' r='9' fill='none' stroke='%2322d3ee' stroke-width='2'/%3E%3Ccircle cx='16' cy='16' r='2.5' fill='%2322d3ee'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#05060a;--card:#0e1119;--card2:#0a0d14;--line:#1e2534;--fg:#eef2fa;--mut:#7d879b;
  --acc:#22d3ee;--acc2:#d946ef;--ok:#34d399;--warn:#fbbf24;--err:#fb7185;
  --g1:34,211,238;--g2:217,70,239;--glow:.16;--panel:rgba(14,17,25,.66);--inset:rgba(0,0,0,.35);
}
html[data-theme="light"]{
  --bg:#eef1f7;--card:#ffffff;--card2:#f4f6fb;--line:#d6dce8;--fg:#0f1522;--mut:#5b6475;
  --acc:#0b7f96;--acc2:#a21caf;
  --g1:34,211,238;--g2:217,70,239;--glow:.16;--panel:rgba(255,255,255,.82);--inset:rgba(0,0,0,.05);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
html{height:100%}
body{
  margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;color:var(--fg);
  background:
    /* Single top glow only: the bottom magenta wash showed as a purple band
       under the controller and read as a different app. */
    radial-gradient(120% 50% at 50% -8%,rgba(var(--g1),var(--glow)),transparent 60%),
    var(--bg);
  background-attachment:fixed;overflow-x:hidden;overflow-y:auto;transition:background .6s ease;
}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);
  background-size:44px 44px;mask-image:radial-gradient(circle at 50% 30%,#000,transparent 75%);-webkit-mask-image:radial-gradient(circle at 50% 30%,#000,transparent 75%)}
.app{position:relative;z-index:1;display:flex;flex-direction:column;gap:8px;width:100%;max-width:none;margin:0;min-width:0;box-sizing:border-box;
  padding:calc(6px + env(safe-area-inset-top)) env(safe-area-inset-left) calc(10px + env(safe-area-inset-bottom)) env(safe-area-inset-right)}
.offline{position:fixed;left:0;right:0;top:0;background:var(--err);color:#fff;text-align:center;font-size:12px;padding:6px;z-index:40;display:none}
.offline.show{display:block}

/* header */
.top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.brand{display:flex;align-items:center;gap:9px;min-width:0}
.brandtxt{display:flex;flex-direction:column;line-height:1.1;min-width:0}
.brandtxt #name{font-size:14px;font-weight:800;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brandtxt small{font-size:11px;letter-spacing:1.6px;color:var(--mut);font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);flex:none}
.dot.ok{background:var(--ok);box-shadow:0 0 6px rgba(52,211,153,.8)}
.dot.bad{background:var(--err);box-shadow:0 0 10px var(--err)}
/* Reachable but nothing delivered: amber, not green. */
.dot.warn{background:#f0b45a;box-shadow:0 0 8px rgba(240,180,90,.7)}
.topbtns{display:flex;gap:6px;flex:0 1 auto;flex-wrap:wrap;justify-content:flex-end;min-width:0}
.moremenu{display:none}
.morebtn{display:none;width:44px;height:44px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);
  align-items:center;justify-content:center;cursor:pointer;font-size:18px;font-weight:900;letter-spacing:1px}
.icobtn{width:44px;height:44px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);
  display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s}
.icobtn:active{transform:scale(.93)}
.icobtn svg{width:18px;height:18px}

/* hero now-playing */
.hero{display:flex;gap:14px;align-items:center;padding:14px;border-radius:20px;
  background:var(--panel);border:1px solid var(--line);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:0 14px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.05)}
.artwrap{position:relative;width:118px;height:118px;flex:none;border-radius:16px;overflow:hidden;
  background:linear-gradient(135deg,#141a28,#080b12);box-shadow:0 10px 28px rgba(0,0,0,.55),inset 0 0 0 1px rgba(255,255,255,.07);cursor:pointer}
.vinyl{position:absolute;inset:-3px;border-radius:50%;background:conic-gradient(from 0deg,transparent,rgba(var(--g1),.5),transparent 40%,rgba(var(--g2),.5),transparent 75%);opacity:0;transition:opacity .4s}
.artwrap.playing .vinyl{opacity:.85;animation:spin 4s linear infinite}
.artph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,.28);font-weight:700}
.art{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:none}
.art.show{display:block}
#state{position:absolute;top:6px;right:6px;z-index:3}
.badge{font-size:11px;font-weight:800;padding:4px 9px;border-radius:20px;white-space:nowrap;background:rgba(0,0,0,.72);backdrop-filter:blur(6px);color:#e6ebf5}
.badge.on{background:var(--ok);color:#04240f;box-shadow:0 0 12px var(--ok)}
.deckinfo{flex:1;min-width:0}
.title{font-size:18px;font-weight:800;line-height:1.22;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{color:var(--mut);font-size:12px;margin-top:2px;min-height:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nowsrc{font-size:11px;color:var(--acc);font-weight:700;margin-top:6px;letter-spacing:.2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.seekwrap{margin-top:10px}
.seekbar{position:relative;height:7px;border-radius:4px;background:var(--inset);cursor:pointer;touch-action:none;border:1px solid var(--line)}
.seekbar .fill{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:linear-gradient(90deg,var(--acc),var(--acc2));width:0%;transition:width .25s linear;box-shadow:0 0 10px rgba(var(--g1),.6)}
.seekbar .knob{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;background:#fff;transform:translate(-50%,-50%);box-shadow:0 2px 8px rgba(0,0,0,.6);left:0%;transition:left .25s linear;pointer-events:none}
.seekbar.disabled{opacity:.4;cursor:default}
.seekbar.disabled .knob{display:none}
.times{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-top:5px;font-variant-numeric:tabular-nums}

/* waveform */
.wave{display:block;width:100%;height:64px;border-radius:14px;
  background:linear-gradient(180deg,rgba(var(--g1),.06),rgba(var(--g2),.06));
  border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
@media (max-width:470px){.wave{height:52px}}
.dwave{display:block;width:100%;height:26px;border-radius:8px;margin:2px 0 0;
  background:linear-gradient(180deg,rgba(var(--g1),.07),rgba(var(--g1),.02));
  border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
@media (max-width:470px){.dwave{height:36px}}
@keyframes eq{0%{transform:scaleY(.10)}100%{transform:scaleY(1)}}
@keyframes spin{to{transform:rotate(360deg)}}

/* transport */
.transport{display:flex;align-items:center;justify-content:center;gap:16px}
.transport button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;flex:none}
.transport button:active{transform:scale(.92)}
.transport button:disabled{opacity:.35;cursor:not-allowed}
.transport .tbtn{width:48px;height:48px}
#playbtn{width:76px;height:76px;background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;box-shadow:0 0 0 5px rgba(var(--g1),.14),0 12px 30px rgba(var(--g2),.45);color:#fff}
#mutbtn.on{background:rgba(251,113,133,.18);border-color:var(--err);color:var(--err)}

/* dj toggles */
.djrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;padding:8px;
  border-radius:14px;border:1px solid #1c2026;background:#0c0e11}
.djbtn{min-height:48px;background:#0e1013;border:1px solid #23272e;border-radius:9px;color:#b6bdc9;font-size:11px;font-weight:900;letter-spacing:1.1px;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;transition:.15s;padding:5px;box-shadow:none}
.djbtn:active{transform:scale(.96)}
.djbtn:disabled{opacity:.4;cursor:not-allowed}
.djbtn.on{background:#101a20;border-color:var(--acc);color:#dff6fb;
  box-shadow:inset 0 0 0 1px rgba(var(--g1),.3),0 0 12px rgba(var(--g1),.18)}
.djbtn svg{width:18px;height:18px}
.djbtn small{font-size:10px;font-weight:900;letter-spacing:1px;color:#8b93a1}
.djbtn.on small{color:#dff6fb}

/* ---- DDJ-FLX4 chassis ---------------------------------------------------
   The real unit is a wide, matte-black slab: two jogs at the outer edges, the
   mixer column in the middle, and a thin strip of branding along the top. */
.flx4{padding:0;border-radius:18px;overflow:hidden;border:1px solid #23272e;
  /* Matte black, like the real unit: almost no specular sheen, just a faint
     top-edge highlight so the slab reads as a physical object. */
  background:
    radial-gradient(140% 70% at 50% -20%,rgba(255,255,255,.035),transparent 60%),
    #101215;
  display:flex;flex-direction:column;
  box-shadow:0 1px 0 rgba(255,255,255,.05) inset,0 14px 34px rgba(0,0,0,.6)}
.flx4-top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
  padding:10px 14px;background:#14171b;
  border-bottom:1px solid #08090b;box-shadow:0 1px 0 rgba(255,255,255,.035) inset}
.flx4-brand{display:flex;align-items:baseline;gap:9px;min-width:0}
.flx4-logo{font-size:13px;font-weight:900;letter-spacing:3px;color:#d9dee7;
  text-shadow:0 1px 0 #000}
.flx4-model{font-size:11px;font-weight:800;letter-spacing:2.4px;color:#8b93a3}
/* Real FLX4 geometry: deck A left, deck B right, mixer column between them.
   The second bank (C/D) takes the row underneath, shown only when selected. */
.flx4-body{display:grid;grid-template-columns:1fr minmax(272px,28%) 1fr;
   /* No side padding: the 4px inset pushed the deck cards 5px right of the
     sampler/tabs/lighting panels, which are flush with the slab edge. */
   grid-template-rows:auto auto;gap:0;padding:6px 0 10px;align-items:stretch}
.decks{display:contents}
#dA{grid-column:1;grid-row:1}
#dB{grid-column:3;grid-row:1}
#dC{grid-column:1;grid-row:2}
#dD{grid-column:3;grid-row:2}
.flx4-mix{grid-column:2;grid-row:1;display:flex;flex-direction:column;gap:10px;min-width:0}
/* The hidden bank must not leave a reserved empty row under the live decks. */
.flx4.bank-0 .flx4-body,.flx4.bank-1 .flx4-body{grid-template-rows:auto}
/* Only the selected bank is on the surface, like the two live channels. */
.flx4.bank-0 #dC,.flx4.bank-0 #dD{display:none}
.flx4.bank-1 #dA,.flx4.bank-1 #dB{display:none}
@media(max-width:860px){
  .flx4-body{grid-template-columns:1fr;grid-template-rows:none;gap:12px}
  #dA,#dB,#dC,#dD,.flx4-mix{grid-column:1;grid-row:auto}
  /* Stacked layout must not stretch the mixer to the full tablet width: the
     real unit keeps it a narrow column between the decks. */
  /* Stacked layout: the mixer was capped at 430px and left half the row
     empty on a tablet. Let it use the width it has. */
  .flx4-mix{order:2;width:100%;max-width:min(100%,640px);margin-inline:auto}
  #dA{order:1}#dB{order:3}#dC{order:4}#dD{order:5}
}
/* The console head buttons live in the top strip on the FLX4. */
.console{display:contents}
.consolehead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.syncbtn .lblShort{display:none}
.syncbtn{min-height:44px;padding:0 14px;border-radius:11px;border:1px solid var(--line);
  background:var(--card2);color:var(--fg);font:inherit;font-size:12px;font-weight:800;cursor:pointer}
.syncbtn[aria-pressed="true"]{border-color:var(--acc);color:var(--acc);
  box-shadow:0 0 0 1px var(--acc) inset,0 0 16px rgba(var(--g1),.35)}
.syncbtn:active{transform:scale(.97)}
/* While nothing reaches the speaker, the fix is this one button: outline it in
   the same amber as the 未送信 badge instead of leaving the user to hunt. */
.syncbtn.attention{border-color:#f0b45a;color:#f7c877;
  box-shadow:0 0 0 1px rgba(240,180,90,.55),0 0 14px rgba(240,180,90,.28)}
.huesync{min-height:44px;padding:0 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--card2);color:var(--fg);font:inherit;font-size:clamp(11px,3vw,12px);font-weight:800;cursor:pointer;
  white-space:nowrap;flex:0 0 auto}
.huesync[aria-pressed="true"]{border-color:var(--warn);color:var(--warn);
  box-shadow:0 0 0 1px var(--warn) inset,0 0 16px rgba(251,191,36,.3)}
.cTitle{font-size:13px;font-weight:900;letter-spacing:1.6px;color:var(--acc)}
.mixstate{font-size:12px;color:var(--mut);font-weight:600}
.mixmode{font-size:12px;font-weight:900;letter-spacing:1.2px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;padding:0 14px;border-radius:12px;cursor:pointer;user-select:none;transition:.15s;border:1px solid var(--line)}
.mixmode.auto{color:#04121a;background:linear-gradient(160deg,var(--acc),var(--acc2));box-shadow:0 0 14px rgba(var(--g1),.5)}
.mixmode.manual{color:var(--acc);background:var(--inset);border-color:var(--acc)}
.bankbtn{font-size:12px;font-weight:900;letter-spacing:1.2px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;padding:0 14px;border-radius:12px;cursor:pointer;border:1px solid var(--line);background:var(--inset);color:var(--fg);transition:.15s}
.bankbtn:active{transform:scale(.95)}
.dcPfl{min-height:44px;min-width:44px;font-size:11px;font-weight:900;letter-spacing:.6px;padding:4px 10px;border-radius:12px;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--mut);transition:.12s}
.dcPfl.on{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff;box-shadow:0 0 12px rgba(var(--g1),.6)}
.deckcard{background:transparent;border:0;border-radius:0;
  padding:10px 14px;display:flex;flex-direction:column;gap:7px;transition:.2s;min-width:0;overflow:hidden;
  box-shadow:none}
.deckcard.active{background:linear-gradient(180deg,rgba(255,255,255,.035),transparent 40%)}
.dcHead{display:flex;align-items:center;justify-content:space-between}
.dcName{font-size:11px;font-weight:900;letter-spacing:2.4px;color:#cfd6e4}
.dcState{font-size:11px;color:var(--mut);font-weight:700}
.deckcard.active .dcState{color:var(--ok)}
.deckcard.active.onair .dcState{color:#0b0d12}
.dcState.onair{color:#0b0d12;background:#ffb020;border-radius:5px;padding:1px 6px;font-weight:900}
.deckcard.onair{box-shadow:inset 0 2px 0 rgba(255,176,32,.85),0 0 0 1px rgba(255,176,32,.35)}
.deckcard.active .dcName{color:#fff}
/* Jog + transport sit side by side, like the real deck: big platter left,
   CUE/PLAY stacked to its right. */
.dcJogRow{display:flex;align-items:center;justify-content:center;gap:14px;flex:1 1 auto;min-height:0;max-height:330px}
.dcSide{display:flex;flex-direction:column;align-items:center;gap:10px;flex:none;height:100%;justify-content:center;
  /* sync 44 + gap 10 + tempo (tlen+18) must fit the 360px jog row. */
  --tlen:clamp(150px,26vh,288px)}
.dcSync{min-height:44px;min-width:56px;padding:0 10px;border-radius:10px;border:1px solid #3a414d;
  background:linear-gradient(180deg,#262c36,#171b22);color:#9aa3b4;font-size:11px;font-weight:900;
  letter-spacing:1px;cursor:pointer;transition:.12s;line-height:1.15;text-align:center;white-space:normal}
/* Silk print is two words on the unit; stack them so the control keeps one
   shape from 360 to 1440. */
.dcSync .syncLong{display:block;font-size:10px;letter-spacing:0}
.dcSync.on{background:var(--acc);border-color:#7fe9f8;color:#04222a;box-shadow:0 0 12px rgba(34,211,238,.45)}
.dcSync:active{transform:scale(.95)}
.dcTempoWrap{position:relative;width:56px;height:calc(var(--tlen,200px) + 18px);flex:0 0 auto}
/* Pitch scale: 7 ticks plus a heavier centre detent, as on the unit. */
.dcTempoWrap::before{content:"";position:absolute;z-index:2;left:-6px;top:calc(50% - var(--tlen,200px)/2 - 9px);
  width:3px;height:var(--tlen,200px);
  background:repeating-linear-gradient(180deg,#8b95a6 0 1.5px,transparent 1.5px calc((var(--tlen,200px) - 1.5px)/6));
  pointer-events:none}
/* Centre detent: twice as long and brighter than the ticks, on the left rail so
   it never sits under the thumb's own white stripe. */
.dcTempoWrap::after{content:"";position:absolute;z-index:2;left:-6px;top:calc(50% - 9px);width:5px;height:3px;
  background:#eef2f8;border-radius:2px;pointer-events:none;box-shadow:0 0 5px rgba(238,242,248,.5)}
.dcTempo{-webkit-appearance:none;appearance:none;position:absolute;left:50%;top:50%;
  width:var(--tlen,200px);height:44px;margin:0;background:transparent;
  transform:translate(-50%,calc(-50% - 9px)) rotate(-90deg);outline:none;cursor:pointer}
.dcTempo::-webkit-slider-runnable-track{height:8px;border-radius:5px;
  background:linear-gradient(180deg,#05070c,#1a2130);box-shadow:inset 0 2px 4px rgba(0,0,0,.8)}
.dcTempo::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:22px;height:30px;border-radius:5px;margin-top:-11px;
  background:
    linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,0) 40%),
    linear-gradient(90deg,transparent 0 46%,#e8edf6 46% 54%,transparent 54% 100%),
    linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);
  border:1px solid #05070a;
  box-shadow:0 3px 7px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.14)}
.dcTempo::-moz-range-track{height:8px;border-radius:5px;background:#0a0e16}
.dcTempo::-moz-range-thumb{width:22px;height:30px;border:1px solid #05070a;border-radius:5px;
  background:linear-gradient(90deg,transparent 0 46%,#e8edf6 46% 54%,transparent 54% 100%),
    linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12)}
.dcTempoVal{position:absolute;left:50%;bottom:0;transform:translateX(-50%);min-width:46px;text-align:center;
  font-size:11px;font-weight:800;font-variant-numeric:tabular-nums;color:#9aa3b4;
  background:#0b0e14;border-radius:4px;padding:1px 0;pointer-events:none}
.dcLoopRow{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.dcLoopSub{display:block;font-size:10.5px;font-weight:800;letter-spacing:.3px;opacity:.9;margin-top:1px}
.dcLoop{min-height:36px;border-radius:7px;border:1px solid #2c323d;background:#151920;color:#c3cad6;
  font-size:11px;font-weight:900;letter-spacing:.4px;cursor:pointer;transition:.12s}
.dcLoop.on{background:#232a35;color:#e8edf7;border-color:#3a424f}
.dcLoop:active{transform:scale(.96)}
.dcLoop.disabled,.dcSync.disabled,.dcCue.disabled,.pmode.disabled{cursor:not-allowed;
  /* #6f7787 on #0b0d10 was ~4.2:1; this clears 5:1 and still reads as "off". */
  border:1px solid #22272f!important;background:#0b0d10!important;color:#8b94a4!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.03)!important;opacity:.6}
.dcCue.disabled{border-radius:50%}
/* A live source has no position to scrub, but the jog is still a physical
   knob: dimming the whole platter erased the knurled rim and made it a flat
   disc. Keep the body solid and fade only the moving parts. */
.deckcard.live .platter{cursor:default}
.deckcard.live .platter::before{opacity:.25}
.deckcard.live .platterRing,.deckcard.live .platterMark{opacity:.25}
.deckcard.live .plabel{opacity:.45}
.dcHint{display:none!important}
/* Live-source state: amber, not green — it is a limitation, not a good state. */
/* Amber is reserved for the 未送信 warning; a live source is a limitation of
   the source, not a fault, so it reads as neutral info. */
.dcState.live{color:#9fb6cf;background:rgba(120,160,200,.12);border:1px solid rgba(120,160,200,.35);
  border-radius:5px;padding:1px 6px;font-weight:800}
.deckcard.active .dcState.live{color:#9fb6cf}
.pads.disabled{pointer-events:none}
.pads.disabled .pad{border:1px solid #23272e;background:#0e1013;color:#5c6472;cursor:not-allowed;box-shadow:none}
.pads.disabled .pad{opacity:.75;border-style:solid!important;border-color:#1c2026!important;
  background:#0b0d10!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)!important;cursor:not-allowed}
.pads.disabled .pad .padn,.pads.disabled .pad .padt{color:#6b7380}
.dcTempoWrap.disabled{opacity:1}
.dcTempoWrap.disabled .dcTempo{border:1px solid #20242b;border-radius:8px;cursor:not-allowed}
.pads.disabled .pad .padn,.pads.disabled .pad .padt{color:#8b93a1;opacity:1}

.dcTempo:disabled{opacity:.35;cursor:not-allowed}
.dcTempoWrap.disabled{cursor:not-allowed}
.dcTempoWrap.disabled .dcTempo{opacity:.45;border:1px solid #1c2026;border-radius:8px;background:#0b0d10;color:#8b93a1}
.dcTempoWrap.disabled .dcTempo::-webkit-slider-runnable-track{border:0;background:#15181d;box-shadow:inset 0 1px 2px rgba(0,0,0,.8)}
.dcTempoWrap.disabled .dcTempoVal{border:1px solid #1c2026;background:#0b0d10;color:#7b8391}
.dcTempoWrap.disabled .dcTempo::-webkit-slider-thumb{
  background:
    linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,0) 40%),
    linear-gradient(90deg,transparent 0 46%,#b8c0cd 46% 54%,transparent 54% 100%),
    linear-gradient(180deg,#242a32,#11151a 60%,#0a0d11);
  border:1px solid #05070a;box-shadow:0 2px 5px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.08)}
.platter{position:relative;flex:0 1 auto;width:100%;max-width:360px;aspect-ratio:1;margin:0 auto;border-radius:50%;
  background:
    radial-gradient(circle at 50% 34%,#3d444f 0%,#272d36 42%,#12161d 70%,#070a0e 100%);
  /* Brushed-aluminium rim: fine conic striations over a machined bevel. */
  box-shadow:
    inset 0 0 0 2px rgba(255,255,255,.30),
    inset 0 0 0 6px #4a5260,
    inset 0 0 0 9px #1b2027,
    inset 0 0 0 8px rgba(0,0,0,.55),
    inset 0 2px 3px rgba(255,255,255,.16),
    inset 0 -6px 14px rgba(0,0,0,.7),
    0 0 0 1px #05070a,0 16px 34px rgba(0,0,0,.7);
  display:flex;align-items:center;justify-content:center}
/* Machined metal ring, drawn as a separate layer so the grooves stay crisp. */
/* Machined metal rim. Order matters: the knurl sits on top of a specular
   sweep, which sits on a brushed-metal base. The previous version put a dark
   lighting gradient over the knurl and flattened the ridges to grey. */
.platter::after{content:"";position:absolute;inset:0;border-radius:50%;pointer-events:none;
  background:
    /* Ridges are mid-grey, not white: pure #fff against pure #000 read as a
       hazard stripe instead of machined aluminium. */
    repeating-conic-gradient(from 0deg,
      #4e5763 0deg 0.52deg,
      #333a44 0.52deg 0.95deg,
      #14181d 0.95deg 1.5deg,
      #262c34 1.5deg 1.8deg),
    radial-gradient(circle at 30% 22%,rgba(255,255,255,.30),rgba(255,255,255,0) 46%),
    linear-gradient(150deg,#6b7482 0%,#3a414b 22%,#10141a 52%,#4d5563 74%,#151a20 100%);
  /* closest-side matters: the default (farthest-corner) puts the 79% stop
     outside the element, which masked the whole rim away and left only the
     inset box-shadow looking like a plain grey band. */
  -webkit-mask:radial-gradient(circle closest-side,transparent 0 79%,#000 81% 100%);
  mask:radial-gradient(circle closest-side,transparent 0 79%,#000 81% 100%);
  opacity:1}
.platter::before{content:"";position:absolute;inset:17%;border-radius:50%;
  background:
    radial-gradient(circle at 50% 30%,rgba(255,255,255,.10),transparent 55%),
    repeating-radial-gradient(circle,#0b0e14 0 2px,#151a22 2px 4px);
  opacity:.9;box-shadow:inset 0 0 22px rgba(0,0,0,.9),inset 0 0 0 1px rgba(255,255,255,.07)}
/* Outer ring with a position mark, so the jog reads as a spinning platter. */
.platterRing{position:absolute;inset:4%;border-radius:50%;pointer-events:none;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.platterRing::before{content:"";position:absolute;inset:0;border-radius:50%;
  background:conic-gradient(from 0deg,rgba(255,255,255,.10),rgba(255,255,255,0) 25%,rgba(255,255,255,.10) 50%,rgba(255,255,255,0) 75%,rgba(255,255,255,.10))}
.deckcard.active .platterRing::before{animation:spin 3.2s linear infinite}
.platterMark{position:absolute;top:4%;left:50%;width:5px;height:15%;margin-left:-2.5px;border-radius:2.5px;
  background:linear-gradient(180deg,#ffffff 0%,#dfe6f2 55%,rgba(223,230,242,.25) 100%);z-index:2;
  box-shadow:0 0 10px rgba(255,255,255,.55),0 0 2px rgba(255,255,255,.9)}
.deckcard.active .platter::before{animation:spin 3.2s linear infinite}
/* The hardware jog has a small centre cap, not a 30px letter. */
.plabel{font-size:15px;font-weight:900;letter-spacing:1px;color:#9aa3b4;z-index:1;
  text-shadow:0 1px 0 #000}
.dcTransport{display:flex;flex-direction:column;gap:8px;flex:none}
.dcCue{width:48px;height:48px;border-radius:50%;border:1px solid #3a414d;
  background:linear-gradient(180deg,#262c36,#171b22);color:#c8d0de;font-size:11px;font-weight:900;
  letter-spacing:.6px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;
  box-shadow:0 2px 6px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.08)}
.dcCue:active{transform:scale(.93)}
.dcCue.on{background:#f0a92a;border-color:#ffc65a;color:#241a00}
.dcPlay{width:48px;height:48px;border-radius:50%;border:1px solid #3a414d;
  background:linear-gradient(180deg,#262c36,#171b22);color:#eef2fa;font-size:17px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:.15s;
  box-shadow:0 2px 6px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.08)}
.dcPlay:active{transform:scale(.92)}
.dcPlay.on{background:var(--acc);border-color:#7fe9f8;color:#04222a;box-shadow:0 0 12px rgba(34,211,238,.45)}
.dcHint .nw{white-space:nowrap}
.dcHint{display:none;font-size:12px;line-height:17px;height:auto;color:#f0b45a;text-align:center;padding:0 2px;word-break:keep-all;overflow-wrap:anywhere}

.dcTitle{font-size:13px;line-height:20px;height:20px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg);text-align:center}
.dcSrc{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.dcSrc button{min-height:44px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--fg);font-size:11px;font-weight:700;cursor:pointer;transition:.12s}
.dcSrc button:active{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff}
.dcYt{display:flex;gap:6px}
.dcYt input{flex:1;min-width:0;min-height:44px;background:var(--card2);border:1px solid var(--line);border-radius:10px;color:var(--fg);padding:0 10px;font-size:12px;outline:none}
.dcYt input:focus{border-color:var(--acc)}
.dcYt button{flex:none;min-width:50px;min-height:44px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--fg);font-size:12px;font-weight:700;cursor:pointer}
.dcYt button:active{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff}
.dcSecLbl{font-size:11px;font-weight:900;letter-spacing:1.8px;color:#8b93a3;margin:2px 0 0}
.dcMore{display:flex;flex-direction:column;gap:5px;margin-top:2px}
.dcMore>summary{min-height:44px;min-width:44px;display:flex;align-items:center;justify-content:center;gap:6px;
  list-style:none;cursor:pointer;border:1px solid var(--line);border-radius:10px;background:var(--card);
  color:var(--acc);font-size:12px;font-weight:800;letter-spacing:.4px;transition:.12s}
.dcMore>summary::-webkit-details-marker{display:none}
.dcMore>summary::after{content:"▾";font-size:11px;transition:transform .15s}
.dcMore[open]>summary::after{transform:rotate(180deg)}
.dcMore>summary:active{transform:scale(.97)}
.dcMore[open]>summary{margin-bottom:6px;border-color:var(--acc)}
.dcMore .pads,.dcMore .dcSrc,.dcMore .dcYt{margin-top:6px}
.dcPadModes{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.pmode{min-height:30px;border-radius:7px;border:1px solid #2c323d;background:#151920;color:#c3cad6;
  font-size:10px;font-weight:900;letter-spacing:.4px;cursor:pointer;transition:.12s;padding:0 2px}
.pmode.on{background:#232a35;color:#e8edf7;border-color:#3a424f;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
/* The selected mode stays lit on the unit whatever the source is; only the
   pads below it go dark. Keep it dimmer than an active mode, never black. */
.pmode.disabled.on{border:1px solid #3a424f!important;background:#20262f!important;
  color:#aeb7c4!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.06)!important;opacity:1}
.pmode:active{transform:scale(.96)}
.dcPadBody{display:flex;align-items:center;gap:10px}
.dcPadBody .dcTransport{justify-content:center;gap:8px}
.dcPadBody .dcTransport{justify-content:center;gap:8px}
.pads{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-auto-rows:auto;gap:8px;
  /* The grid sits in a flex row beside the transport: width:100% measured the
     whole deck and pushed the last pad 46px past the edge. Grow into the
     remaining space instead. */
  flex:1 1 auto;width:auto;min-width:0}
.pad{position:relative;min-height:44px;min-width:0;aspect-ratio:1/0.85;border-radius:10px;border:1px solid #3a414c;
  background:linear-gradient(180deg,#2a2f37,#1b2027);color:#c9d1db;
  font-size:12px;font-weight:800;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;transition:.12s;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}
.pad:active{transform:scale(.94)}
.pad .padn{font-size:13px;font-weight:900}
.pad .padt{font-size:11px;font-weight:700;letter-spacing:.3px;opacity:.9}
.pad.set{background:#1f6f7a;border-color:#2b8b98;color:#eafcff}
.pad.set .padt{color:#c9f2f8}
.pad.hit{background:#37b6c6;border-color:#5ad2e0;color:#04141a;box-shadow:0 0 10px rgba(55,182,198,.45)}
.pad.empty{border:1px solid #20242b;background:
  linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,0) 55%),
  linear-gradient(180deg,#23272e,#14171c);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.06),inset 0 -2px 4px rgba(0,0,0,.6)}
.pad.empty .padn{position:absolute;top:3px;left:6px;font-size:10px;opacity:.8}
.pad.empty .padt{font-size:18px;line-height:1;font-weight:400;opacity:.95}

/* crossfader */
.xfwrap{display:flex;flex-direction:column;gap:6px}
.xflabels{display:flex;justify-content:space-between;font-size:11px;font-weight:800;letter-spacing:1.4px;color:#9aa3b4}
.xflabels span:nth-child(2){color:#cfd6e4}
.xfader{-webkit-appearance:none;appearance:none;width:100%;height:12px;border-radius:6px;outline:none;
  background:linear-gradient(90deg,var(--acc),#2a3550 50%,var(--acc2))}
.xfader::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:30px;height:30px;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 0 0 4px rgba(var(--g1),.25),0 3px 10px rgba(0,0,0,.7)}

/* channel mixer */
.sampler{padding:12px;border-radius:20px;background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;gap:10px}
.smpHead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.smpTitle{font-size:12px;font-weight:900;letter-spacing:1px;color:var(--acc)}
.smpDeckSel{display:flex;gap:6px}
.smpDeckSel button{min-height:44px;min-width:44px;border-radius:9px;border:1px solid #23272e;background:#0e1013;color:#8b93a1;font-weight:900;font-size:12px;letter-spacing:.6px;cursor:pointer}
.smpDeckSel button[aria-pressed="true"]{background:#101a20;border-color:var(--acc);color:#dff6fb;
  box-shadow:inset 0 0 0 1px rgba(var(--g1),.3),0 0 12px rgba(var(--g1),.18)}
.padgrid{display:grid;grid-template-columns:repeat(4,minmax(48px,1fr));gap:8px}
.spad{position:relative;min-height:56px;aspect-ratio:1;border-radius:9px;border:1px solid #23272e;
  /* Same flat resin as the deck pads: two pad styles on one console read as
     two different products. */
  background:#0e1013;color:#8b93a1;box-shadow:none;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;cursor:pointer;padding:4px;transition:.12s}
.spad:active,.spad.hit{transform:scale(.94);background:linear-gradient(160deg,var(--acc),var(--acc2));color:#fff;border:none;box-shadow:0 0 16px rgba(var(--g2),.6)}
.spad .spn{font-size:13px;font-weight:900}
.spad .spt{font-size:11px;font-weight:700;opacity:.85;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spad.set{border-color:var(--acc);color:#cfe9ff;background:#101a20}
.spad.empty .spn{position:absolute;top:4px;left:7px;font-size:11px;opacity:.55}
.spad.empty .spt{font-size:20px;line-height:1;opacity:.95;font-weight:400}
.spad.set{border-style:solid}
.looper{display:flex;flex-direction:column;gap:8px;padding-top:6px;border-top:1px solid var(--line);height:100%}
/* The looper column is as tall as the 16-pad grid, so it had ~120px of bare
   plate under the hint. A loop-length readout fills it and is real hardware. */
.loopBig{display:flex;align-items:center;justify-content:center;gap:6px;
  flex:1 1 auto;min-height:0;margin-top:8px;padding:6px 0;border-top:1px solid #1c2026}
.loopBig{align-items:baseline}
.loopBig b{font-size:34px;font-weight:900;font-variant-numeric:tabular-nums;letter-spacing:1px;color:#dff6fb;line-height:1}
.loopBig i{font-style:normal;font-size:12px;font-weight:900;letter-spacing:1.4px;color:#8b93a1;line-height:1;position:relative;top:3px}
/* Unrecorded = dark segment, like the unit's idle display. */
.loopBig b.off{color:#b9c2cf;font-weight:800}
.loopBig.rec b{color:#f0b45a}
.loopTitle{font-size:12px;font-weight:900;letter-spacing:.6px;color:var(--acc)}
.loopTitle span{font-weight:700;color:var(--fg);margin-left:6px}
.loopBtns{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
.loopBtns button{min-height:44px;border-radius:10px;border:1px solid var(--line);background:var(--inset);color:var(--fg);font-size:11px;font-weight:800;cursor:pointer;padding:2px}
.loopBtns button[aria-pressed="true"]{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff}
.loopBtns button:disabled{opacity:.4;cursor:default}
.loopHint{font-size:11px;line-height:1.5;color:var(--mut);font-weight:700;line-break:strict;word-break:auto-phrase}
.loopMeter{display:none;height:10px;border-radius:6px;background:#0b0d10;border:1px solid #232830;overflow:hidden}
.loopMeter i{display:block;height:100%;width:0%;border-radius:5px;background:linear-gradient(90deg,#22d3ee,#7fe9f8);transition:width .2s}
.loopMeterWrap{position:relative;--tw:20px}
.loopThumb{position:absolute;top:-5px;left:0;width:20px;height:20px;border-radius:5px;box-sizing:border-box;
  background:linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);border:1px solid #05070a;
  box-shadow:0 2px 4px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.14);
  transition:left .2s,opacity .2s;opacity:0}
.loopThumb::after{content:"";position:absolute;left:50%;top:3px;bottom:3px;width:2px;margin-left:-1px;
  border-radius:1px;background:#e8edf6}
/* Visual stays 14px; the grab area is 44px tall and 30px wide. */
.loopThumb::before{content:"";position:absolute;left:-8px;right:-8px;top:-13px;bottom:-13px}
.loopThumb{opacity:.7}
.loopThumb.on{opacity:1;border-color:#7fe9f8}
.loopTicks span{color:#9aa3b4}
.loopTicks span.on{color:#e8edf6;font-weight:900}
.loopTicks .tickEnd{color:#a8b0be;letter-spacing:0;padding-right:1px}
.loopMeterWrap::before{content:none}
.loopTicks{position:relative;height:12px;font-size:10px;font-weight:800;letter-spacing:.4px;color:#9aa3b4;margin-top:7px}
/* Ticks mark where the thumb CENTRE sits for each length, so they are inset by
   half a thumb at both ends instead of using the raw rail width. */
.loopTicks span{position:absolute;top:0;
  left:calc(var(--tw)/2 + (100% - var(--tw)) * var(--t,0));transform:translateX(-50%);white-space:nowrap}
.loopTicks span::before{content:"";position:absolute;left:50%;top:-4px;width:1px;height:3px;background:#3a414d;transform:translateX(-50%)}
.loopTicks span:last-child::before{left:50%;right:auto;transform:translateX(-50%)}
.loopTicks span:first-child{transform:translateX(-50%)}
.loopTicks span:last-child{transform:translateX(-50%)}
@media(max-width:420px){.padgrid{grid-template-columns:repeat(4,minmax(44px,1fr));gap:6px}
  /* 3+2 gave the second row two 1.5x-wide buttons. One row of five equal. */
  .loopBtns{grid-template-columns:repeat(5,1fr);gap:4px}
  .loopBtns button{font-size:10.5px;letter-spacing:-.4px;padding:0;white-space:nowrap}
  /* Every other card insets 12px; the shuffle row used 8px and sat 4px wider. */
  .djrow{padding:8px 12px}}
.mixer{display:flex;flex-direction:column;justify-content:flex-start;gap:8px;padding:10px 12px 12px;border-radius:0;flex:1 1 auto;
  /* The real unit is one continuous top plate: no separate black slab, just a
     hairline seam where the mixer section is silk-screened. */
  background:transparent;min-width:0;
  border-left:1px solid #1c2026;border-right:1px solid #1c2026;
  box-shadow:none}
.loadRow{display:grid;grid-template-columns:1fr 34px 1fr;align-items:center;gap:6px;margin-bottom:2px}
.loadBtn{min-height:34px;border-radius:8px;border:1px solid #2c323d;background:#151920;color:#c3cad6;
  font-size:10px;font-weight:900;letter-spacing:.6px;cursor:pointer;transition:.12s}
.loadBtn:active{transform:scale(.96);background:#232a35}
/* 28-34px tall visually so the mixer matches the deck's height; the touch
   target is padded back to 44px without changing the layout. */
.loadBtn{position:relative}
.loadBtn::after{content:"";position:absolute;left:0;right:0;top:50%;height:44px;transform:translateY(-50%)}
.loadBtn.on{border-color:var(--acc);color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
.loadKnob{position:relative;width:30px;height:30px;border-radius:50%;justify-self:center;
  background:radial-gradient(circle at 38% 32%,#3a414b,#14171c 70%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 2px 5px rgba(0,0,0,.6)}
.loadKnob i{position:absolute;left:50%;top:4px;width:1.5px;height:8px;margin-left:-.75px;background:#cfd6e2;border-radius:1px}
/* 22-30px visual dial; the touch target stays 44px via an invisible margin. */
.loadKnob::after{content:"";position:absolute;inset:-9px;border-radius:50%}
.mixHead{display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:nowrap;
  font-size:10px;font-weight:900;letter-spacing:2.2px;color:var(--acc);padding:0 2px;min-width:0}
.mixHead>span:first-child{flex:0 0 auto}
.mixHeadSub{letter-spacing:1.4px;color:#aab2c2;display:flex;align-items:center;gap:6px;
  min-width:0;white-space:nowrap}
.mixHeadSub b{font-weight:800;color:#dfe5f0}
.mixHeadSub i[hidden]{display:none}
.mixHeadSub i{font-style:normal;font-size:11px;font-weight:800;letter-spacing:0;padding:2px 6px;border-radius:7px;
  background:#3a2f14;color:#f0b45a;cursor:pointer;white-space:nowrap;flex:0 0 auto}
.mixHeadSub .why{display:block;position:fixed;z-index:60;width:min(320px,calc(100vw - 24px));padding:10px 30px 10px 12px;box-shadow:0 10px 28px rgba(0,0,0,.55);
  border-radius:9px;background:#1b2027;border:1px solid #3a414c;color:#dfe5f0;font-size:11px;line-height:1.5;letter-spacing:0;font-weight:600}
.mixHeadSub .why[hidden]{display:none}
.mixHeadSub .why::before{content:"";position:absolute;top:-6px;left:var(--ax,20px);width:10px;height:10px;transform:rotate(45deg);background:#1b2027;border-left:1px solid #3a414c;border-top:1px solid #3a414c;z-index:-1}
.mixHeadSub .why.above::before{top:auto;bottom:-6px;border-left:0;border-top:0;border-right:1px solid #3a414c;border-bottom:1px solid #3a414c}
.mixHeadSub .whyx{position:absolute;top:4px;right:4px;width:22px;height:22px;border:0;border-radius:6px;background:transparent;color:#aab2c2;font-size:14px;line-height:1;cursor:pointer}
.mixHeadSub .whyx:hover{background:#2a3038;color:#fff}

.mixFxRow{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:0 2px;margin-bottom:6px}
.mixFx{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  border:1px solid #1c2026;border-radius:7px;padding:4px 2px 14px;min-height:26px;
  background:#0b0d10;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
.mixFx b{font-size:9.5px;font-weight:900;letter-spacing:.3px;color:#b6bdc9;white-space:nowrap}
.fxknobs{display:flex;gap:18px;align-items:center;padding-bottom:10px;margin-top:5px}
.fxk.off{opacity:.35;filter:grayscale(1)}
.fxk{display:block;width:34px;height:34px;border-radius:50%;border:1px solid #262b33;
  background:radial-gradient(circle at 38% 32%,#20252c,#12151a 70%);position:relative}
/* Indicator line, like the unit's knobs. */
.fxk::after{content:'';position:absolute;left:50%;top:3px;width:1.5px;height:7px;background:#cfd6e2;
  border-radius:1px;transform:translateX(-50%)}
/* Printed scale around the dial (own element: ::before carries the label). */
.fxt{position:absolute;inset:-4px;border-radius:50%;pointer-events:none;
  background:repeating-conic-gradient(from -120deg,#a9b2c2 0 2.4deg,transparent 2.4deg 30deg);
  -webkit-mask:radial-gradient(circle closest-side,transparent 0 calc(100% - 4px),#000 calc(100% - 3px));
  mask:radial-gradient(circle closest-side,transparent 0 calc(100% - 4px),#000 calc(100% - 3px));opacity:.9}
/* 34px dial, 44px+ touch target. */
.fxg{position:absolute;inset:-9px;border-radius:50%}
.fxk[data-l]::before{content:attr(data-l);position:absolute;left:50%;top:100%;transform:translateX(-50%);
  margin-top:7px;font-size:10px;font-weight:800;font-style:normal;letter-spacing:0;color:#b6bdc9;white-space:nowrap}
.chstrips{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px 4px 2px;border-top:1px solid #232830;flex:1 1 auto;align-content:center;
  --flen:clamp(190px,38vh,360px)}
.chstrip{display:flex;flex-direction:column;align-items:center;gap:14px;min-width:0}
.chfaderRow{display:flex;align-items:center;justify-content:center;gap:6px;width:100%}
.chName{font-size:11px;font-weight:900;letter-spacing:1.4px;color:#cfd6e4}
.chCue{min-height:44px;min-width:52px;padding:0 10px;border-radius:8px;border:1px solid #3a414d;
  background:linear-gradient(180deg,#262c36,#171b22);color:#9aa3b4;font-size:10px;font-weight:900;
  letter-spacing:1px;cursor:pointer;transition:.12s}
.chCue{position:relative}
.chCue::after{content:"";position:absolute;left:0;right:0;top:50%;height:44px;transform:translateY(-50%)}
.chCue[aria-pressed="true"]{background:#f0a92a;border-color:#ffc65a;color:#241a00;
  box-shadow:0 0 10px rgba(240,169,42,.45)}
.chCue:active{transform:scale(.95)}
.chfaderWrap{display:flex;align-items:center;justify-content:center;height:var(--flen,180px);flex:0 0 44px;width:44px;min-width:0;position:relative}
/* Channel faders have a printed scale on the unit; without it the fader read
   as a bare slot. 10 ticks, brighter every 5th, drawn outside the track. */
.chfaderWrap::before{content:"";position:absolute;left:-1px;top:50%;width:5px;
  height:calc(var(--flen,180px) - 24px);transform:translateY(-50%);pointer-events:none;
  background:repeating-linear-gradient(180deg,#7c8698 0 1px,transparent 1px calc((var(--flen,180px) - 24px - 1px)/10))}
.chfader{-webkit-appearance:none;appearance:none;flex:0 0 var(--flen,180px);width:var(--flen,180px);height:44px;margin:0;background:transparent;
  transform:rotate(-90deg);transform-origin:center;outline:none;cursor:pointer}
.chfader::-webkit-slider-runnable-track{height:8px;border-radius:5px;
  background:linear-gradient(180deg,#05070c,#1a2130);box-shadow:inset 0 2px 4px rgba(0,0,0,.8)}
/* Real FLX4 faders are black resin with a single white index line down the
   middle. A white cap made the fader the brightest thing on the console. */
.chfader::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:24px;height:34px;border-radius:5px;margin-top:-13px;
  background:
    linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,0) 40%),
    linear-gradient(90deg,transparent 0 46%,#e8edf6 46% 54%,transparent 54% 100%),
    linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);
  border:1px solid #05070a;
  box-shadow:0 3px 7px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.14)}
.chfader::-moz-range-track{height:8px;border-radius:5px;background:#0a0e16}
.chfader::-moz-range-thumb{width:24px;height:34px;border:1px solid #05070a;border-radius:5px;
  background:linear-gradient(90deg,transparent 0 46%,#e8edf6 46% 54%,transparent 54% 100%),
    linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12)}
.chmeter{position:relative;flex:0 0 8px;width:8px;height:var(--flen,180px);border-radius:4px;
  /* Unlit LEDs stay faintly visible: an all-black meter looked broken. */
  background:repeating-linear-gradient(180deg,#1b2028 0 4px,#0a0d11 4px 6px);
  box-shadow:inset 0 1px 3px rgba(0,0,0,.9);overflow:hidden}
.chmeter::after{content:'';position:absolute;left:0;right:0;bottom:var(--peak,0%);height:2px;background:#fff;opacity:.55;transition:bottom .1s linear}
.chmeter::before{content:'';position:absolute;inset:0;border-radius:4px;opacity:.22;
  background:linear-gradient(180deg,#ff4d4d 0%,#ff4d4d 12%,#ffc233 26%,#ffc233 42%,#3ddc84 58%,#3ddc84 100%);
  -webkit-mask:repeating-linear-gradient(180deg,#000 0 4px,transparent 4px 6px);
  mask:repeating-linear-gradient(180deg,#000 0 4px,transparent 4px 6px)}
.chmeter i{position:absolute;left:0;right:0;bottom:0;height:0%;border-radius:2px;
  /* Real FLX4 meters are discrete LEDs: green low, amber mid, red top. The
     fill is masked into 4px segments so it reads as LEDs, not a gradient bar. */
  background:linear-gradient(180deg,#ff4d4d 0%,#ff4d4d 12%,#ffc233 26%,#ffc233 42%,#3ddc84 58%,#3ddc84 100%);
  background-size:100% var(--flen,180px);background-position:bottom;background-repeat:no-repeat;
  -webkit-mask:repeating-linear-gradient(180deg,#000 0 4px,transparent 4px 6px);
  mask:repeating-linear-gradient(180deg,#000 0 4px,transparent 4px 6px);
  transition:height .12s linear}
.masterSec{position:relative;border:1px solid #23272e;border-radius:12px;padding:14px 4px 4px;background:#0b0d10}
.secLabel{position:absolute;top:-7px;left:10px;padding:0 6px;background:#0d0f12;
  font-size:9px;font-weight:900;letter-spacing:1.8px;color:#7f8798}
.knobrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;padding:0 0 4px}
/* Before a Sonos client is connected the controls are inert, but the printed
   scale around them is silk print, not a control: dimming the whole row to 55%
   also dimmed the ticks to the point of being unreadable. Dim only the parts
   that actually move. */
.knobrow.disabled{opacity:1}
.knobrow.disabled .knob-dial,.knobrow.disabled .knob-cap,.knobrow.disabled .knob-ptr{opacity:.55}
/* A black knob at 55% is still black: the ring has to fade too, and the panel
   has to say why it is inert. */
.knobrow.disabled .rknob::before{opacity:.35}
.masterSec:has(.knobrow.disabled)::after{content:none;
  font-size:9px;font-weight:900;letter-spacing:.4px;white-space:nowrap;pointer-events:none}
/* VOL/BASS/TREBLE is silk print, not a control: keep it readable and dim only
   the live value, so "printed scale bright / value dim" reads as "no client". */
.knobrow.disabled .cval{opacity:.5}
.knobchan{display:flex;flex-direction:column;align-items:center;gap:11px;background:transparent;border:none;border-radius:10px;padding:2px 2px 4px}
.klabel{font-size:11px;letter-spacing:1.6px;color:#9aa3b4;font-weight:800;order:-1}
.cval{width:100%;text-align:center;font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;color:#eef2f8;line-height:1.05;white-space:nowrap}
.cval small{display:block;margin-top:2px;font-size:11px;font-weight:600;color:#a3abb9;letter-spacing:.2px}
.cval[data-sending]{color:var(--acc)}
/* rotary knob */
.rknob{position:relative;width:62px;height:62px;touch-action:none;cursor:grab;outline:none;border-radius:50%}
.rknob:active{cursor:grabbing}
/* Invisible 6px grab margin: the visible dial can shrink with the layout
   without the touch target dropping under 44px. */
.rknob::after{content:"";position:absolute;inset:-6px;border-radius:50%}
/* Printed scale around the dial. Display only — the range input still owns
   the value, so nothing about the interaction changes. */
.rknob::before{content:"";position:absolute;inset:-6px;border-radius:50%;pointer-events:none;
  background:repeating-conic-gradient(from -135deg,#b9c1cf 0 2.8deg,transparent 2.8deg 27deg);
  /* Keep the print in the 8px ring outside the dial at every size. */
  -webkit-mask:radial-gradient(circle closest-side,transparent 0 calc(100% - 6px),#000 calc(100% - 5px));
  mask:radial-gradient(circle closest-side,transparent 0 calc(100% - 6px),#000 calc(100% - 5px));opacity:.95}
.rknob:focus-visible{box-shadow:0 0 0 3px rgba(var(--g1),.5)}
.knob-dial{position:absolute;inset:0;border-radius:50%;
  background:
    radial-gradient(circle at 50% 38%,#2b3446 0%,#1a2130 46%,#0c1018 78%),
    conic-gradient(from 210deg,#0c1018 0deg,#1a2130 60deg,#2b3446 120deg,#1a2130 200deg,#0c1018 300deg);
  box-shadow:0 8px 18px rgba(0,0,0,.6),inset 0 2px 3px rgba(255,255,255,.12),inset 0 -3px 6px rgba(0,0,0,.7);
  border:1px solid rgba(255,255,255,.08);
  transition:transform .05s linear}
.knob-dial::before{content:"";position:absolute;inset:7px;border-radius:50%;
  background:radial-gradient(circle at 50% 40%,#3a4558,#161c28 70%);
  box-shadow:inset 0 1px 2px rgba(255,255,255,.15),inset 0 -2px 4px rgba(0,0,0,.6)}
/* Index line inside the cap (60-92% of the radius). It must be a sibling of
   .knob-dial, not a child: the dial's transform creates a stacking context, so
   a child with z-index 4 still lost to the cap at z-index 3 and never showed.
   The element spans the knob and paints the line with a gradient so it rotates
   around the true centre. */
.knob-ptr{position:absolute;inset:0;border-radius:50%;z-index:4;pointer-events:none;
  transform:rotate(0deg);transition:filter .15s}
/* A gradient band on the round element became a wide chord, not a radial line,
   because border-radius:50% clips it. Draw a real 3px bar instead. */
.knob-ptr::after{content:"";position:absolute;left:50%;top:15px;width:3px;height:13px;
  margin-left:-1.5px;border-radius:1.5px;background:#e8edf6}
/* The pointer only glows while the knob is focused or being turned. */
.rknob:focus-visible .knob-ptr::after,.rknob.turning .knob-ptr::after{background:var(--acc)}
.rknob:focus-visible .knob-ptr,.rknob.turning .knob-ptr{filter:drop-shadow(0 0 4px rgba(var(--g1),.9))}
/* A real knob is a black cylinder: flat dark top, index line inside the cap,
   no chrome dome. The old light-grey cap plus the long pointer read as a lever. */
.knob-cap{position:absolute;left:50%;top:50%;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;z-index:3;
  background:radial-gradient(circle at 42% 30%,#2e343d,#191d23 62%,#0b0e12);
  border:1px solid #05070a;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 2px 5px rgba(0,0,0,.65);pointer-events:none}
.rknob input[type=range]{position:absolute;inset:0;width:100%;height:100%;opacity:0;pointer-events:none}
/* crossfader */
.xfwrap{display:flex;flex-direction:column;gap:6px;margin-top:0;padding:8px 2px 0;border-top:1px solid #232830}
.xflabels{display:flex;justify-content:space-between;font-size:11px;font-weight:800;letter-spacing:1.2px;color:var(--mut)}
.xflabels span:nth-child(2){color:var(--acc)}
.xfader-track{position:relative;height:56px;display:flex;align-items:center;padding:0 4px}
/* One groove only. A second darker slot underneath made the left end of the
   rail read as a filled progress bar. */
.xfader-track{--capW:34px}
.xfader-rail{position:absolute;left:24px;right:24px;top:50%;height:10px;margin-top:-5px;border-radius:6px;
  background:linear-gradient(180deg,#05070c,#1a2130);box-shadow:inset 0 2px 4px rgba(0,0,0,.8),0 1px 0 rgba(255,255,255,.05)}
.xfader-rail::before{content:none}
/* Centre detent: the real unit clicks at 0, so show where the middle is. */
.xfader-rail::after{content:"";position:absolute;left:50%;top:-6px;bottom:-6px;width:2px;margin-left:-1px;
  background:rgba(255,255,255,.28);border-radius:1px}
.xfader-cap{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:34px;height:40px;border-radius:6px;z-index:3;
  background:
    linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,0) 40%),
    linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);
  border:1px solid #05070a;
  box-shadow:0 4px 10px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.14);
  display:flex;flex-direction:column;justify-content:center;gap:3px;padding:0 6px;pointer-events:none;transition:left .08s linear}
.xfader-cap span{display:block;height:2px;border-radius:2px;background:#e8edf6}
.xfader{position:absolute;inset:0;width:100%;height:100%;-webkit-appearance:none;appearance:none;background:transparent;opacity:0;cursor:grab;margin:0}
.xfader:active{cursor:grabbing}

/* tabs */
.tabs{display:flex;flex-direction:column;gap:10px}
.tabbar{display:flex;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:13px;padding:4px}
.tab{flex:1;min-height:44px;border:1px solid transparent;background:transparent;color:var(--mut);font-size:12px;font-weight:800;letter-spacing:.6px;border-radius:10px;cursor:pointer;transition:.15s}
.tab.on{background:rgba(var(--g1),.16);color:#cfe9ff;border-color:var(--acc);box-shadow:0 0 14px rgba(var(--g1),.3)}
.tabpanel{display:none;flex-direction:column;gap:9px}
.tabpanel.on{display:flex}
.src{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.srchint{margin:0;font-size:11px;color:var(--mut);font-weight:700;letter-spacing:.2px}
.src button{min-height:48px;background:linear-gradient(180deg,#1a1e24,#12151a);color:var(--fg);border:1px solid #2a2f37;border-radius:13px;padding:9px;font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:.15s;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.src button:active{transform:scale(.97)}
.src button:disabled{opacity:.4;cursor:not-allowed}
.src button.on{background:rgba(var(--g1),.16);border-color:var(--acc);color:#cfe9ff;box-shadow:0 0 14px rgba(var(--g1),.3)}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row button{flex:1;min-width:64px;min-height:46px;background:linear-gradient(180deg,#1a1e24,#12151a);color:var(--fg);border:1px solid #2a2f37;border-radius:13px;padding:10px;font-size:12px;font-weight:700;cursor:pointer;transition:.15s;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.row button:active{transform:scale(.97)}
.row button:disabled{opacity:.4;cursor:not-allowed}
.row button.on{background:rgba(var(--g1),.16);border-color:var(--acc);color:#cfe9ff;box-shadow:0 0 14px rgba(var(--g1),.3)}
.ytrow{display:flex;gap:8px}
.ytrow input{flex:1;min-width:0;min-height:48px;background:var(--card2);border:1px solid var(--line);border-radius:13px;color:var(--fg);padding:0 12px;font-size:13px;outline:none}
.ytrow input:focus{border-color:var(--acc)}
.ytrow input::placeholder{color:var(--mut)}
.ytrow button{flex:none;min-width:70px;min-height:48px;background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;border-radius:13px;color:#fff;font-size:13px;font-weight:800;cursor:pointer;transition:.15s}
.ytrow button:active{transform:scale(.96)}
.ytrow button:disabled{opacity:.5;cursor:not-allowed}
.ythint{color:var(--mut);font-size:12px;line-height:1.55;min-height:18px}
.ythint.err{color:var(--err)}
.ythint.ok{color:var(--ok)}
.hint{color:var(--mut);font-size:12px;line-height:1.65;padding-top:4px}

/* --- Console skin for the panels under the decks -------------------------
   These were generic web-app cards (rounded 13px, gradient fill, cyan pill for
   the selected tab) sitting under a hardware-styled console. Same plate, same
   hairline border, silk-screened uppercase labels, LED-style selection. */
.tabs{padding:10px 12px 12px;border-radius:14px;border:1px solid #1c2026;background:#0c0e11;gap:9px}
.tabbar{gap:0;background:#0a0c0f;border:1px solid #1c2026;border-radius:9px;padding:3px}
.tab{min-height:40px;border:0;border-radius:6px;color:#a4adbb;
  font-size:11px;font-weight:900;letter-spacing:1.6px}
/* A divider only on the right of one tab read as a stray mark. */
.tabbar{gap:3px}
.tab.on{background:#101a20;color:#dff6fb;border-color:transparent;
  box-shadow:inset 0 -2px 0 var(--acc),inset 0 0 0 1px rgba(var(--g1),.35)}
.src button,.row button{min-height:46px;border-radius:9px;border:1px solid #23272e;
  background:#0e1013;box-shadow:none;font-size:11px;font-weight:900;letter-spacing:1.1px;color:#b6bdc9}
.src button.on,.row button.on{background:#101a20;border-color:var(--acc);color:#dff6fb;
  box-shadow:inset 0 0 0 1px rgba(var(--g1),.3),0 0 12px rgba(var(--g1),.18)}
.ytrow input{min-height:46px;border-radius:9px;border:1px solid #23272e;background:#0e1013;font-size:12px}
.ytrow button{min-height:46px;border-radius:9px;background:#101a20;border:1px solid var(--acc);
  color:#dff6fb;font-size:11px;font-weight:900;letter-spacing:1.2px}
.hue{border-radius:14px;border:1px solid #1c2026;background:#0c0e11;backdrop-filter:none;-webkit-backdrop-filter:none}
.hueTitle{letter-spacing:1.8px}
.huetoggle{border-radius:9px;border:1px solid #23272e;background:#0e1013;letter-spacing:1.2px}
.huetoggle.on{background:#101a20;border-color:var(--acc);box-shadow:inset 0 0 0 1px rgba(var(--g1),.3)}

/* lights / hue */
.stopbtn{display:flex;align-items:center;gap:6px;min-height:44px;padding:0 14px;border-radius:12px;
  border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:13px;font-weight:900;
  letter-spacing:.5px;cursor:pointer;transition:.15s}
.stopbtn:active{transform:scale(.95)}
.stopbtn svg{width:16px;height:16px;flex:none}
.stopbtn[disabled]{opacity:.5;cursor:default}
.qrbtn{position:relative}
.qrpanel{display:none;flex-direction:column;align-items:center;gap:10px;padding:16px;
  border-radius:18px;background:var(--panel);border:1px solid var(--line);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.qrpanel.show{display:flex}
.qrhead{display:flex;align-items:center;gap:8px;width:100%}
.qrTitle{font-size:13px;font-weight:900;letter-spacing:1.6px;color:var(--acc)}
.qrclose{margin-left:auto;min-width:44px;min-height:44px;border-radius:12px;border:1px solid var(--line);
  background:var(--card);color:var(--mut);font-size:18px;font-weight:900;cursor:pointer;line-height:1}
.qrclose:active{transform:scale(.95)}
.qrimg{width:min(240px,68vw);height:auto;aspect-ratio:1/1;background:#fff;border-radius:12px;padding:10px;display:block}
.qrurl{font-size:12px;font-weight:700;color:var(--mut);word-break:break-all;text-align:center;max-width:100%}
.qrhint{font-size:11px;color:var(--mut);text-align:center;line-height:1.5}
.huehint{font-size:11px;line-height:1.5;color:var(--mut);font-weight:700}
.hue{padding:12px 14px;border-radius:18px;background:var(--panel);border:1px solid var(--line);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);display:flex;flex-direction:column;gap:10px}
.huehead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.hueTitle{font-size:13px;font-weight:900;letter-spacing:1.6px;color:var(--acc)}
.huestate{font-size:11px;color:var(--mut);font-weight:700;margin-left:auto;text-align:right}
.huetoggle{min-width:56px;min-height:44px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--mut);font-size:11px;font-weight:900;letter-spacing:1px;cursor:pointer;transition:.15s}
.huetoggle:active{transform:scale(.95)}
.huetoggle.on{background:rgba(var(--g1),.16);border:1px solid var(--acc);color:#cfe9ff;box-shadow:0 0 14px rgba(var(--g1),.3)}
.huerow{display:flex;align-items:center;gap:10px}
.huerow input[type=range]{flex:1 1 auto;min-width:0}
.huelbl{font-size:12px;color:var(--mut);font-weight:800;width:46px;flex:none}
.huerow input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:44px;border-radius:4px;outline:none;background:transparent;margin:0}
.huerow input[type=range]::-webkit-slider-runnable-track{height:10px;border-radius:6px;
  background:linear-gradient(90deg,#9ecbff,#fff2d6,#ffb86b);box-shadow:inset 0 2px 4px rgba(0,0,0,.55),0 1px 0 rgba(255,255,255,.05)}
.huerow input[type=range]::-moz-range-track{height:10px;border-radius:6px;
  background:linear-gradient(90deg,#9ecbff,#fff2d6,#ffb86b);box-shadow:inset 0 2px 4px rgba(0,0,0,.55)}
/* Brightness is a dimmer, not a colour: keep the two tracks visually distinct. */
#huebri::-webkit-slider-runnable-track{background:linear-gradient(90deg,#14171c,#4a5058,#e8ecf2)}
#huebri::-moz-range-track{background:linear-gradient(90deg,#14171c,#4a5058,#e8ecf2)}
.huerow input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:20px;height:28px;margin-top:-10px;border-radius:5px;cursor:pointer;
  background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,0) 40%),linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);
  border:1px solid #05070a;box-shadow:0 3px 8px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.14)}
.huerow input[type=range]::-moz-range-thumb{width:20px;height:28px;border:1px solid #05070a;border-radius:5px;cursor:pointer;
  background:linear-gradient(180deg,#2b3139,#14181d 60%,#0b0e12);box-shadow:0 3px 8px rgba(0,0,0,.7)}
.huerow input[type=range]:focus-visible{box-shadow:0 0 0 3px rgba(251,191,36,.5);border-radius:8px}
.hueval{font-size:12px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--fg);min-width:4.5ch;text-align:right;flex:none}
.huepresets{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}
.huepresets button{display:flex;flex-direction:row;align-items:center;justify-content:center;gap:4px;line-height:1.2;white-space:nowrap}
.huepresets small{font-size:10.5px;font-weight:900;opacity:.9;font-variant-numeric:tabular-nums}
.huepresets button{min-height:44px;border-radius:9px;border:1px solid #23272e;background:#0e1013;color:#b6bdc9;font-size:11px;font-weight:900;letter-spacing:.6px;cursor:pointer;transition:.12s}
.huepresets button:active{background:linear-gradient(160deg,#fbbf24,#f59e0b);border:none;color:#241a00}
/* Phones: four 72px cells cut "昼光色 6500K" (scrollWidth > clientWidth) and
   the label touched both borders. 2x2 after the base rule so it wins. */
@media (max-width:420px){
  .huepresets{grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}
  .huepresets button{flex-direction:row;gap:5px;font-size:10.5px;letter-spacing:0;padding:2px 6px;min-width:0}
  .huepresets small{font-size:10px}
}

/* misc */
.err{color:var(--err);font-size:12px;min-height:0}
.note{position:fixed;left:50%;transform:translateX(-50%);top:calc(10px + env(safe-area-inset-top));background:var(--warn);color:#241a00;font-size:12px;font-weight:700;padding:8px 16px;border-radius:20px;box-shadow:0 6px 20px rgba(0,0,0,.5);z-index:50;display:none;max-width:90vw;text-align:center}
.note.show{display:block}

/* text size */
html[data-size="lg"] .title{font-size:21px}
html[data-size="lg"] .meta{font-size:14px}
html[data-size="lg"] .nowsrc{font-size:13px}
html[data-size="lg"] .cval{font-size:22px}
html[data-size="lg"] .src button,html[data-size="lg"] .row button{font-size:14px}
html[data-size="lg"] .tab{font-size:12px}

/* narrow phones: 2-col decks, touch-safe targets, scroll allowed */
@media (max-width:470px){
  .console{display:flex;flex-direction:column}
  .consolehead{order:0}
  .xfwrap{order:1;margin-top:2px}
  .decks{order:2}
  .decks{grid-template-columns:1fr 1fr;gap:12px}
  .deckcard{padding:7px;gap:5px;border-radius:12px}
  .platter{max-width:210px}
  .dcTitle{font-size:11px}
  .dwave{height:36px}
  .pads{grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}
  .pad{min-height:44px;min-width:44px;font-size:12px;border-radius:9px}
  .dcSrc{gap:5px}
  .dcSrc button{min-height:44px;font-size:11px}
  .dcYt input,.dcYt button{min-height:44px;font-size:12px}
  .dcPlay{width:46px;height:46px;font-size:15px}
  .dcCue{width:46px;height:46px;font-size:10px}
  .dcPfl{min-height:44px;min-width:44px;font-size:11px;padding:4px 12px}
  .dcName{font-size:12px;letter-spacing:1px}
  .dcState{font-size:11px}
  .hero{padding:8px 10px;gap:10px;border-radius:14px}
  .artwrap{width:52px;height:52px}
  .title{font-size:14px}
  .meta{display:none}
  .nowsrc{display:none}
  .seekwrap{margin-top:4px}
  .times{font-size:11px}
  .transport{gap:12px;padding:2px 0}
  .transport .tbtn{width:44px;height:44px}
  #playbtn{width:56px;height:56px}
  .consolehead{gap:6px}
  .consolehead .mixstate{display:none}
}
@media (max-width:380px){
  /* 360px cannot hold 4 text buttons + 3 icons; drop the least-used icons
     rather than showing half of one. */
  .decks{grid-template-columns:1fr;gap:10px}
  .pads{grid-template-columns:repeat(4,minmax(0,1fr))}
  /* 360 keeps the near-square pads; only the narrowest phones need this. */
  .dcPadBody{gap:8px}
  .mixer{gap:5px;padding:10px 5px 8px}
  /* 360: the same 3px clearance between the silk label and the printed ring. */
  .knobchan{padding:5px 3px 5px;gap:11px}
  .rknob{width:62px;height:62px}
  .knob-cap{width:30px;height:30px;margin:-15px 0 0 -15px}
  .knob-ptr::after{top:14px;height:11px}
}

/* phone landscape: DJ posture — 4 decks in a row, compact top */

/* iPad / wide: two-column console, full-bleed */
/* Real FLX4 pads are near-square: give the two rows more height once the
   deck is wide enough that it does not push the mixer out of view. */
@media (min-width:1100px){
  /* 1:0.85 keeps every width's pads the same shape, but at 1440 the 440px
     deck made them 104x88 and pushed the console 21px below a 900px fold.
     Cap the pad block at 88px cells (4*88+3*8) and centre it. */
  .pads{grid-auto-rows:auto;max-width:376px}}
@media (min-width:760px){
  main{display:contents}
  .app{display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:min-content;gap:14px;
    align-items:start;padding:12px max(12px,env(safe-area-inset-left)) 16px max(12px,env(safe-area-inset-right))}
  .top{grid-column:1 / -1}
  .hero{grid-column:1 / -1}
  .wave{grid-column:1 / -1;height:80px}
  .dwave{height:36px}
  .transport{grid-column:1 / -1}
  .flx4{grid-column:1 / -1}
  /* 760-1099px is a single console row, so the jog can use the height it has
     instead of sitting small in a wide empty row (820px was 250px in 758px). */
  .platter{max-width:min(330px,34vh)}
  .dcJogRow{max-height:360px}
  /* Source tabs span the full width: leaving them in a half column left a
     large empty panel beside them on wide screens. */
  .tabs{grid-column:1 / -1}
  /* Sampler + looper fill the row instead of leaving the right half blank. */
  .sampler{grid-column:1 / -1;display:grid;grid-template-columns:minmax(0,1fr);
    grid-template-rows:auto auto auto;row-gap:10px;align-items:start}
  .smpHead{grid-column:1;grid-row:1}
  /* 8 pads over the full left column: 4x2 squares used to stop at 472px and
     leave a 400px hole; 8 columns keeps them pad-sized and fills the row. */
  .padgrid{grid-column:1;grid-row:2;grid-template-columns:repeat(8,minmax(0,1fr))}
  .spad{min-height:0;aspect-ratio:1}
  .looper{grid-column:1;grid-row:3;padding-top:10px;border-top:1px solid var(--line);border-left:0;padding-left:0;
    align-self:stretch;justify-content:flex-start;gap:10px}
  .loopBtns{grid-template-columns:repeat(5,1fr);gap:8px;flex:0 0 auto;grid-auto-rows:minmax(48px,54px)}
  .loopBtns #loopClear{grid-column:auto}
  .loopBtns button{min-height:48px;font-size:12px}
  .loopMeter{display:block}
  .djrow{grid-column:1 / -1}
  .hue{grid-column:1 / -1}
  .err{grid-column:1 / -1}
}
/* Wide screens have room for pads + looper side by side; 760-1099px does not
   (the looper column floated with ~170px of air), so it goes underneath there. */
@media (min-width:1100px){
  /* Looper beside the pads, wide enough for 5 buttons in one row (a 340px
     column made them 58px and pushed 消去 onto its own row). */
  .sampler{grid-template-columns:minmax(0,1fr) minmax(400px,440px);
    grid-template-rows:auto auto;column-gap:16px;row-gap:0}
  .smpHead{grid-column:1;grid-row:1}
  .padgrid{grid-column:1;grid-row:2}
  .looper{grid-column:2;grid-row:1 / span 2;padding-top:0;border-top:0;border-left:1px solid var(--line);
    padding-left:14px;justify-content:flex-start}
  .loopBtns{grid-template-columns:repeat(5,1fr);grid-auto-rows:minmax(48px,54px)}
  .loopBtns #loopClear{grid-column:auto}
}
/* One column (<=819px): the desktop side seam has no top/bottom frame here, so
   it read as two stray vertical lines. Must come after the base .mixer rule —
   placed earlier, the base border-left/right simply won. */
@media (max-width:819px){
  .mixer{border-left:0;border-right:0;border-top:1px solid #1c2026;border-bottom:1px solid #1c2026}
}
/* Must come after the min-width:760 block: that block sets the desktop
   knob/jog sizes and would otherwise win at 820px. */
@media(min-width:820px) and (max-width:860px){
  /* Tablet: A | MIX | B on one row like the hardware. Stacking put the two
     live decks ~2 screens apart; spanning the mixer left a 350px hole. */
  /* SYNC above the fader in a 48px column: side by side it ate 94px and the
     jog shrank to 149px with dead space above and below. */
  .dcSide{flex-direction:column;width:58px;gap:6px}
  /* 9px on one line was unreadable in a dark booth. Stack the unit's two
     silk-print words at 10px: SYNC is 28px wide inside the 58px box. */
  .dcSync{min-width:58px;width:58px;padding:0;font-size:10px;letter-spacing:0}
  .dcTempoWrap{width:58px;--tlen:clamp(150px,20vh,190px)}
  /* The deck column is 285px wide, so the platter tops out at ~197px whatever
     the max-width says. Sizing the row to the platter (197+16) instead of
     345px removes the ~74px of air the taller row added above and below it. */
  .platter{max-width:276px}
  /* 8px left the printed scale 2px from the platter rim. */
  .dcJogRow{max-height:214px;gap:12px}
  .dcTempoWrap{--tlen:clamp(150px,26vh,190px)}
  .dcSide{width:58px}
  /* The unit's silk print is two words; stacking them keeps 10px type in a
     58px column (one line needed 8.5px, which was unreadable). */
  .dcSync{min-width:58px;width:58px;padding:0;font-size:10px;letter-spacing:0}
  .dcTempoWrap{width:58px}
  .platter{max-width:240px}
  .dcPadBody{flex:0 0 auto;min-height:0}
  .dcPadBody .pads{grid-auto-rows:auto;align-content:start}
  /* 46x39 was under the 44px touch minimum. 44px transport + 6px gap +
     4px pad gap gives 48.75px wide, so 44px tall is 1:0.9 — close to 1:0.85. */
  .dcPadBody{gap:4px}
  .dcCue,.dcPlay{width:44px;height:44px;font-size:10px}
  .pads{gap:4px}
  /* aspect-ratio forced 51.8px into a 43px column (8.5px overflow). With the
     tighter gaps the cells are 44.5px, so the 44px touch target still holds. */
  .dcPadBody .pad{min-height:44px;aspect-ratio:auto}
  .pad.empty .padt{font-size:14px}
  /* space-between spread the height slack into the gaps (5px here, 38px
     there). Keep the gaps fixed and let the jog row absorb the slack. */
  .deckcard{justify-content:flex-start}
  /* The deck had 184px more content height than the mixer, all of it pooling
     above and below the jog. Narrow the mixer cell (it only needs 186px) and
     shorten the faders so the deck's own height meets the mixer's. */
  .flx4-body{grid-template-columns:1fr minmax(170px,186px) 1fr}
  /* The deck column is 285px wide, so its content is ~609px tall whatever the
     mixer does. A 736px mixer therefore left 127px of dead plate under
     SOURCE. Shorten the faders so the mixer meets the deck's own height. */
  .dcJogRow{flex:0 0 auto;max-height:none}
  .flx4-body{grid-template-columns:1fr minmax(224px,238px) 1fr;grid-template-rows:auto;gap:8px}
  #dA{grid-column:1;grid-row:1;order:0}
  .flx4-mix{grid-column:2;grid-row:1;order:0;width:auto;max-width:none;margin:0}
  #dB{grid-column:3;grid-row:1;order:0}
  #dC,#dD{grid-column:1 / -1;order:0}
  .pads{gap:4px}
  .pad{font-size:11px;border-radius:8px}
  .dcLoopRow{gap:4px}
  .dcLoop{font-size:10.5px}
  .dcLoopSub{font-size:9.5px}
  /* 820: four 54px cells cannot hold "BEAT JUMP" at 10px, and an ellipsis on
     a mode name is worse than 0.5px less type. */
  .pmode{font-size:10px;letter-spacing:-.35px;padding:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* 4 cells at gap:5 left "BEAT JUMP" 0.09px of room and it clipped at 2x. */
  .dcPadModes{gap:2px}
  .xfader-rail{left:14px;right:14px}
  /* Two knobs need ~70px; a third of a 200px mixer is 64px. Widen the HP cell
     instead of stacking (stacking left HP 48px taller than SMART CFX/BEAT FX). */
  .mixFxRow{grid-template-columns:1.34fr 1fr 1fr;gap:3px}
  .fxknobs{gap:14px;padding-bottom:8px}
  .fxk{width:32px;height:32px}
  .mixFx{padding:3px 1px 7px;min-height:0}
  .mixFx b{white-space:nowrap;text-align:center;line-height:1.2;letter-spacing:-.4px;font-size:10px;padding:0}
  .mixFx .fxLong{display:inline}
  .fxk[data-l]::before{font-size:10px;margin-top:5px}
  .fxknobs{padding-bottom:13px}
  /* Equal thirds put MIX 7px from LEVEL, so the pair read as one word.
     1.5fr for HP buys ~24px of separation; the single-knob cells still hold
     their 10px labels. */
  .mixFxRow{grid-template-columns:1.34fr 1.12fr 1fr;gap:4px}
  .fxk{width:26px;height:26px}
  /* Each printed ring reaches 4px past its dial, so an 8px gap made the two
     HP scales touch. 14px leaves 6px of air between the rings. */
  .fxknobs{gap:14px}
  .mixFx b{font-size:9px;letter-spacing:-.45px}
  /* Shorter faders keep the mixer and the decks the same height here. */
  .chstrips{--flen:clamp(140px,18vh,190px)}
  .rknob{width:48px;height:48px}
  .knob-cap{width:26px;height:26px;margin:-13px 0 0 -13px}
  .cval{font-size:16px}
  /* 9px range text was unreadable on the 200px mixer. */
  .cval small{display:block;font-size:10px;margin-top:2px;letter-spacing:0}
  .knobrow{gap:2px}
  .mixer{padding:8px 6px 8px}
  .loadRow{grid-template-columns:1fr 24px 1fr}
  .loadBtn{font-size:9px;min-height:30px}
  .loadKnob{width:22px;height:22px}
  /* The "未出力" badge overflowed the 200px mixer into deck B. Let the head
     wrap and drop the wide letter-spacing so it stays inside the column. */
  .mixHead{flex-wrap:wrap;letter-spacing:1px;row-gap:2px}
  .mixHeadSub{letter-spacing:.4px;gap:4px;flex-wrap:wrap;white-space:normal}
  .mixHeadSub i{font-size:10px;padding:1px 5px}
  .chstrips{--flen:clamp(118px,11vh,132px)}
  .dcSide{--tlen:clamp(140px,24vh,220px)}
}
@media (max-width:640px){
  html,body{overflow-x:hidden}
  /* env(safe-area-inset-*) is 0 on most phones, so 停止 sat flush on the edge
     and its right border was clipped. */
  .app{padding-left:max(10px,env(safe-area-inset-left));padding-right:max(10px,env(safe-area-inset-right))}
  /* A phone cannot show 7 header buttons; the three icon toggles are desktop
     conveniences and were being cut in half. */
  /* The three icon toggles move into the "…" popover: 8 buttons in one row
     overflowed 360px by 60px. */
  .topbtns #themebtn,.topbtns #qrbtn,.topbtns #sizebtn{display:none}
  .morebtn{display:flex}
  /* Phone: A -> MIX -> B, the hardware's left-to-right hand flow. The mixer is
     compressed so DECK A's jog still lands on the first screen. */
  #dA{order:1}
  .flx4-mix{order:2}
  #dB{order:3}#dC{order:4}#dD{order:5}
  /* Phone: the whole mixer is compressed so DECK A's jog still breaks into
     the first screen — a controller you cannot see is not a controller. */
  .chstrips{--flen:clamp(122px,15.5vh,150px);padding:4px 4px 0;gap:8px}
  .chstrip{gap:6px}
  .chCue{min-height:36px;min-width:44px;font-size:9px}
  .masterSec{padding:10px 2px 0}
  .knobrow{gap:4px}
  .rknob{width:44px;height:44px}
  .knob-cap{width:23px;height:23px;margin:-11.5px 0 0 -11.5px}
  .cval{font-size:15px}
  .cval small{display:block;font-size:8.5px;margin-top:1px;letter-spacing:0}
  .klabel{font-size:9px}
  .loadRow{grid-template-columns:1fr 26px 1fr;margin-bottom:0}
  .loadBtn{min-height:28px;font-size:9px}
  .loadKnob{width:24px;height:24px}
  .mixer{gap:6px;padding:8px 6px 6px}
  .mixHead{font-size:10px}
  .xfwrap{padding:2px 2px 0}
  .xfader-track{height:38px}
  .xflabels{font-size:9px}
  /* "曲間クロスフェード" wrapped with a lone ド on line 2 at 390 too. */
  .djbtn small{font-size:9.5px;letter-spacing:0;white-space:nowrap}
  /* The track was display:none here, so the thumb sat on top of the "1s"
     label. Show the track and put the thumb on it. */
  .loopMeter{display:block;height:8px;margin-bottom:4px}
  .loopMeterWrap::before{height:8px}
  .loopThumb{top:-4px;width:16px;height:18px}
  .loopMeterWrap{--tw:16px}
  .hintMore{display:inline}
  /* Jog sized so the whole platter fits the first phone screen. */
  .dcJogRow{max-height:230px;gap:10px;justify-content:center;position:relative}
  /* The tempo column took 52px out of the row, so the jog sat 30px left of
     centre. Park the column on the right edge and centre the jog in the row. */
  .dcSide{position:absolute;right:0;top:50%;transform:translateY(-50%)}
  .platter{max-width:200px;margin:0 auto}
  /* SYNC sits above the pitch fader; the row must hold 34 + 6 + (tlen+18)
     or the stack overflows upward into the LIVE band. */
  /* The jog row is 200px tall (platter max-width 200). SYNC 34 + gap 6 +
     fader 158 = 198, so the pitch column can no longer spill 15px into the
     waveform above and the title below. */
  .dcSide{--tlen:clamp(96px,17vh,142px);flex-direction:column;gap:6px;height:auto;align-items:center;justify-content:center}
  .dcTempoWrap{height:calc(var(--tlen) + 16px);width:52px}
  .dcSync{min-height:34px;min-width:52px;font-size:9px}
  .chstrips{--flen:clamp(122px,15.5vh,150px)}
  .klabel{font-size:9px}
  .loadRow{grid-template-columns:1fr 28px 1fr}
  .loadBtn{min-height:30px}
  .loadKnob{width:26px;height:26px}
  .xfwrap{padding:4px 2px 0}
  .xfader-track{height:40px}
  .huesync{padding:0 10px;font-size:11px;letter-spacing:0}
  .mixHeadSub i{margin-left:6px}
  .app{padding-left:max(12px,env(safe-area-inset-left));padding-right:max(12px,env(safe-area-inset-right))}
  /* One header row: wrapping to two rows ate ~30% of a phone screen and
     pushed the jog off the first view. The button strip scrolls sideways. */
  .top{flex-wrap:nowrap;row-gap:0;gap:6px;align-items:center}
  .brand{flex:1 1 auto;min-width:0}
  .brandtxt small{display:none}
  /* Keep the room name: without it a phone cannot tell which Sonos it drives. */
  .brandtxt{display:flex;min-width:0;max-width:none}
  .brandtxt #name{font-size:11px;letter-spacing:0}
  /* Title on its own line, then one row of controls: 操作/自動/同期/停止/….
     The three icon toggles live in the "…" popover instead of a second row. */
  .top{flex-wrap:wrap;position:relative;row-gap:6px}
  .brand{flex:1 1 100%}
  .topbtns{flex:1 1 100%;justify-content:flex-start;flex-wrap:nowrap;overflow:visible}
  .moremenu{display:none;position:absolute;right:0;top:100%;margin-top:6px;z-index:30;
    gap:6px;padding:8px;border-radius:14px;background:#12151c;border:1px solid #2a3040;
    /* A huge spread shadow acts as a scrim: the live badge used to show
       through, half-cut, under the panel. */
    box-shadow:0 12px 30px rgba(0,0,0,.75),0 0 0 100vmax rgba(0,0,0,.86)}
  .moremenu.open{display:flex}
  /* The menu is the only route to these on a phone, where title= never shows. */
  .mico{flex-direction:column;gap:2px;height:auto;min-height:56px;padding:6px 4px}
  .mico small{font-size:9px;font-weight:800;letter-spacing:.2px;color:#aab2c2;line-height:1}
  /* 入 / ノブで照明 / 音に合わせる were three different widths on three rows.
     Title + state on row 1, then three equal buttons. */
  .huehead{display:grid;grid-template-columns:repeat(3,1fr);column-gap:6px;row-gap:8px}
  .hueTitle{grid-column:1 / 3;grid-row:1;align-self:center}
  .huestate{grid-column:3;grid-row:1;margin-left:0;text-align:right;align-self:center}
  .huetoggle{grid-column:1;grid-row:2;justify-self:stretch;min-width:0;width:100%}
  #huesync{grid-column:2;grid-row:2;justify-self:stretch;min-width:0;width:100%;padding:0 4px}
  #knobhue{grid-column:3;grid-row:2;justify-self:stretch;min-width:0;width:100%;padding:0 4px}
  /* Every header control keeps its natural size and never shrinks below a
     tap target; the strip scrolls instead of squeezing buttons to 22px. */
  .topbtns>*{flex:0 0 auto;min-height:44px;white-space:nowrap}
  /* 停止 is the panic button and 同期再生 is the main action: both stay at the
     left edge so they are never scrolled out of reach on a phone. The icon
     row scrolls to the right. */
  /* Same left-to-right order as desktop: 操作 → 自動 → 同期 → 停止. */
  .topbtns .stopbtn{order:0}
  .topbtns .syncbtn{order:0}
  .topbtns .bankbtn{order:0}
  .topbtns .icobtn{order:0}
  .syncbtn .lblLong{display:none}
  .syncbtn .lblShort{display:inline}
  .topbtns{gap:4px}
  /* The four text buttons must fit before the icon strip: 390px only has
     ~264px for them, and a half-cut 手動 looked broken. */
  .topbtns .bankbtn,.topbtns .mixmode,.topbtns .syncbtn{padding:0 7px;font-size:10px;letter-spacing:0}
  .topbtns .bankbtn{padding:0 6px;font-size:10px}
  .topbtns .stopbtn{padding:0 8px;font-size:10px;gap:4px}
  .topbtns .stopbtn svg{width:14px;height:14px}
  .topbtns .icobtn{min-width:44px}
  .topbtns .stopbtn{margin-right:0}
}
@media (max-width:380px){
  /* 360x800: keep the fader long enough to ride; the jog still fits. */
  .chstrips{--flen:clamp(130px,16vh,150px);padding-top:2px}
  .masterSec{padding:6px 2px 0}
  .rknob{width:44px;height:44px}
  .knob-cap{width:22px;height:22px;margin:-11px 0 0 -11px}
  .cval{font-size:13px}
  .dcJogRow{max-height:160px}
  .platter{max-width:150px}
  /* 360px: SYNC stays above the fader like the hardware; the fader is shortened
     so sync 36 + gap 6 + fader+16 fits the jog row. */
  .dcSide{--tlen:clamp(96px,18vh,142px);flex-direction:column;gap:6px;align-items:center;justify-content:center}
  .dcTempoWrap{height:calc(var(--tlen) + 16px);width:44px}
  .dcSync{min-height:36px;min-width:44px;padding:0 4px;font-size:9px}
  .dcJogRow{max-height:200px}
  .platter{max-width:200px;margin:0}
  .djbtn small{font-size:9px}

/* 320px only: must come after the 380 block, which sets .platter to 200px. */
@media (max-width:340px){
  /* 320px: 4 x 51.8px + 3 gaps needed 222px in a 216px row (1.5px spill).
     Let the column set the width and keep the 44px touch height. */
  .pads{gap:4px}
  .dcPadBody .pad{aspect-ratio:auto}
  /* The 200px jog centred in a 272px row ran 8px under the absolutely
     positioned pitch column, so the printed scale landed on the rim. */
  .platter{max-width:calc(100% - 108px)}
}

  /* 360px: keep both toggles half-width on one row so the sliders stay close. */
  .huehead{row-gap:6px}
  .huesync{padding:0 8px}
}
@media (min-width:1100px){
  .app{max-width:1360px;margin:0 auto}
  .hero{padding:12px 16px;gap:14px}
  .artwrap{width:96px;height:96px}
  .title{font-size:19px}
  .transport{gap:14px}
  .flx4-body{grid-template-columns:1fr minmax(300px,30%) 1fr}
  .platter{max-width:340px}
  .dcJogRow{max-height:330px}
}
@media (max-height:920px) and (min-width:861px){
  .flx4-body{padding:4px 0 6px}
  .deckcard{padding:8px 12px;gap:6px}
  .platter{max-width:340px}
  /* 366 = sync 44 + gap 10 + tempo (288+18) + 6 slack. The unit's pitch fader
     runs nearly the full jog height; at 340 it was only 55% of the diameter. */
  .dcJogRow{max-height:366px}
  /* sync 44 + gap 10 + tempo(tlen+18) must stay inside the jog row. */
  .dcTempoWrap{--tlen:clamp(150px,36vh,288px)}
  .chstrips{--flen:clamp(150px,22vh,240px)}
  .dwave{height:38px}
}
/* 861-1099px: the deck column is wide enough to stack SYNC above the fader,
   which frees 46px for the jog (1024 was a 225px jog in a 340px row). */
@media(min-width:861px) and (max-width:1099px){
  .dcSide{flex-direction:column;width:58px;gap:6px}
  /* This block comes after the 820-860 one, so it overrode the 11px there:
     1024 kept an 8.5px "BEAT SYNC". Same stacked 10px silk print. */
  .dcSync{min-width:58px;width:58px;padding:0;font-size:10px;letter-spacing:0}
  /* The 920px-tall rule above set --tlen to 36vh (276px at 768), so
     44 + 10 + 294 overflowed the 285px row by 30px and SYNC ran into the
     LIVE band. Cap it to what the row actually holds. */
  /* --tlen is set directly on .dcTempoWrap by the short-viewport rule above,
     so overriding it on .dcSide had no effect. 44 + 10 + (213+18) = 285. */
  .dcTempoWrap{width:58px;--tlen:clamp(150px,26vh,213px)}
  .platter{max-width:280px}
  .dcJogRow{max-height:285px}
  /* 1024 kept the 9.5px/.3px label and left "SMART CFX" 5.4px from the frame,
     while 820 had 7.8px. Same type here so the two tablet widths match. */
  .mixFx b{font-size:9px;letter-spacing:-.45px}
  /* The deck stretches to the mixer's height; spread the slack between rows
     instead of leaving one 70px hole under SOURCE. */
  .deckcard{justify-content:space-between}
}

@media (orientation:landscape) and (max-height:520px){
  .app{display:flex;flex-direction:column;gap:6px;padding:6px max(6px,env(safe-area-inset-left)) 8px max(6px,env(safe-area-inset-right))}
  .top{order:0}
  .hero{order:1;padding:4px 10px;gap:10px;border-radius:12px}
  .artwrap{width:40px;height:40px}
  .title{font-size:13px}
  .meta,.nowsrc{display:none}
  .seekwrap{margin-top:3px}
  .transport{order:2;gap:10px;padding:0}
  .transport .tbtn{width:44px;height:44px}
  #playbtn{width:44px;height:44px}
  .flx4{order:3}
  .flx4-top{padding:6px 10px}
  .flx4-body{padding:8px;gap:8px;grid-template-columns:1fr minmax(170px,210px) 1fr}
  .tabs{order:5}
  .djrow{order:6}
  .hue{order:8}
  .err{order:9}
  .consolehead{flex-wrap:nowrap}
  .consolehead .mixstate{display:none}
  .deckcard{padding:5px;gap:4px;border-radius:10px}
  .platter{max-width:180px}
  .dcTitle{font-size:11px}
  .dwave{height:36px}
  .pads{grid-template-columns:repeat(4,minmax(0,1fr));gap:4px}
  .pad{min-height:44px;min-width:44px;font-size:11px;border-radius:7px}
  .dcSrc{gap:3px}
  .dcSrc button{min-height:44px;font-size:11px}
  .dcYt input,.dcYt button{min-height:44px;font-size:11px}
  .dcPlay{width:44px;height:44px;font-size:14px}
  .dcCue{width:44px;height:44px;font-size:10px}
  .dcPfl{min-height:44px;min-width:44px;font-size:11px;padding:2px 8px}
  .dcName{font-size:11px;letter-spacing:1px}
  .dcState{font-size:11px}
  .xfwrap{margin-top:4px}
}
</style>
</head>
<body>
<div class="offline" id="offline"><span id="offlinemsg">Sonosに接続できません。再試行しています…</span></div>
<div class="app">
  <header class="top">
    <div class="brand"><span class="dot" id="dot"></span><span class="brandtxt"><span id="name">Sonos</span><small>__BRAND__ DJ</small></span></div>
    <div class="topbtns">
      <button class="bankbtn" id="bankbtn" onclick="toggleBank()" title="中央のコントローラーが操作するデッキを切り替えます" aria-label="操作するデッキを切り替え">A/B 操作</button>
      <button class="mixmode" id="mixmode" onclick="toggleMixMode()" title="自動クロスフェードの入／切" aria-label="自動クロスフェード切替" aria-pressed="true">自動XF</button>
      <button class="syncbtn" id="syncbtn" onclick="togglePhoneSync()" title="このiPhoneをSonosと同じタイミングで再生します" aria-label="iPhoneをSonosと同期して再生" aria-pressed="false"><span class="lblLong">この端末で同期再生</span><span class="lblShort">同期再生</span></button>
      <button class="stopbtn" id="stopallbtn" onclick="stopAll()" aria-label="すべて停止"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg><span>停止</span></button>
      <button class="icobtn" id="themebtn" onclick="toggleTheme()" aria-label="ライト／ダーク切替" title="ライト／ダーク切替"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 000 18z" fill="currentColor" stroke="none"/></svg></button>
      <button class="icobtn qrbtn" id="qrbtn" onclick="toggleQr()" aria-label="スマホで開くQRコード" title="スマホで開くQRコード" aria-expanded="false" aria-controls="qrpanel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14h1M14 20h1M18 18h3v3h-3z"/></svg></button>
      <button class="icobtn" id="sizebtn" onclick="toggleSize()" aria-label="文字サイズ" title="文字サイズ" style="font-weight:800;font-size:14px">Aa</button>
      <button class="morebtn" id="morebtn" onclick="toggleMore()" aria-label="その他の設定" aria-expanded="false" aria-controls="moremenu">…</button>
    </div>
    <div class="moremenu" id="moremenu">
      <button class="icobtn mico" onclick="toggleTheme()" aria-label="ライト／ダーク切替"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 000 18z" fill="currentColor" stroke="none"/></svg><small>表示</small></button>
      <button class="icobtn mico" onclick="toggleQr()" aria-label="スマホで開くQRコード"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14h1M14 20h1M18 18h3v3h-3z"/></svg><small>QR</small></button>
      <button class="icobtn mico" onclick="toggleSize()" aria-label="文字サイズ" style="font-weight:800;font-size:15px">Aa<small>文字</small></button>
    </div>
  </header>

  <section class="qrpanel" id="qrpanel" aria-label="スマホで開くQRコード">
    <div class="qrhead">
      <span class="qrTitle">スマホで開く</span>
      <button class="qrclose" onclick="toggleQr()" aria-label="QRコードを閉じる">&times;</button>
    </div>
    <img class="qrimg" id="qrimg" alt="このダッシュボードを開くQRコード" width="240" height="240" loading="lazy">
    <div class="qrurl" id="qrurl">—</div>
    <div class="qrhint">カメラで読み取ると、この画面がスマホで開きます。<br>同じWi-Fiにつないでください。</div>
  </section>

  <main>
  <h1 class="sr-only">Sonos 4デッキDJミキサー</h1>
  <div class="flx4" id="console">
    <div class="flx4-top">
      <div class="flx4-brand"><span class="flx4-logo">SOLUNA</span><span class="flx4-model">DDJ-FLX4</span></div>
      <span class="mixstate" id="mixstate">DECK A〜D に音源を読み込んでください</span>
    </div>
    <div class="flx4-body">
      <div class="decks" id="decks"></div>
      <div class="flx4-mix">
  <div class="mixer">
    <div class="loadRow">
      <button class="loadBtn" id="loadA" onclick="openLoad('A')" aria-label="DECK A に音源を読み込む">LOAD A</button>
      <span class="loadKnob" aria-hidden="true"><i></i></span>
      <button class="loadBtn" id="loadB" onclick="openLoad('B')" aria-label="DECK B に音源を読み込む">LOAD B</button>
    </div>
    <div class="mixHead"><span>MIXER</span><span class="mixHeadSub" id="mixHeadSub"><span id="mixHeadMain"><b>Sonos</b></span><i id="notsent" role="button" tabindex="0" aria-describedby="notsentwhy" aria-expanded="false" title="再生中ですが、まだSonosへ送られていません">未送信 ⓘ</i><span id="notsentwhy" class="why" hidden><button type="button" class="whyx" id="notsentx" aria-label="閉じる">×</button>再生中ですが、まだSonosへ送られていません。「同期再生」ボタンを押すと配信が始まり、この音量・音質が反映されます。</span></span></div>
    <div class="mixFxRow" aria-hidden="true">
      <span class="mixFx"><b>HP</b><span class="fxknobs"><i class="fxk" data-l="MIX"><span class="fxt"></span><span class="fxg"></span></i><i class="fxk" data-l="LEVEL"><span class="fxt"></span><span class="fxg"></span></i></span></span>
      <span class="mixFx"><b><span class="fxLong">SMART </span>CFX</b><span class="fxknobs"><i class="fxk"><span class="fxt"></span><span class="fxg"></span></i></span></span>
      <span class="mixFx"><b><span class="fxLong">BEAT </span>FX</b><span class="fxknobs"><i class="fxk off" title="SonosにBEAT FX相当の機能はありません" aria-hidden="true"><span class="fxt"></span><span class="fxg"></span></i></span></span>
    </div>
    <div class="masterSec">
    <span class="secLabel">MASTER OUT</span>
    <div class="knobrow">
    <div class="knobchan">
      <div class="rknob" data-kind="volume" data-min="0" data-max="__MAXVOL__" role="slider" aria-label="Volume" tabindex="0">
        <div class="knob-dial"></div>
        <div class="knob-ptr"></div>
        <div class="knob-cap"></div>
        <input type="range" min="0" max="__MAXVOL__" step="1" id="vol" oninput="setv('volume',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">VOL</div>
      <div class="cval" id="v-vol">—</div>
    </div>
    <div class="knobchan">
      <div class="rknob" data-kind="bass" data-min="-10" data-max="10" role="slider" aria-label="Bass" tabindex="0">
        <div class="knob-dial"></div>
        <div class="knob-ptr"></div>
        <div class="knob-cap"></div>
        <input type="range" min="-10" max="10" step="1" id="bass" oninput="setv('bass',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">BASS</div>
      <div class="cval" id="v-bass">—</div>
    </div>
    <div class="knobchan">
      <div class="rknob" data-kind="treble" data-min="-10" data-max="10" role="slider" aria-label="Treble" tabindex="0">
        <div class="knob-dial"></div>
        <div class="knob-ptr"></div>
        <div class="knob-cap"></div>
        <input type="range" min="-10" max="10" step="1" id="treble" oninput="setv('treble',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">TREBLE</div>
      <div class="cval" id="v-treble">—</div>
    </div>
    </div>
    </div>
    <div class="chstrips">
      <div class="chstrip">
        <span class="chName" id="chn1">A</span>
        <button class="chCue" id="chcue1" onclick="deckPfl(chDeckName(1))" aria-pressed="false" title="このチャンネルをヘッドホンで聴く" aria-label="チャンネル1をヘッドホンで試聴">🎧 CUE</button>
        <div class="chfaderRow">
          <div class="chfaderWrap">
            <input type="range" class="chfader" min="0" max="100" value="100" id="chf1" oninput="deckVolByIdx(1,this.value)" aria-label="チャンネル1の音量">
          </div>
          <div class="chmeter" aria-hidden="true"><i id="chm1"></i></div>
        </div>
      </div>
      <div class="chstrip">
        <span class="chName" id="chn2">B</span>
        <button class="chCue" id="chcue2" onclick="deckPfl(chDeckName(2))" aria-pressed="false" title="このチャンネルをヘッドホンで聴く" aria-label="チャンネル2をヘッドホンで試聴">🎧 CUE</button>
        <div class="chfaderRow">
          <div class="chfaderWrap">
            <input type="range" class="chfader" min="0" max="100" value="100" id="chf2" oninput="deckVolByIdx(2,this.value)" aria-label="チャンネル2の音量">
          </div>
          <div class="chmeter" aria-hidden="true"><i id="chm2"></i></div>
        </div>
      </div>
    </div>
        <div class="xfwrap">
          <div class="xflabels"><span id="xfch1">A</span><span>CROSSFADER</span><span id="xfch2">B</span></div>
          <div class="xfader-track">
            <div class="xfader-rail"></div>
            <div class="xfader-cap" id="xfcap"><span></span></div>
            <input type="range" min="0" max="100" value="50" id="xfader" class="xfader" oninput="setXfader(this.value)" aria-label="Crossfader">
          </div>
        </div>
  </div>
      </div>
    </div>
  </div><!-- /console -->

  <div class="sampler" id="sampler">
    <div class="smpHead">
      <span class="smpTitle">サンプラー <span id="smpDeck">A</span></span>
      <div class="smpDeckSel" id="smpDeckSel"></div>
    </div>
    <div class="padgrid" id="padgrid" aria-label="サンプラーパッド"></div>
    <div class="looper" id="looper">
      <div class="loopTitle">ルーパー <span id="loopInfo">未録音</span></div>
      <div class="loopBtns">
        <button id="loopRec" onclick="loopOp('rec')" aria-label="ループ録音">● 録音</button>
        <button id="loopStop" onclick="loopOp('stop')" aria-label="録音を止めてループ">■ 確定</button>
        <button id="loopPlay" onclick="loopOp('play')" aria-label="ループ再生">▶ 再生</button>
        <button id="loopPause" onclick="loopOp('pause')" aria-label="ループ停止">❚❚ 停止</button>
        <button id="loopClear" onclick="loopOp('clear')" aria-label="ループ消去">✕ 消去</button>
      </div>
      <div class="loopMeterWrap">
        <div class="loopMeter" aria-hidden="true"><i id="loopbar"></i></div>
        <span class="loopThumb" id="loopthumb" aria-hidden="true"></span>
        <div class="loopTicks" aria-hidden="true"><span style="--t:0">1s</span><span style="--t:.204">2s</span><span style="--t:.407">4s</span><span style="--t:.611">8s</span><span style="--t:.815">16s</span><span class="tickEnd" style="--t:1">30s</span></div>
      </div>
      <div class="loopBig" id="loopBig" aria-live="off"><b id="loopBigVal">—</b><i>SEC</i></div>
      <div class="loopHint">録音 → 確定 でその場の音がループします。<span class="hintMore">パッドに音源URLを入れると鳴ります。</span></div>
    </div>
  </div>

  <div class="tabs">
    <div class="tabbar">
      <button class="tab on" data-tab="source" onclick="showTab('source')">音源</button>
      <button class="tab" data-tab="radio" onclick="showTab('radio')">ラジオ</button>
      <button class="tab" data-tab="youtube" onclick="showTab('youtube')">YouTube</button>
    </div>
    <div class="tabpanel on" id="tab-source">
      <p class="srchint">音源は LOAD A / LOAD B で各デッキに読み込みます。</p>
      <div class="src">
        <button onclick="act('world')" id="b-world" aria-pressed="false"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg> WORLD</button>
        <button onclick="act('linein')" id="b-linein" aria-pressed="false"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 11h12v2H3zM15 8h2v8h-2zM18 10h3v4h-3z"/></svg> Line-in</button>
      </div>
      <div class="row">
        <button onclick="act('loudness')" id="loudbtn" aria-pressed="false">ラウドネス 切</button>
        <button onclick="act('mute')" id="mute2" aria-pressed="false">ミュート 切</button>
      </div>
      <div class="hint" id="foot"></div>
    </div>
    <div class="tabpanel" id="tab-radio">
      <div class="src">
        <button onclick="act('radio',{key:'groovesalad'})" id="r-groovesalad" aria-pressed="false">Groove Salad</button>
        <button onclick="act('radio',{key:'dronezone'})" id="r-dronezone" aria-pressed="false">Drone Zone</button>
        <button onclick="act('radio',{key:'secretagent'})" id="r-secretagent" aria-pressed="false">Secret Agent</button>
        <button onclick="act('radio',{key:'lush'})" id="r-lush" aria-pressed="false">Lush</button>
      </div>
    </div>
    <div class="tabpanel" id="tab-youtube">
      <div class="ytrow">
        <input type="url" id="yturl" inputmode="url" autocomplete="off" placeholder="YouTubeのURLを貼り付け" aria-label="YouTubeのURL">
        <button onclick="playYt()" id="ytbtn">Play</button>
      </div>
      <div class="ythint" id="ythint">URLを貼り付けると、音声を取り出してSonosで再生します。</div>
    </div>
  </div>

  </main>

  <div class="djrow">
    <button class="djbtn" onclick="toggleShuffle()" id="shufbtn" aria-label="Shuffle" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>
      <small id="shuflbl">シャッフル</small>
    </button>
    <button class="djbtn" onclick="cycleRepeat()" id="repbtn" aria-label="Repeat" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 2l4 4-4 4M3 11V9a4 4 0 014-4h14M7 22l-4-4 4-4M21 13v2a4 4 0 01-4 4H3"/></svg>
      <small id="replbl">リピート</small>
    </button>
    <button class="djbtn" onclick="act('crossfade')" id="xfbtn" aria-label="Crossfade" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8h6l6 8h6M3 16h6l6-8h6"/></svg>
      <small id="xflbl">曲間クロスフェード</small>
    </button>
  </div>

  <div class="hue" id="huepanel">
    <div class="huehead">
      <span class="hueTitle">照明 · Hue</span>
      <span class="huestate" id="huestate">—</span>
      <button class="huetoggle" id="hueon" onclick="hueToggle()" aria-pressed="false" title="照明の点灯／消灯">点灯</button>
      <button class="huesync" id="huesync" onclick="toggleHueSync()" aria-pressed="false" title="照明を音楽の音量に合わせて揺らします">音に合わせる</button>
      <button class="huesync" id="knobhue" onclick="toggleKnobHue()" aria-pressed="false" title="VOLで明るさ・BASSで色温度・TREBLEで彩度を操作します">ノブで照明</button>
    </div>
    <div class="huerow">
      <span class="huelbl">明るさ</span>
      <input type="range" min="1" max="254" step="1" value="140" id="huebri" oninput="hueSet('bri',this.value)" aria-label="明るさ" aria-valuetext="140">
      <span class="hueval" id="v-huebri">—</span>
    </div>
    <div class="huerow">
      <span class="huelbl">色温度</span>
      <input type="range" min="153" max="500" step="1" value="500" id="huect" oninput="hueSet('ct',this.value)" aria-label="色温度（右ほど暖かい）" aria-valuetext="500">
      <span class="hueval" id="v-huect">—</span>
    </div>
    <div class="huepresets">
      <button onclick="huePreset(154)">昼光色<small>6500K</small></button>
      <button onclick="huePreset(200)">昼白色<small>5000K</small></button>
      <button onclick="huePreset(286)">温白色<small>3500K</small></button>
      <button onclick="huePreset(370)">電球色<small>2700K</small></button>
    </div>
    <div class="huehint" id="knobhuehint">ノブ連動：VOL→明るさ ・ BASS→色温度 ・ TREBLE→彩度</div>
  </div>

  <div class="err" id="err" role="alert" aria-live="assertive"></div>
</div>
<div class="note" id="note" role="status" aria-live="polite"></div>
<script>
let st={online:false}, busy=false, timers={}, noteTimer=null, switchUntil=0;
const METER_PEAK={A:0,B:0,C:0,D:0};
const dragging={vol:false,bass:false,treble:false};
const pending={vol:false,bass:false,treble:false};
const busyBtns=['mute2','loudbtn','b-world','b-linein','r-groovesalad','r-dronezone','r-secretagent','r-lush','shufbtn','repbtn','xfbtn','ytbtn'];
function showErr(m){document.getElementById('err').textContent=m||''}
function showNote(m){
  const el=document.getElementById('note');
  el.textContent=m||'';el.className=m?'note show':'note';
  clearTimeout(noteTimer);
  if(m)noteTimer=setTimeout(()=>{el.className='note';el.textContent='';},3500);
}
function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab===name));
  document.querySelectorAll('.tabpanel').forEach(p=>p.classList.toggle('on',p.id==='tab-'+name));
}
// ---- phone playback in step with the speaker -------------------------------
// The speaker is ~5s behind the mixer because it buffers before it plays. A
// phone that simply opens the live stream runs ahead of the room. We ask the
// server where the speaker is *now* in the shared stream and start there.
let phoneAudio=null, phoneSyncing=false;
window.phoneAudio=null;

// The button carries both the long and the short label (CSS picks one), so
// textContent would stack them. Write the two spans instead.
function setSyncLabel(long,short){
  const btn=document.getElementById('syncbtn');
  if(!btn)return;
  const l=btn.querySelector('.lblLong'), s=btn.querySelector('.lblShort');
  if(l)l.textContent=long; if(s)s.textContent=short||long;
  if(!l&&!s)btn.textContent=long;
  btn.setAttribute('aria-label',long);
}

async function togglePhoneSync(){
  const btn=document.getElementById('syncbtn');
  if(phoneAudio&&!phoneAudio.paused){
    phoneAudio.pause();
    btn.setAttribute('aria-pressed','false');
    setSyncLabel('この端末で同期再生','同期再生');
    return;
  }
  setSyncLabel('合わせています…','合わせ中…');
  btn.disabled=true;
  try{
    const r=await fetch('/api/sync');const info=await r.json();
    if(!info.ok){
      showErr(info.error==='Sonos is not playing the mix'
        ? 'Sonosがミックスを再生していません。先にSonosで再生してください。'
        : '同期の準備ができませんでした: '+(info.error||'不明'));
      return;
    }
    if(!phoneAudio){
      phoneAudio=new Audio();window.phoneAudio=phoneAudio;
      phoneAudio.preload='none';
      phoneAudio.addEventListener('ended',()=>{
        btn.setAttribute('aria-pressed','false');
        setSyncLabel('この端末で同期再生','同期再生');
      });
    }
    // Cache-bust so an old buffered stream is not reused.
    phoneAudio.src=info.url+'&t='+Date.now();
    phoneAudio.volume=0.9;
    await phoneAudio.play();
    btn.setAttribute('aria-pressed','true');
    setSyncLabel('同期中 · 停止','同期中');
    // Re-align every 20s: clocks drift and the speaker's buffer moves a little.
    clearInterval(phoneSyncing);
    phoneSyncing=setInterval(async()=>{
      if(!phoneAudio||phoneAudio.paused)return;
      try{
        const rr=await fetch('/api/sync');const ii=await rr.json();
        if(ii.ok&&Math.abs((ii.lag||0))>0){/* server already offsets; nothing to do */}
      }catch(e){}
    },20000);
  }catch(e){
    showErr('この端末で再生できませんでした: '+e.message);
  }finally{
    btn.disabled=false;
  }
}

// ---- lights that move with the music --------------------------------------
let hueSyncTimer=null, hueSyncOn=false, huePrevBri=null;
window.hueSyncState=()=>({on:hueSyncOn});

async function toggleHueSync(){
  const btn=document.getElementById('huesync');
  hueSyncOn=!hueSyncOn;
  btn.setAttribute('aria-pressed',hueSyncOn?'true':'false');
  btn.textContent=hueSyncOn?'音に同期中':'音に合わせる';
  if(!hueSyncOn){
    clearInterval(hueSyncTimer);hueSyncTimer=null;
    if(huePrevBri!=null){
      // A tick may have fired just before the clear; send the restore twice,
      // a moment apart, so the last write wins and the room settles.
      huePost({bri:huePrevBri});
      setTimeout(()=>huePost({bri:huePrevBri}),260);
    }
    return;
  }
  const briEl=document.getElementById('huebri');
  const liveBri=(st.hue&&typeof st.hue.bri==='number')?st.hue.bri:null;
  huePrevBri=liveBri!=null?liveBri:(briEl?parseInt(briEl.value):140);
  const tick=async()=>{
    try{
      const r=await fetch('/api/level');const lv=await r.json();
      // A tick already in flight when the user switched sync off must not
      // land after the restore, or the room stays stuck at the last level.
      if(!hueSyncOn)return;
      const l=Math.max(0,Math.min(1,lv.level||0));
      // Keep it gentle: never below 25% of the user's chosen brightness.
      const base=huePrevBri||140;
      const bri=Math.round(base*(0.35+0.65*l));
      huePost({bri:bri,on:true});
    }catch(e){}
  };
  tick();
  hueSyncTimer=setInterval(tick,180);
}

function toggleTheme(){
  const h=document.documentElement;
  const next=h.getAttribute('data-theme')==='light'?'dark':'light';
  h.setAttribute('data-theme',next);
  try{localStorage.setItem('soluna-theme',next)}catch(e){}
}
let stopping=false;
async function stopAll(){
  if(stopping)return;
  stopping=true;
  const btn=document.getElementById('stopallbtn');
  btn.disabled=true;
  showErr('');showNote('停止しています…');
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd:'stopall'})});
    const j=await r.json();
    if(j.error)showErr('停止できませんでした: '+j.error);
    else showNote('停止しました');
    // The phone player is separate from the speaker; stop it too.
    try{if(window.phoneAudio&&!window.phoneAudio.paused){window.phoneAudio.pause();window.phoneAudio.currentTime=0;}}catch(e){}
    try{if(hueSyncOn)toggleHueSync();}catch(e){}
  }catch(e){
    showErr('停止できませんでした: '+e.message);
  }finally{
    stopping=false;btn.disabled=false;
    refresh();
    setTimeout(()=>showNote(''),2500);
  }
}
let qrLoaded=false;
function toggleQr(force){
  const panel=document.getElementById('qrpanel');
  const btn=document.getElementById('qrbtn');
  const open=force!==undefined?force:!panel.classList.contains('show');
  panel.classList.toggle('show',open);
  btn.setAttribute('aria-expanded',open?'true':'false');
  if(!open)return;
  if(!qrLoaded){
    // The image is fetched once; the server always renders the address this
    // process is reachable on, so it stays correct if the LAN IP changes.
    const img=document.getElementById('qrimg');
    img.src='/qr.svg?t='+Date.now();
    img.onerror=()=>{document.getElementById('qrurl').textContent='QRコードを生成できませんでした';};
    fetch('/api/phone').then(r=>r.json()).then(j=>{
      document.getElementById('qrurl').textContent=j.url||'';
    }).catch(()=>{});
    qrLoaded=true;
  }
  panel.scrollIntoView({block:'nearest',behavior:'smooth'});
}
function toggleSize(){
  const h=document.documentElement;
  const next=h.getAttribute('data-size')==='lg'?'md':'lg';
  h.setAttribute('data-size',next);
  try{localStorage.setItem('soluna-size',next)}catch(e){}
}
(function initPrefs(){
  try{
    const t=localStorage.getItem('soluna-theme');if(t)document.documentElement.setAttribute('data-theme',t);
    const s=localStorage.getItem('soluna-size');if(s)document.documentElement.setAttribute('data-size',s);
  }catch(e){}
})();
function artProxy(u){return u?('/api/art?u='+encodeURIComponent(u)):''}
let lastGlowSrc='';
function applyArtworkColor(src){
  if(!src||src===lastGlowSrc)return;
  lastGlowSrc=src;
  const img=new Image();
  img.onload=()=>{
    try{
      const c=document.createElement('canvas');c.width=c.height=28;
      const ctx=c.getContext('2d');ctx.drawImage(img,0,0,28,28);
      const d=ctx.getImageData(0,0,28,28).data;
      let r=0,g=0,b=0,n=0, vr=0,vg=0,vb=0,vn=0;
      for(let i=0;i<d.length;i+=4){
        const R=d[i],G=d[i+1],B=d[i+2];
        const mx=Math.max(R,G,B),mn=Math.min(R,G,B);
        if(mx<28)continue;
        r+=R;g+=G;b+=B;n++;
        if(mx-mn>45){vr+=R;vg+=G;vb+=B;vn++;}
      }
      if(!n)return;
      const avg=[Math.round(r/n),Math.round(g/n),Math.round(b/n)];
      const viv=vn?[Math.round(vr/vn),Math.round(vg/vn),Math.round(vb/vn)]:avg;
      const root=document.documentElement.style;
      root.setProperty('--g1',avg.join(','));
      root.setProperty('--g2',viv.join(','));
    }catch(e){}
  };
  img.src=src;
}
async function act(cmd,data={}){
  if(busy&&cmd!=='volume'){showNote('Working… try again in a moment');return;}
  busy=true;showErr('');showNote('');
  const srcCmds=['world','linein','radio'];
  if(srcCmds.includes(cmd)){const ns=document.getElementById('nowsrc');if(ns)ns.textContent='Switching…';switchUntil=Date.now()+3000;}
  for(const id of busyBtns.concat(['vol','bass','treble'])){const b=document.getElementById(id);if(b)b.disabled=true;}
  const ctl=new AbortController();
  const to=setTimeout(()=>ctl.abort(),8000);
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,...data}),signal:ctl.signal});
    const j=await r.json();
    if(j.error)showErr('Action failed: '+j.error);
    else if(j.note)showNote(j.note);
  }catch(e){
    showErr(e.name==='AbortError'?'No response — please try again':'Network error: '+e.message);
  }finally{
    clearTimeout(to);busy=false;refresh();
  }
}
let knobHueOn=false, knobHuePrev={bri:null,ct:null,sat:null}, knobHueTimer=null;
window.knobHueState=()=>({on:knobHueOn});
function toggleKnobHue(){
  const btn=document.getElementById('knobhue');
  knobHueOn=!knobHueOn;
  btn.setAttribute('aria-pressed',knobHueOn?'true':'false');
  btn.textContent=knobHueOn?'ノブ連動中':'ノブで照明';
  if(!knobHueOn){
    // Put the lamps back the way they were before the knobs took over.
    const p=knobHuePrev, payload={};
    if(p.bri!=null)payload.bri=p.bri;
    if(p.ct!=null)payload.ct=p.ct;
    if(p.sat!=null)payload.sat=p.sat;
    if(Object.keys(payload).length)huePost(payload);
    knobHuePrev={bri:null,ct:null,sat:null};
    return;
  }
  // Remember the current lamp state so switching off restores it.
  const h=st.hue||{};
  knobHuePrev={bri:typeof h.bri==='number'?h.bri:null,
               ct:typeof h.ct==='number'?h.ct:null,
               sat:typeof h.sat==='number'?h.sat:null};
  // Do not jump the lamps on enable; the first knob turn takes over.
}
function knobHueApply(){
  if(!knobHueOn)return;
  clearTimeout(knobHueTimer);
  // Coalesce rapid knob turns into one light update; the bridge dislikes bursts.
  knobHueTimer=setTimeout(()=>{
    if(!knobHueOn)return;
    const volEl=document.getElementById('vol'), bassEl=document.getElementById('bass'), trEl=document.getElementById('treble');
    const maxVol=parseInt(volEl.max)||40;
    const vol=parseFloat(volEl.value)||0;
    const bass=parseFloat(bassEl.value)||0;   // -10..10
    const tr=parseFloat(trEl.value)||0;       // -10..10
    const bri=Math.round(1+ (vol/maxVol)*253);
    const ct=Math.round(500 - ((bass+10)/20)*(500-153));
    const sat=Math.round(((tr+10)/20)*254);
    huePost({bri:bri,ct:ct,sat:sat,on:true});
  },180);
}
function setv(kind,value){
  const vid={volume:'v-vol',bass:'v-bass',treble:'v-treble'}[kind]||('v-'+kind);
  const lbl=document.getElementById(vid);
  if(lbl)lbl.textContent=value;
  const sid={volume:'vol',bass:'bass',treble:'treble'}[kind];
  if(sid){const sl=document.getElementById(sid);if(sl&&parseFloat(sl.value)!==parseFloat(value))sl.value=value;const kn=document.querySelector('.rknob[data-kind="'+kind+'"]');if(kn)paintKnob(kn);}
  const pk0={volume:'vol',bass:'bass',treble:'treble'}[kind];
  if(pk0)pending[pk0]=true;
  if(knobHueOn&&['volume','bass','treble'].includes(kind))knobHueApply();
  clearTimeout(timers[kind]);
  timers[kind]=setTimeout(()=>{
    const ctl=new AbortController();
    const to=setTimeout(()=>ctl.abort(),8000);
    const label={volume:'Volume',bass:'Bass',treble:'Treble'}[kind]||kind;
    const pk={volume:'vol',bass:'bass',treble:'treble'}[kind];
    if(pk)pending[pk]=true;
    if(lbl)lbl.dataset.sending='1';
    fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:kind,value:parseInt(value)}),signal:ctl.signal})
      .then(r=>r.json()).then(j=>{if(j.error)showErr(label+' change failed: '+j.error)})
      .catch(e=>showErr(e.name==='AbortError'?label+': no response':'Network error: '+e.message))
      .finally(()=>{clearTimeout(to);if(lbl)delete lbl.dataset.sending;if(pk)pending[pk]=false;refresh();});
  },120);
}
function stepv(kind,delta){
  const slider=document.getElementById({volume:'vol',bass:'bass',treble:'treble'}[kind]);
  if(!slider)return;
  let min=parseInt(slider.min),max=parseInt(slider.max);
  let cur=parseInt(slider.value);
  if(isNaN(cur))cur=parseInt(document.getElementById({volume:'v-vol',bass:'v-bass',treble:'v-treble'}[kind]).textContent)||0;
  const nv=Math.max(min,Math.min(max,cur+delta));
  if(nv===cur){return;}
  slider.value=nv;setv(kind,nv);
}
async function deckPost(payload){
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(j.error)showErr('Action failed: '+j.error);
    return j;
  }catch(e){showErr('Network error: '+e.message);return {error:e.message};}
  finally{refresh();}
}
function deckRadio(d,key){deckPost({cmd:'deck',deck:d,kind:'radio',key});}
function deckYt(d){
  const inp=document.getElementById(d+'yt');
  const url=(inp.value||'').trim();
  if(!url){showErr('YouTubeのURLを入力してください');return;}
  deckPost({cmd:'deck',deck:d,kind:'youtube',url});
}
function deckPlay(d){
  const cur=st.mix&&st.mix[d];
  const playing=cur&&cur.playing;
  deckPost({cmd:'deckctl',deck:d,act:playing?'pause':'play'});
}
function deckPfl(d){
  const cur=st.mix&&st.mix[d];
  const on=!!(cur&&cur.pfl);
  deckPost({cmd:'pfl',deck:d,value:!on});
}
// CUE on a real deck: stop and return to the cue point. We use the top of the
// track, and on a live source there is nothing to rewind, so it just pauses.
function deckCue(d){
  const cur=st.mix&&st.mix[d];
  if(cur&&cur.playing)deckPost({cmd:'deckctl',deck:d,act:'pause'});
  deckPost({cmd:'cue',deck:d,slot:0,op:'goto'});
}
let bankValue=0;
function toggleBank(){
  bankValue=bankValue?0:1;
  deckPost({cmd:'bank',value:bankValue});
}
function openLoad(d){
  const more=document.getElementById('more'+d);
  if(!more)return;
  more.open=true;
  document.querySelectorAll('.loadBtn').forEach(b=>b.classList.remove('on'));
  const btn=document.getElementById('load'+d);
  if(btn)btn.classList.add('on');
  more.scrollIntoView({behavior:'smooth',block:'center'});
  const first=more.querySelector('.dcSrc button');
  if(first)first.focus({preventScroll:true});
}
function buildDecks(){
  const wrap=document.getElementById('decks');
  if(!wrap)return;
  const DECKS=['A','B','C','D'];
  const radios=[['groovesalad','Groove'],['dronezone','Drone'],['secretagent','Agent'],['lush','Lush']];
  for(const d of DECKS){
    const card=document.createElement('div');
    card.className='deckcard';card.id='d'+d;
    let srcBtns=radios.map(([k,l])=>`<button onclick="deckRadio('${d}','${k}')" aria-label="DECK ${d} に ${l} を読み込む">${l}</button>`).join('');
    card.innerHTML=
      `<div class="dcHead"><span class="dcName">DECK ${d}</span>`+
        `<span class="dcState" id="d${d}state">停止中</span>`+
        `</div>`+
      `<div class="dcLoopRow" role="group" aria-label="DECK ${d} のループ">`+
        `<button class="dcLoop" onclick="deckLoop('${d}','rec')" id="d${d}loopin" aria-label="DECK ${d} ループイン">IN<span class="dcLoopSub">4BEAT</span></button>`+
        `<button class="dcLoop" onclick="deckLoop('${d}','stop')" id="d${d}loopout" aria-label="DECK ${d} ループアウト">OUT</button>`+
        `<button class="dcLoop" id="d${d}loopexit" onclick="deckLoop('${d}','clear')" aria-label="DECK ${d} リループ／ループ解除">RELOOP<span class="dcLoopSub">EXIT</span></button>`+
      `</div>`+
      `<canvas class="dwave" id="wave${d}" role="img" aria-label="DECK ${d} の波形"></canvas>`+
      `<div class="dcJogRow">`+
        `<div class="platter"><span class="plabel">${d}</span>`+
          `<div class="platterRing" aria-hidden="true"></div>`+
          `<span class="platterMark" aria-hidden="true"></span></div>`+
        `<div class="dcSide">`+
          `<button class="dcSync" id="d${d}sync" onclick="deckSync('${d}')" title="反対側のデッキとテンポをそろえる" aria-label="DECK ${d} ビートシンク"><span class="syncLong">BEAT </span>SYNC</button>`+
          `<div class="dcTempoWrap">`+
            `<input type="range" class="dcTempo" id="d${d}tempo" min="-16" max="16" step="0.5" value="0" oninput="deckTempo('${d}',this.value)" aria-label="DECK ${d} テンポ">`+
            `<span class="dcTempoVal" id="d${d}tempov" title="テンポ変更量">0.0%</span>`+
          `</div>`+
        `</div>`+
      `</div>`+

      `<div class="dcHint" id="d${d}hint"></div>`+
      `<div class="dcTitle" id="d${d}title">曲なし</div>`+
      `<div class="dcPadModes" role="group" aria-label="DECK ${d} のパッドモード">`+
        `<button class="pmode on" aria-pressed="true">HOT CUE</button>`+
        `<button class="pmode disabled" aria-pressed="false" aria-disabled="true" title="未対応です">PAD FX</button>`+
        `<button class="pmode disabled" aria-pressed="false" aria-disabled="true" title="未対応です">BEAT JUMP</button>`+
        `<button class="pmode disabled" aria-pressed="false" aria-disabled="true" title="パッドでは未対応（下のサンプラー欄で操作）">SAMPLER</button>`+
      `</div>`+
      `<div class="dcPadBody">`+
        `<div class="dcTransport">`+
          `<button class="dcCue" onclick="deckCue('${d}')" id="d${d}cue" aria-label="DECK ${d} キュー">CUE</button>`+
          `<button class="dcPlay" onclick="deckPlay('${d}')" id="d${d}play" aria-label="DECK ${d} 再生／一時停止">▶</button>`+
        `</div>`+
        `<div class="pads" id="pads${d}" aria-label="DECK ${d} のホットキュー"></div>`+
      `</div>`+
      `<details class="dcMore" id="more${d}">`+
        `<summary>SOURCE / LOAD</summary>`+
        `<div class="dcSrc">${srcBtns}</div>`+
        `<div class="dcYt"><input type="url" id="d${d}yt" placeholder="YouTube URL を ${d} へ" aria-label="DECK ${d} のYouTube URL"><button onclick="deckYt('${d}')" aria-label="DECK ${d} に読み込む">読込</button></div>`+
      `</details>`;
    wrap.appendChild(card);
  }
}
buildDecks();
let deckTempoTimers={};
function deckTempo(d,v){
  const pct=parseFloat(v)||0;
  const lbl=document.getElementById('d'+d+'tempov');
  if(lbl)lbl.textContent=(pct>0?'+':'')+pct.toFixed(1)+'%';
  clearTimeout(deckTempoTimers[d]);
  deckTempoTimers[d]=setTimeout(()=>{
    deckPost({cmd:'deckctl',deck:d,act:'tempo',value:Math.pow(2,pct/16*0.5)});
  },160);
}
function deckSync(d){
  // Match the tempo of the deck on the other side of the crossfader.
  const bank=bankValue?['C','D']:['A','B'];
  const other=bank[0]===d?bank[1]:bank[0];
  const mine=window.MIX&&window.MIX[d], theirs=window.MIX&&window.MIX[other];
  if(!theirs||!theirs.src){showErr('反対側のデッキに音源がありません');return;}
  const t=(typeof theirs.tempo==='number'&&theirs.tempo>0)?theirs.tempo:1;
  const pct=Math.max(-16,Math.min(16,Math.log2(t)/0.5*16));
  const el=document.getElementById('d'+d+'tempo');
  if(el)el.value=pct;
  deckTempo(d,pct);
  const btn=document.getElementById(d+'sync');
  if(btn){btn.classList.add('on');setTimeout(()=>btn.classList.remove('on'),600);}
  if(mine&&typeof mine.tempo==='number')showErr('');
}
function deckLoop(d,op){
  deckPost({cmd:'loop',deck:d,op:op});
}
let deckVolTimers={};
function deckVol(d,v){
  clearTimeout(deckVolTimers[d]);
  deckVolTimers[d]=setTimeout(()=>{deckPost({cmd:'deckctl',deck:d,act:'volume',value:parseInt(v)/100});},140);
}
// The two mixer channel faders follow whichever bank is live (A/B or C/D).
function chDeckName(idx){return (bankValue?['C','D']:['A','B'])[idx-1];}
function deckVolByIdx(idx,v){
  deckVol(chDeckName(idx),v);
}
let xfTimer=null;
let xfUserTouched=false;
let xfKeyTouched=false;
function setXfader(v){
  paintXfader();
  // Chrome restores form values on reload and fires `input` for it. Treating
  // that as a gesture would silently drop the mix out of AUTO on every visit,
  // so only a real pointer drag or key press counts as user intent.
  if(!draggingXf&&!xfKeyTouched)return;
  if(!xfUserTouched){xfUserTouched=true;setMixMode(false);}
  clearTimeout(xfTimer);
  xfTimer=setTimeout(()=>{deckPost({cmd:'crossfader',value:parseInt(v)/100});},140);
}
function setMixMode(auto){
  const el=document.getElementById('mixmode');
  if(el){el.textContent=auto?'自動XF':'手動XF';el.className='mixmode '+(auto?'auto':'manual');el.setAttribute('aria-pressed',auto?'true':'false');}
  deckPost({cmd:'mixauto',value:auto});
}
function toggleMixMode(){
  const el=document.getElementById('mixmode');
  const next=!(el&&el.getAttribute('aria-pressed')==='true');
  xfUserTouched=!next;
  setMixMode(next);
}
function renderMix(m){
  const stateEl=document.getElementById('mixstate');
  window.MIX=m||null;
  if(!m){
    stateEl.textContent='ミキサーが起動していません（デッキは操作できません）';
    for(const d of ['A','B','C','D']){
      const card=document.getElementById('d'+d);
      if(!card)continue;
      card.classList.remove('active');
      card.style.opacity='.55';
      for(const el of card.querySelectorAll('button,input'))el.disabled=true;
      const st=document.getElementById('d'+d+'state');
      if(st)st.textContent='停止中';
    }
    return;
  }
  for(const d of ['A','B','C','D']){
    const card=document.getElementById('d'+d);
    if(!card)continue;
    card.style.opacity='';
    for(const el of card.querySelectorAll('button,input'))el.disabled=false;
  }
  const liveDecks=['A','B','C','D'].filter(d=>m[d]&&m[d].playing);
  const mhSub2=document.getElementById('mixHeadSub');
  const syncBtnEl=document.getElementById('syncbtn');
  if(syncBtnEl)syncBtnEl.classList.toggle('attention',!(m.clients>0));
  const nsEl=document.getElementById('notsent');
  if(nsEl){
    const sent=!!(m.clients>0);
    nsEl.hidden=sent;
    if(sent){const w=document.getElementById('notsentwhy');if(w)w.hidden=true;nsEl.setAttribute('aria-expanded','false');}
  }
  const knobRow=document.querySelector('.knobrow');
  if(knobRow)knobRow.classList.toggle('disabled',!(m.clients>0));
  const notSent=!(m.clients>0);
  // The 未配信 badge on the mixer says this; repeating it here made two
  // competing notices for one state.
  // Same word as the 未送信 badge: "playing here" and "sent to Sonos" are two
  // facts, and saying only the first read as "everything is fine".
  stateEl.textContent=liveDecks.length
    ?liveDecks.join('/')+' 再生中'
    :'停止中';
  if(typeof m.bank==='number')bankValue=m.bank;
  try{renderSampler();renderLooper();}catch(e){}
  const bankBtn=document.getElementById('bankbtn');
  // "操作 A/B" read as an action on the decks; the noun first says what it is.
  if(bankBtn)bankBtn.textContent=(bankValue?'C/D':'A/B')+' 操作';
  const flx=document.getElementById('console');
  if(flx){flx.classList.toggle('bank-0',!bankValue);flx.classList.toggle('bank-1',!!bankValue);}
  const ch1=document.getElementById('xfch1'),ch2=document.getElementById('xfch2');
  if(ch1)ch1.textContent=bankValue?'C':'A';
  if(ch2)ch2.textContent=bankValue?'D':'B';
  const chn1=document.getElementById('chn1'),chn2=document.getElementById('chn2');
  if(chn1)chn1.textContent=bankValue?'C':'A';
  if(chn2)chn2.textContent=bankValue?'D':'B';
  for(let i=1;i<=2;i++){
    const dName=chDeckName(i);
    const dk=m[dName]||{};
    const f=document.getElementById('chf'+i);
    if(f&&document.activeElement!==f&&typeof dk.volume==='number')f.value=Math.round(dk.volume*100);
    const mt=document.getElementById('chm'+i);
    if(mt){
      // Use the measured loudness of the deck, not its fader position, so the
      // meter actually moves with the music and goes dark when it is silent.
      const raw=typeof dk.level==='number'?dk.level:0;
      // Show the measured level as-is: a boost made quiet tracks look hot and
      // pinned the meter near the red for loud ones.
      const lvl=dk.playing?Math.round(Math.min(1,raw)*100):0;
      const pk=Math.max(lvl,Math.round((METER_PEAK[dName]||0)*0.86));
      METER_PEAK[dName]=pk;
      mt.style.height=lvl+'%';
      if(mt.parentElement)mt.parentElement.style.setProperty('--peak',pk+'%');
    }
  }
  for(const d of ['A','B','C','D']){
    const deck=m[d]||{};
    const card=document.getElementById('d'+d);
    const btn=document.getElementById('d'+d+'play');
    const stEl=document.getElementById('d'+d+'state');
    const tEl=document.getElementById('d'+d+'title');
    if(!card)continue;
    if(deck.playing){card.classList.add('active');btn.classList.add('on');btn.textContent='⏸';}
    else{card.classList.remove('active');btn.classList.remove('on');btn.textContent='▶';}
    const onLeft=(d==='A'||d==='C');
    const cf=typeof m.crossfader==='number'?m.crossfader:0.5;
    const sideGain=onLeft?Math.cos(cf*Math.PI/2):Math.sin(cf*Math.PI/2);
    // Only the louder side is ON AIR. In the middle both decks are audible,
    // so mark the dominant one and let the meters show the other.
    const dominant=cf<0.5?onLeft:!onLeft;
    const toSonos=(m.clients||0)>0;
    const onAir=!!deck.playing&&toSonos&&dominant&&sideGain>0.2&&(bankValue?['C','D']:['A','B']).includes(d);
    card.classList.toggle('onair',onAir);
    stEl.classList.toggle('onair',onAir);
    const isLive=!(deck.duration>0);
    const posTxt=deck.playing?(isLive?'':' '+fmtCue(deck.pos||0)):'';
    const kindTxt=isLive?'ライブ音源':'';
    stEl.textContent=onAir?('ON AIR'+posTxt):(deck.playing?(kindTxt||('再生中'+posTxt)):(deck.src?'停止中':'空き'));
    const tEl2=document.getElementById('d'+d+'tempo'), tvEl=document.getElementById('d'+d+'tempov');
    if(tEl2&&document.activeElement!==tEl2&&typeof deck.tempo==='number'){
      const pct=Math.max(-16,Math.min(16,Math.log2(deck.tempo)/0.5*16));
      tEl2.value=pct;
      if(tvEl)tvEl.textContent=(pct>0?'+':'')+pct.toFixed(1)+'%';
    }
    const lp=deck.loop||{};
    const liveSrc=!!deck.playing&&!(deck.duration>0);
    const inBtn=document.getElementById('d'+d+'loopin'), outBtn=document.getElementById('d'+d+'loopout');
    if(inBtn)inBtn.classList.toggle('on',!!lp.recording);
    if(outBtn)outBtn.classList.toggle('on',!!lp.recorded&&!lp.recording);
    for(const id of ['d'+d+'loopin','d'+d+'loopout','d'+d+'sync','d'+d+'loopexit','d'+d+'cue']){
      const b2=document.getElementById(id);
      if(!b2)continue;
      b2.classList.toggle('disabled',liveSrc);
      b2.setAttribute('aria-disabled',liveSrc?'true':'false');
      if(liveSrc)b2.title=(id.endsWith('cue')?'ライブ音源ではキュー点を置けません':'ライブ音源では使えません');
      else b2.removeAttribute('title');
    }
    const tempoEl=document.getElementById('d'+d+'tempo');
    if(tempoEl){tempoEl.disabled=liveSrc;tempoEl.setAttribute('aria-disabled',liveSrc?'true':'false');}
    const twEl=tempoEl&&tempoEl.closest('.dcTempoWrap');
    if(twEl)twEl.classList.toggle('disabled',liveSrc);
    // Keep the numeric offset visible while disabled: a lone dash read as an
    // unexplained empty box in review.
    const tvEl2=document.getElementById('d'+d+'tempov');
    if(tvEl2){
      const tv=parseFloat(tempoEl&&tempoEl.value)||0;
      tvEl2.textContent=(tv>0?'+':'')+tv.toFixed(1)+'%';
      tvEl2.title=liveSrc?'ライブ音源ではテンポ変更できません':'テンポ変更量';
    }
    card.classList.toggle('live',liveSrc);
    const padWrap=document.getElementById('pads'+d);
    if(padWrap){
      padWrap.classList.toggle('disabled',liveSrc);
      padWrap.setAttribute('aria-disabled',liveSrc?'true':'false');
      for(const pb of padWrap.children){
        pb.disabled=liveSrc;
        const pt=pb.querySelector('.padt');
        if(pt&&pb.classList.contains('empty'))pt.textContent=liveSrc?'':'＋';
      }
    }
    for(const pm of card.querySelectorAll('.pmode')){
      // title= never shows on a phone, so a tap on a dark mode says why.
      pm.onclick=()=>{if(pm.classList.contains('disabled'))showNote(pm.title||'このモードは使えません');};
      // A real deck keeps the selected pad mode lit even when the pads are
      // dark; only modes that do not exist here are switched off. The pads
      // still go dark on a live source, but the operator can see the mode.
      const implemented=pm.textContent.trim()==='HOT CUE';
      // A lit mode over all-dark pads read as "pressable". On a live source
      // nothing on the pad row works, so the mode goes dark with it.
      const usable=implemented&&!liveSrc;
      pm.classList.toggle('disabled',!usable);
      pm.setAttribute('aria-disabled',!usable?'true':'false');
      pm.title=!implemented
        ?(pm.textContent.trim()==='SAMPLER'?'パッドでは未対応（下のサンプラー欄で操作）':'Sonosに対応機能がありません')
        :(liveSrc?'ライブ音源ではパッドは使えません':'');
      // Selected mode stays lit (dimmed) even when the pads are unusable.
      pm.classList.toggle('on',implemented);
      pm.setAttribute('aria-pressed',implemented?'true':'false');
    }
    const hintEl=document.getElementById('d'+d+'hint');
    // A live stream has no loop points or tempo, so those controls go dark.
    // The explanation lives on the state badge (and each button's title)
    // rather than as a paragraph wedged between the jog and the track name.
    if(hintEl)hintEl.innerHTML='';
    if(stEl){
      stEl.classList.toggle('live',liveSrc);
      stEl.title=liveSrc?'ライブ音源：ループ・テンポ・SYNC・パッドは使えません（🎧CUEは可）':'';
    }
    tEl.textContent=deck.title||(deck.src?'読み込み済み':'曲なし');
    const pflBtn=document.getElementById('d'+d+'pfl');
    if(pflBtn)pflBtn.classList.toggle('on',!!deck.pfl);
    for(const i of [1,2]){
      if(chDeckName(i)!==d)continue;
      const cc=document.getElementById('chcue'+i);
      if(cc)cc.setAttribute('aria-pressed',deck.pfl?'true':'false');
    }
    waveFetch(d,deck.src||'');
    waveDraw(d);
  }
  const xf=document.getElementById('xfader');
  const isAuto=!!m.auto;
  const modeEl=document.getElementById('mixmode');
  if(modeEl){modeEl.textContent=isAuto?'自動XF':'手動XF';modeEl.className='mixmode '+(isAuto?'auto':'manual');modeEl.setAttribute('aria-pressed',isAuto?'true':'false');}
  if(xf&&typeof m.crossfader==='number'){
    const nv=Math.round(m.crossfader*100);
    // In AUTO, always follow the server sweep. In MANUAL, only when not being dragged.
    if(isAuto||(document.activeElement!==xf&&!draggingXf)){
      if(parseFloat(xf.value)!==nv){xf.value=nv;paintXfader();}
    }
  }
  for(const d of ['A','B','C','D'])renderPads(d,m[d]&&m[d].cues,!!(m[d]&&m[d].playing&&!(m[d].duration>0)));
}
const PAD_COUNT=8;
function fmtCue(sec){
  sec=Math.max(0,Math.round(sec));
  const m=Math.floor(sec/60),s=sec%60;
  return m+':'+String(s).padStart(2,'0');
}
function renderPads(d,cues,live){
  const wrap=document.getElementById('pads'+d);
  if(!wrap)return;
  cues=cues||{};
  for(let i=0;i<PAD_COUNT;i++){
    let el=wrap.children[i];
    if(!el){
      el=document.createElement('button');
      el.className='pad';
      el.dataset.deck=d;el.dataset.slot=i;
      el.innerHTML='<span class="padn">'+(i+1)+'</span><span class="padt">＋</span>';
      bindPad(el);
      wrap.appendChild(el);
    }
    const has=Object.prototype.hasOwnProperty.call(cues,String(i));
    el.classList.toggle('set',has);
    el.classList.toggle('empty',!has);
    const t=el.querySelector('.padt');
    if(t)t.textContent=has?fmtCue(cues[String(i)]):(live?'—':'＋');
    el.disabled=!!live;
    el.setAttribute('aria-disabled',live?'true':'false');
  }
}
let padTimers={};
function bindPad(el){
  const d=el.dataset.deck, slot=parseInt(el.dataset.slot);
  let longFired=false, tapCount=0;
  const start=()=>{
    longFired=false;
    padTimers[d+slot]=setTimeout(()=>{
      longFired=true;
      cuePost(d,slot,'set');
      el.classList.add('hit');
      setTimeout(()=>el.classList.remove('hit'),180);
    },500);
  };
  const end=(e)=>{
    clearTimeout(padTimers[d+slot]);
    if(longFired){longFired=false;return;}
    // tap: single=jump/set-if-empty, double=clear
    tapCount++;
    if(tapCount===1){
      setTimeout(()=>{
        if(tapCount===1){
          const has=el.classList.contains('set');
          cuePost(d,slot,has?'goto':'set');
          el.classList.add('hit');setTimeout(()=>el.classList.remove('hit'),160);
        }else if(tapCount>=2){
          cuePost(d,slot,'clear');
        }
        tapCount=0;
      },260);
    }
  };
  el.addEventListener('pointerdown',start);
  el.addEventListener('pointerup',end);
  el.addEventListener('pointercancel',()=>{clearTimeout(padTimers[d+slot]);longFired=false;tapCount=0;});
  el.addEventListener('contextmenu',e=>e.preventDefault());
}
// ---- sampler + looper -----------------------------------------------------
let smpDeck='A';
const SMP_SLOTS=16;
function buildSampler(){
  const sel=document.getElementById('smpDeckSel');
  if(sel&&!sel.children.length){
    for(const d of ['A','B','C','D']){
      const b=document.createElement('button');
      b.textContent=d;b.dataset.deck=d;
      b.setAttribute('aria-pressed',d===smpDeck?'true':'false');
      b.onclick=()=>{smpDeck=d;buildSampler();renderSampler();};
      sel.appendChild(b);
    }
  }
  const grid=document.getElementById('padgrid');
  if(grid&&!grid.children.length){
    for(let i=0;i<SMP_SLOTS;i++){
      const b=document.createElement('button');
      b.className='spad empty';b.dataset.slot=i;
      b.innerHTML='<span class="spn">'+(i+1)+'</span><span class="spt">＋</span>';
      b.onclick=()=>hitPad(i);
      b.oncontextmenu=(e)=>{e.preventDefault();assignPad(i);};
      grid.appendChild(b);
    }
  }
  const lbl=document.getElementById('smpDeck');
  if(lbl)lbl.textContent=smpDeck;
  for(const b of (sel?sel.children:[]))b.setAttribute('aria-pressed',b.dataset.deck===smpDeck?'true':'false');
}
function placeWhy(){
  const b=document.getElementById('notsent'), w=document.getElementById('notsentwhy');
  if(!b||!w||w.hidden)return;
  const r=b.getBoundingClientRect(), ww=w.offsetWidth, wh=w.offsetHeight;
  const vw=document.documentElement.clientWidth, vh=document.documentElement.clientHeight;
  const kn=document.querySelector('.knobrow');
  const kr=kn?kn.getBoundingClientRect():null;
  const hits=(l,t)=>{ if(!kr)return false;
    return l<kr.right+6 && l+ww>kr.left-6 && t<kr.bottom+6 && t+wh>kr.top-6; };
  let left=Math.min(Math.max(8,r.right-ww),vw-ww-8);
  let top=r.bottom+10, above=false;
  if(hits(left,top)){
    const alt=r.top-wh-10;
    if(alt>=8){top=alt;above=true;}
    else if(kr&&kr.right+ww+10<=vw-8){left=kr.right+10;top=Math.min(r.top, vh-wh-8);}
  }
  if(top+wh>vh-8){top=Math.max(8,vh-wh-8);}
  w.classList.toggle('above',above);
  w.style.left=Math.round(left)+'px';
  w.style.top=Math.round(top)+'px';
  const ax=Math.round(Math.min(Math.max(14,r.left+r.width/2-left-5),ww-24));
  w.style.setProperty('--ax',ax+'px');
}
function setWhy(open){
  const b=document.getElementById('notsent'), w=document.getElementById('notsentwhy');
  if(!b||!w)return;
  w.hidden=!open; b.setAttribute('aria-expanded',open?'true':'false');
  if(open)placeWhy();
}
document.addEventListener('click',e=>{
  if(e.target.closest('#notsentx')){setWhy(false);return;}
  const b=e.target.closest('#notsent');
  if(b){setWhy(b.getAttribute('aria-expanded')!=='true');return;}
  // a click anywhere else closes it, so the knobs underneath are never trapped
  if(!e.target.closest('#notsentwhy'))setWhy(false);
});
window.addEventListener('resize',()=>{const w=document.getElementById('notsentwhy');if(w&&!w.hidden)placeWhy();});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){setWhy(false);return;}
  if(e.key!=='Enter'&&e.key!==' ')return;
  const b=e.target.closest('#notsent'); if(!b)return;
  e.preventDefault();
  setWhy(b.getAttribute('aria-expanded')!=='true');
});
function renderSampler(){
  const grid=document.getElementById('padgrid');
  if(!grid)return;
  const m=(st.mix||{})[smpDeck]||{};
  const pads=m.pads||{};
  for(let i=0;i<SMP_SLOTS;i++){
    const el=grid.children[i];if(!el)continue;
    const p=pads[String(i)];
    el.classList.toggle('set',!!p);
    el.classList.toggle('empty',!p);
    const t=el.querySelector('.spt');
    if(t)t.textContent=p?(p.title||('音 '+i)):'＋';
    el.title=p?(p.title+' ('+p.seconds+'s)'):'タップで音源URLを割り当て';
  }
}
async function hitPad(i){
  const el=document.getElementById('padgrid').children[i];
  if(!el||el.classList.contains('empty')){assignPad(i);return;}
  el.classList.add('hit');setTimeout(()=>el.classList.remove('hit'),160);
  await deckPost({cmd:'pad',deck:smpDeck,slot:i,op:'hit'});
}
function assignPad(i){
  const url=prompt('パッド '+(i+1)+'（'+smpDeck+'）に割り当てる音源URL\n空欄で消去','');
  if(url===null)return;
  const src=url.trim();
  deckPost({cmd:'pad',deck:smpDeck,slot:i,op:src?'load':'clear',src:src}).then(()=>setTimeout(refresh,600));
}
async function loopOp(op){
  await deckPost({cmd:'loop',deck:smpDeck,op:op});
  setTimeout(refresh,400);
}
function renderLooper(){
  const m=(st.mix||{})[smpDeck]||{};
  const l=m.loop||{};
  const info=document.getElementById('loopInfo');
  if(info){
    if(l.recording)info.textContent='録音中…';
    else if(l.recorded)info.textContent=l.seconds+'秒'+(l.playing?' 再生中':'');
    else info.textContent='未録音';
    const bar=document.getElementById('loopbar'), thumb=document.getElementById('loopthumb');
    // Loop lengths are powers of two, so the meter is logarithmic: on a linear
    // 0-30s scale the 1s and 2s labels sat 16px apart and overlapped.
    const lpct=sec=>Math.min(100,Math.max(0,Math.log(Math.max(1,sec))/Math.log(30)*100));
    const pct=l.recorded?lpct(l.seconds||0):0;
    if(bar)bar.style.width=pct+'%';
    if(thumb){
      // Keep the knob inside the track at both ends (it used to hang 7px out).
      thumb.style.left='calc('+pct+'% - '+(pct/100).toFixed(4)+' * var(--tw,20px))';
      thumb.classList.toggle('on',!!l.recorded);
    }
    const big=document.getElementById('loopBig'), bigv=document.getElementById('loopBigVal');
    if(bigv){
      bigv.textContent=l.recording?((l.seconds||0).toFixed(1)):(l.recorded?String(l.seconds||0):'1');
      bigv.classList.toggle('off',!l.recording&&!l.recorded);
    }
    if(big)big.classList.toggle('rec',!!l.recording);
    const marks=[1,2,4,8,16,30];
    document.querySelectorAll('.loopTicks span').forEach((el,i)=>{
      el.classList.toggle('on',!!l.recorded&&(l.seconds||0)>=(marks[i]||999));
    });
  }
  const set=(id,on)=>{const e=document.getElementById(id);if(e)e.setAttribute('aria-pressed',on?'true':'false');};
  set('loopRec',!!l.recording);
  set('loopPlay',!!l.playing);
  const stop=document.getElementById('loopStop');
  if(stop)stop.disabled=!l.recording;
  const play=document.getElementById('loopPlay');
  if(play)play.disabled=!l.recorded;
  const pause=document.getElementById('loopPause');
  if(pause)pause.disabled=!l.recorded;
  const clr=document.getElementById('loopClear');
  if(clr)clr.disabled=!l.recorded&&!l.recording;
}
buildSampler();
function cuePost(d,slot,op){
  deckPost({cmd:'cue',deck:d,slot:slot,op:op});
}
async function playYt(){
  const inp=document.getElementById('yturl');
  const url=(inp.value||'').trim();
  const hint=document.getElementById('ythint');
  const btn=document.getElementById('ytbtn');
  if(!url){hint.className='ythint err';hint.textContent='Enter a URL';return;}
  btn.disabled=true;hint.className='ythint';hint.textContent='Loading YouTube… (extracting audio)';
  showErr('');showNote('');
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'youtube',url})});
    const j=await r.json();
    if(j.error){hint.className='ythint err';hint.textContent='Failed: '+j.error;}
    else{hint.className='ythint ok';hint.textContent=j.note||'Loading…';}
  }catch(e){hint.className='ythint err';hint.textContent='Network error: '+e.message;}
  finally{btn.disabled=false;refresh();}
}
function fmtTime(str){
  if(!str)return '0:00';
  const parts=str.split(':').map(Number);
  if(parts.length===3){const[h,m,sec]=parts;return h>0?`${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`:`${m}:${String(sec).padStart(2,'0')}`;}
  if(parts.length===2){const[m,sec]=parts;return `${m}:${String(sec).padStart(2,'0')}`;}
  return str;
}
function toSecs(str){
  if(!str)return 0;
  const p=str.split(':').map(Number);
  if(p.length===3)return p[0]*3600+p[1]*60+p[2];
  if(p.length===2)return p[0]*60+p[1];
  return p[0]||0;
}
function toggleShuffle(){
  const cur=st.play_mode||'NORMAL';
  const isShuf=cur==='SHUFFLE'||cur==='SHUFFLE_NOREPEAT';
  act('playmode',{mode:isShuf?'NORMAL':'SHUFFLE'});
}
function cycleRepeat(){
  const cur=st.play_mode||'NORMAL';
  const isShuf=cur==='SHUFFLE'||cur==='SHUFFLE_NOREPEAT';
  const base=isShuf?'SHUFFLE':'NORMAL';
  const next=cur==='REPEAT_ALL'?'REPEAT_ONE':(cur==='REPEAT_ONE'?base:'REPEAT_ALL');
  act('playmode',{mode:next});
}
let seekDragging=false, seekPreview=null;
function bindSeek(){
  const bar=document.getElementById('seekbar');
  if(!bar)return;
  const pct=e=>{const r=bar.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));};
  const setPreview=p=>{seekPreview=p;
    const f=document.getElementById('seekfill');if(f)f.style.width=(p*100)+'%';
    const k=document.getElementById('seekknob');if(k)k.style.left=(p*100)+'%';};
  bar.addEventListener('pointerdown',e=>{
    if(bar.classList.contains('disabled'))return;
    seekDragging=true;bar.setPointerCapture(e.pointerId);setPreview(pct(e));
  });
  bar.addEventListener('pointermove',e=>{if(seekDragging)setPreview(pct(e));});
  const commit=e=>{
    if(!seekDragging)return;
    seekDragging=false;
    const p=seekPreview!=null?seekPreview:pct(e);
    const dur=toSecs(st.duration);
    if(dur>0){act('seek',{value:Math.round(p*dur)});}
    seekPreview=null;
  };
  bar.addEventListener('pointerup',commit);
  bar.addEventListener('pointercancel',()=>{seekDragging=false;seekPreview=null;});
}
function bindDrag(id){
  const el=document.getElementById(id);if(!el)return;
  const k=id;
  el.addEventListener('pointerdown',()=>{dragging[k]=true;});
  const end=()=>{dragging[k]=false;};
  el.addEventListener('pointerup',end);
  el.addEventListener('pointercancel',end);
  el.addEventListener('change',end);
  el.addEventListener('blur',end);
}
['vol','bass','treble'].forEach(bindDrag);
/* ---- rotary knobs ---- */
const KNOB_SWEEP=270; // degrees of travel
function knobAngle(el){
  const inp=el.querySelector('input[type=range]');
  const min=parseFloat(inp.min),max=parseFloat(inp.max),v=parseFloat(inp.value);
  const t=max>min?(v-min)/(max-min):0;
  return -KNOB_SWEEP/2 + t*KNOB_SWEEP;
}
function paintKnob(el){
  const deg='rotate('+knobAngle(el)+'deg)';
  const dial=el.querySelector('.knob-dial');
  if(dial)dial.style.transform=deg;
  const ptr=el.querySelector('.knob-ptr');
  if(ptr)ptr.style.transform=deg;
  // role="slider" needs its value exposed, otherwise screen readers announce
  // a slider with no position.
  const inp=el.querySelector('input[type=range]');
  if(inp){
    el.setAttribute('aria-valuemin',inp.min);
    el.setAttribute('aria-valuemax',inp.max);
    el.setAttribute('aria-valuenow',inp.value);
  }
}
function paintAllKnobs(){document.querySelectorAll('.rknob').forEach(paintKnob);}
function knobFromY(el,clientY){
  const inp=el.querySelector('input[type=range]');
  const min=parseFloat(inp.min),max=parseFloat(inp.max);
  const r=el.getBoundingClientRect();
  const cy=r.top+r.height/2;
  const t=Math.max(0,Math.min(1,0.5-(clientY-cy)/(r.height*1.6)));
  return Math.round(min+t*(max-min));
}
document.querySelectorAll('.rknob').forEach(el=>{
  const inp=el.querySelector('input[type=range]');
  const kind=el.dataset.kind;
  let active=false,lastY=0;
  const setVal=v=>{
    inp.value=v;
    paintKnob(el);
    setv(kind,v);
  };
  el.addEventListener('pointerdown',e=>{
    active=true;lastY=e.clientY;el.setPointerCapture(e.pointerId);
    dragging[{volume:'vol',bass:'bass',treble:'treble'}[kind]]=true;
    el.classList.add('turning');
    e.preventDefault();
  });
  el.addEventListener('pointermove',e=>{
    if(!active)return;
    const dy=lastY-e.clientY;
    if(Math.abs(dy)>=2){
      const min=parseFloat(inp.min),max=parseFloat(inp.max);
      let nv=parseFloat(inp.value)+Math.round(dy/6);
      nv=Math.max(min,Math.min(max,nv));
      if(nv!==parseFloat(inp.value)){setVal(nv);}
      lastY=e.clientY;
    }
  });
  const end=()=>{
    if(!active)return;active=false;
    dragging[{volume:'vol',bass:'bass',treble:'treble'}[kind]]=false;
    el.classList.remove('turning');
  };
  el.addEventListener('pointerup',end);
  el.addEventListener('pointercancel',end);
  el.addEventListener('wheel',e=>{
    e.preventDefault();
    const min=parseFloat(inp.min),max=parseFloat(inp.max);
    let nv=parseFloat(inp.value)+(e.deltaY<0?1:-1);
    nv=Math.max(min,Math.min(max,nv));
    setVal(nv);
  },{passive:false});
  el.addEventListener('keydown',e=>{
    const min=parseFloat(inp.min),max=parseFloat(inp.max);
    let nv=parseFloat(inp.value);
    if(e.key==='ArrowUp'||e.key==='ArrowRight')nv++;
    else if(e.key==='ArrowDown'||e.key==='ArrowLeft')nv--;
    else return;
    e.preventDefault();
    nv=Math.max(min,Math.min(max,nv));setVal(nv);
  });
  el.addEventListener('dblclick',()=>{
    const min=parseFloat(inp.min),max=parseFloat(inp.max);
    setVal(kind==='volume'?Math.min(30,max):0);
  });
  paintKnob(el);
});
/* ---- crossfader cap ---- */
let draggingXf=false;
function paintXfader(){
  const xf=document.getElementById('xfader');
  const cap=document.getElementById('xfcap');
  if(!xf||!cap)return;
  const track=xf.closest('.xfader-track'), rail=track&&track.querySelector('.xfader-rail');
  const p=Math.max(0,Math.min(100,parseFloat(xf.value)||0));
  if(rail&&track){
    // Measure the rail itself: the track has padding and the rail is inset
    // 8px, so a percentage of the track put the cap off-centre at 50.
    const tr=track.getBoundingClientRect(), rr=rail.getBoundingClientRect();
    // Keep the cap's edges inside the groove: adding half a cap here made it
    // overhang the rail by 17px at both 0 and 100.
    const left=rr.left-tr.left+p/100*Math.max(0,rr.width-cap.offsetWidth);
    cap.style.left=left+'px';
  }else{
    cap.style.left='calc(8px + '+(p/100)+' * (100% - 16px))';
  }
}
{
  const xf=document.getElementById('xfader');
  if(xf){
    xf.addEventListener('input',paintXfader);
    const xfTrack=xf.closest('.xfader-track');
    xf.addEventListener('pointerdown',()=>{draggingXf=true;if(xfTrack)xfTrack.classList.add('active');});
    xf.addEventListener('keydown',()=>{xfKeyTouched=true;});
    xf.addEventListener('blur',()=>{xfKeyTouched=false;});
    const endXf=()=>{draggingXf=false;if(xfTrack)xfTrack.classList.remove('active');};
    xf.addEventListener('pointerup',endXf);
    xf.addEventListener('pointercancel',endXf);
    xf.addEventListener('change',endXf);
    paintXfader();
  }
  // #mixmode is a <button> now: click and Enter/Space come for free from the
  // browser, so only the crossfader's own key tracking is wired here.
}
bindSeek();
let hueTimers={}, hueDragging={bri:false,ct:false};
async function huePost(payload){
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'hue',...payload})});
    const j=await r.json();
    if(j.error)showErr('Light control failed: '+j.error);
    return j;
  }catch(e){showErr('Network error: '+e.message);return {error:e.message};}
  finally{refresh();}
}
function hueToggle(){
  const cur=st.hue||{};
  huePost({on:!cur.on});
}
function hueSet(kind,value){
  const vid=kind==='bri'?'v-huebri':'v-huect';
  const lbl=document.getElementById(vid);
  if(lbl)lbl.textContent=(kind==='ct')?fmtK(value):fmtPct(value);
  const sl=document.getElementById(kind==='bri'?'huebri':'huect');
  if(sl)sl.setAttribute('aria-valuetext',kind==='ct'?fmtK(value):fmtPct(value));
  hueDragging[kind]=true;
  clearTimeout(hueTimers[kind]);
  hueTimers[kind]=setTimeout(()=>{
    hueDragging[kind]=false;
    const p={};p[kind]=parseInt(value);
    huePost(p);
  },140);
}
function huePreset(ct){
  const sl=document.getElementById('huect');
  if(sl)sl.value=ct;
  document.getElementById('v-huect').textContent=fmtK(ct);
  huePost({ct:ct,on:true});
}
function fmtPct(bri){
  const v=Math.max(1,Math.min(254,parseInt(bri)||1));
  return Math.max(1,Math.round(v/254*100))+'%';
}
function fmtK(mired){
  const k=Math.round(1000000/Math.max(1,parseInt(mired)||1)/50)*50;
  return k+'K';
}
function toggleMore(){
  const m=document.getElementById('moremenu'), b=document.getElementById('morebtn');
  if(!m||!b)return;
  const open=m.classList.toggle('open');
  b.setAttribute('aria-expanded',open?'true':'false');
}
document.addEventListener('click',e=>{
  const m=document.getElementById('moremenu'), b=document.getElementById('morebtn');
  if(!m||!m.classList.contains('open'))return;
  if(m.contains(e.target)||b.contains(e.target))return;
  m.classList.remove('open');b.setAttribute('aria-expanded','false');
});
function renderHue(h){
  const panel=document.getElementById('huepanel');
  if(!panel)return;
  if(!h||!h.configured){panel.style.display='none';return;}
  panel.style.display='flex';
  const stEl=document.getElementById('huestate');
  const onBtn=document.getElementById('hueon');
  if(!h.online){
    stEl.textContent='接続中…';
    onBtn.className='huetoggle';onBtn.textContent='切';onBtn.setAttribute('aria-pressed','false');
    return;
  }
  stEl.textContent=(h.reachable?'接続中':'応答なし')+' · '+(h.reachable_count??h.lights.length)+'/'+(h.total??h.lights.length)+'台';
  onBtn.className='huetoggle'+(h.on?' on':'');onBtn.textContent=h.on?'点灯':'消灯';
  onBtn.setAttribute('aria-pressed',h.on?'true':'false');
  if(!hueDragging.bri&&typeof h.bri==='number'){
    document.getElementById('huebri').value=h.bri;
    document.getElementById('huebri').setAttribute('aria-valuetext',fmtPct(h.bri));
    document.getElementById('v-huebri').textContent=fmtPct(h.bri);
  }
  if(!hueDragging.ct&&typeof h.ct==='number'){
    document.getElementById('huect').value=h.ct;
    document.getElementById('huect').setAttribute('aria-valuetext',h.ct);
    document.getElementById('v-huect').textContent=fmtK(h.ct);
  }
}
async function refresh(){
  try{
    const r=await fetch('/api/status');const s=await r.json();st=s;
    pollFail=s.online?0:pollFail+1;
    document.getElementById('offline').className=s.online?'offline':'offline show';
    document.body.classList.toggle('offline-on',!s.online);
    const dotEl=document.getElementById('dot');
    const delivering=!!(s.mix&&s.mix.clients>0);
    dotEl.className='dot '+(!s.online?'bad':(delivering?'ok':'warn'));
    // The dot means "the speaker answers", which is not the same as "audio is
    // being delivered" (that is the 未配信 badge). Say so, so the two can't be
    // read as contradicting each other.
    const dotMsg=!s.online?'Sonosに接続できません'
      :(delivering?'Sonos接続中・配信中':'Sonos接続中・未送信（「同期再生」で配信開始）');
    dotEl.title=dotMsg;dotEl.setAttribute('role','img');dotEl.setAttribute('aria-label',dotMsg);
    // Feature-detect optional integrations so a plain Sonos setup is not shown
    // a console full of dead controls. Runs before the offline early-return.
    const consoleEl=document.getElementById('console');
    if(consoleEl)consoleEl.style.display=s.mix_running?'':'none';
    const worldBtn=document.getElementById('b-world');
    if(worldBtn)worldBtn.style.display=s.world_running?'':'none';
    const ctrls=['vol','bass','treble','loudbtn','mute2','b-world','b-linein','r-groovesalad','r-dronezone','r-secretagent','r-lush','shufbtn','repbtn','xfbtn'];
    if(!s.online){
      // Say *why*: a wrong IP, a missing speaker and a blocked port all look
      // identical otherwise, and the banner is the only place the user looks.
      const om=document.getElementById('offlinemsg');
      const why=(s.error||'').replace(/^HTTPS?ConnectionPool\(host='([^']+)'.*$/,'no answer from $1')
                                 .replace(/^timeout$/,'the speaker timed out');
      if(om)om.textContent="Can't reach the Sonos"+(why?' — '+why:'')+' · retrying…';
      const nameEl=document.getElementById('name');if(nameEl)nameEl.textContent='Sonos';
      const nsEl=document.getElementById('nowsrc');if(nsEl)nsEl.textContent='';
      for(const id of ctrls){const el=document.getElementById(id);if(el)el.disabled=true;}
      renderMix(s.mix);
      renderHue(s.hue);
      return;
    }
    for(const id of ctrls){const el=document.getElementById(id);if(el)el.disabled=false;}
    if(busy)for(const id of busyBtns.concat(['vol','bass','treble'])){const b=document.getElementById(id);if(b)b.disabled=true;}
    const nameEl=document.getElementById('name');
    if(nameEl)nameEl.textContent=(s.name||'Sonos')+(s.model?' · '+s.model:'');
    const srcLabel={world:'WORLD',radio:'Radio',linein:'Line-in (DJ)',youtube:'YouTube',mix:'Mix',other:'—'}[s.source]||'—';
    const radioName=s.source==='radio'&&s.radio_key&&s.radios?s.radios[s.radio_key]:'';
    const switching=s.stale||Date.now()<switchUntil;
    const titleEl=document.getElementById('title');
    if(titleEl)titleEl.textContent=switching?'切り替え中…':(s.title||radioName||srcLabel);
    const metaEl=document.getElementById('meta');
    if(metaEl)metaEl.textContent=switching?'':(s.artist||'');
    const nowsrcEl=document.getElementById('nowsrc');
    const isPlaying=s.state==='PLAYING';
    const verb=isPlaying?'再生中':(s.state==='PAUSED_PLAYBACK'?'一時停止':(s.state==='STOPPED'?'停止中':'—'));
    const nowsrcTxt=switching?'切り替え中…':verb+'：'+srcLabel+(s.source==='radio'&&s.radio_key&&s.radios?'（'+s.radios[s.radio_key]+'）':'');
    if(nowsrcEl&&nowsrcEl.textContent!==nowsrcTxt)nowsrcEl.textContent=nowsrcTxt;
    const dur=toSecs(s.duration), pos=toSecs(s.position);
    const seekable=!!s.seekable && dur>0 && !switching;
    const bar=document.getElementById('seekbar');
    if(bar){
      bar.className='seekbar'+(seekable?'':' disabled');
      bar.setAttribute('aria-valuemax',dur||100);
      bar.setAttribute('aria-valuenow',pos);
      const tpos=document.getElementById('tpos');if(tpos)tpos.textContent=fmtTime(s.position||'0:00');
      const tdur=document.getElementById('tdur');if(tdur)tdur.textContent=s.duration?fmtTime(s.duration):'—';
      if(!seekDragging){
        const pct=dur>0?Math.min(100,(pos/dur)*100):0;
        const fill=document.getElementById('seekfill');if(fill)fill.style.width=pct+'%';
        const knob=document.getElementById('seekknob');if(knob)knob.style.left=pct+'%';
      }
    }
    const pm=s.play_mode||'NORMAL';
    const shuf=pm==='SHUFFLE'||pm==='SHUFFLE_NOREPEAT';
    const repAll=pm==='REPEAT_ALL', repOne=pm==='REPEAT_ONE';
    const sb=document.getElementById('shufbtn');
    sb.className='djbtn'+(shuf?' on':'');sb.setAttribute('aria-pressed',shuf?'true':'false');
    document.getElementById('shuflbl').textContent='シャッフル '+(shuf?'入':'切');
    const rb=document.getElementById('repbtn');
    rb.className='djbtn'+((repAll||repOne)?' on':'');rb.setAttribute('aria-pressed',(repAll||repOne)?'true':'false');
    document.getElementById('replbl').textContent=repOne?'リピート 1曲':(repAll?'リピート 全曲':'リピート 切');
    const xb=document.getElementById('xfbtn');
    xb.className='djbtn'+(s.crossfade?' on':'');xb.setAttribute('aria-pressed',s.crossfade?'true':'false');
    document.getElementById('xflbl').textContent=s.crossfade?'曲間クロスフェード 入':'曲間クロスフェード';
    const stEl=document.getElementById('state');
    if(stEl){
      const stMap={PLAYING:'再生中',PAUSED_PLAYBACK:'一時停止',STOPPED:'停止中',TRANSITIONING:'切替中',BUFFERING:'読込中'};
      stEl.textContent=switching?'確認中':(stMap[s.state]||s.state||'—');
      stEl.className='badge'+(s.state==='PLAYING'&&!switching?' on':'');
    }
    const artEl=document.getElementById('art');
    if(artEl){
      if(!switching&&s.artwork&&/^https?:/.test(s.artwork)){
        const px=artProxy(s.artwork);
        if(artEl.getAttribute('src')!==px){artEl.src=px;}
        artEl.classList.add('show');
        applyArtworkColor(px);
      }else{
        artEl.classList.remove('show');
      }
    }
    const playing=s.state==='PLAYING';
    const playIcon=document.getElementById('playicon');
    if(playIcon)playIcon.innerHTML=playing?'<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>':'<path d="M8 5v14l11-7z"/>';
    const playBtn=document.getElementById('playbtn');
    if(playBtn)playBtn.setAttribute('aria-label',playing?'一時停止':'再生');
    const muteIcon=document.getElementById('muteicon');
    if(muteIcon)muteIcon.innerHTML=s.mute?'<path d="M4 9v6h4l5 5V4L8 9H4z"/><path d="M22 9l-6 6M16 9l6 6" stroke="currentColor" stroke-width="2" fill="none"/>':'<path d="M4 9v6h4l5 5V4L8 9H4z"/><path d="M16 8a5 5 0 010 8M18.5 5.5a9 9 0 010 13" stroke="currentColor" stroke-width="2" fill="none"/>';
    const mutBtn=document.getElementById('mutbtn');
    if(mutBtn)mutBtn.className='tbtn'+(s.mute?' on':'');
    const m2=document.getElementById('mute2');
    if(m2){m2.className=s.mute?'on':'';m2.textContent='ミュート '+(s.mute?'入':'切');m2.setAttribute('aria-pressed',s.mute?'true':'false');}
    if(typeof s.max_vol==='number'){document.getElementById('vol').max=s.max_vol;}
    const notDragging=id=>!dragging[id]&&!pending[id]&&document.activeElement!==document.getElementById(id);
    if(typeof s.volume==='number'&&notDragging('vol')){document.getElementById('vol').value=Math.min(s.volume,s.max_vol||40);document.getElementById('v-vol').innerHTML=s.volume+'<small>0〜'+(s.max_vol||40)+'</small>';}
    if(typeof s.bass==='number'&&notDragging('bass')){document.getElementById('bass').value=s.bass;document.getElementById('v-bass').innerHTML=(s.bass>0?'+':(s.bass<0?'−':''))+Math.abs(s.bass)+'<small>±10</small>';}
    if(typeof s.treble==='number'&&notDragging('treble')){document.getElementById('treble').value=s.treble;document.getElementById('v-treble').innerHTML=(s.treble>0?'+':(s.treble<0?'−':''))+Math.abs(s.treble)+'<small>±10</small>';}
    paintAllKnobs();
    document.getElementById('loudbtn').className=s.loudness?'on':'';
    document.getElementById('loudbtn').textContent='ラウドネス '+(s.loudness?'入':'切');
    const setPressed=(id,on)=>{const b=document.getElementById(id);if(b)b.setAttribute('aria-pressed',on?'true':'false');};
    setPressed('loudbtn',!!s.loudness);
    setPressed('mutbtn',!!s.mute);
    setPressed('b-world',s.source==='world'&&!s.stale);
    setPressed('b-linein',s.source==='linein'&&!s.stale);
    document.getElementById('b-world').className=(s.source==='world'&&!s.stale)?'on':'';
    document.getElementById('b-linein').className=(s.source==='linein'&&!s.stale)?'on':'';
    if(s.radios)for(const k of Object.keys(s.radios)){const b=document.getElementById('r-'+k);if(b){const on=s.radio_key===k&&!s.stale;b.className=on?'on':'';b.setAttribute('aria-pressed',on?'true':'false');}}
    const yt=s.yt||{};
    const yh=document.getElementById('ythint');
    if(yh&&yt.status){
      if(yt.status==='downloading'){yh.className='ythint';yh.textContent='YouTubeを読み込み中…';}
      else if(yt.status==='error'){yh.className='ythint err';yh.textContent='失敗：'+(yt.error||'');}
      else if(s.source==='youtube'&&yt.title){yh.className='ythint ok';yh.textContent='再生中：'+yt.title;}
    }
    document.getElementById('foot').textContent='DJ連携：'+(s.dj_running?'稼働中':'停止')+' ・ 自動復帰：'+(s.world_running?(s.world_hold?'保留':'有効'):'停止');
    renderMix(s.mix);
    renderHue(s.hue);
  }catch(e){
    // A bug in this function used to show up as "can't reach Sonos", which sent
    // us chasing the network. Keep the real reason visible instead.
    console.error('refresh failed:',e);
    pollFail++;
    const om=document.getElementById('offlinemsg');
    if(om)om.textContent='画面の更新に失敗しました — '+(e&&e.message?e.message:e);
    document.getElementById('offline').className='offline show';
    document.getElementById('dot').className='dot bad';
  }
}
/* ---- per-deck real waveform ---- */
const WAVE={A:{el:document.getElementById('waveA'),ctx:null,peaks:null,src:'',live:false,dur:0,raf:0},
            B:{el:document.getElementById('waveB'),ctx:null,peaks:null,src:'',live:false,dur:0,raf:0},
            C:{el:document.getElementById('waveC'),ctx:null,peaks:null,src:'',live:false,dur:0,raf:0},
            D:{el:document.getElementById('waveD'),ctx:null,peaks:null,src:'',live:false,dur:0,raf:0}};
for(const k of ['A','B','C','D']){ if(WAVE[k].el) WAVE[k].ctx=WAVE[k].el.getContext('2d'); }
function waveSize(d){
  const el=d.el; if(!el)return;
  const dpr=Math.min(window.devicePixelRatio||1,2);
  const r=el.getBoundingClientRect();
  el.width=Math.max(1,Math.round(r.width*dpr));
  el.height=Math.max(1,Math.round(r.height*dpr));
  d.ctx.setTransform(dpr,0,0,dpr,0,0);
}
async function waveFetch(deck,src){
  const d=WAVE[deck]; if(!d)return;
  if(!src){ d.peaks=null; d.src=''; waveDraw(deck); return; }
  if(src===d.src) return;
  d.src=src; d.peaks=null; waveDraw(deck);
  try{
    const r=await fetch('/api/wave?deck='+deck+'&n=240');
    const j=await r.json();
    if(d.src!==src) return;
    d.peaks=j.peaks||null; d.live=!!j.live; d.dur=j.duration||0;
    waveDraw(deck);
  }catch(e){}
}
function waveDraw(deck){
  const d=WAVE[deck]; if(!d||!d.ctx)return;
  const el=d.el, ctx=d.ctx;
  const w=el.width/(Math.min(window.devicePixelRatio||1,2));
  const h=el.height/(Math.min(window.devicePixelRatio||1,2));
  if(w<2||h<2)return;
  ctx.clearRect(0,0,w,h);
  const mid=h/2;
  const pk=d.peaks;
  if(!pk||!pk.length){
    // No peaks: either nothing is loaded or it is a live stream. Say which,
    // instead of drawing a flat line that reads as "the track ended".
    const md0=(window.MIX&&window.MIX[deck])||{};
    if(md0.playing&&!(md0.duration>0)){
      ctx.fillStyle='rgba(34,211,238,.14)';
      ctx.fillRect(0,0,w,h);
      ctx.fillStyle='rgba(34,211,238,.95)';
      ctx.font='900 10px -apple-system,sans-serif';
      ctx.textBaseline='middle';
      ctx.fillText('● LIVE',6,mid);
      // Solid baseline first: the old row of evenly spaced 2px bars read as a
      // dotted centre line, not as audio.
      ctx.strokeStyle='rgba(34,211,238,.45)'; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(44,mid+.5); ctx.lineTo(w-4,mid+.5); ctx.stroke();
      // One continuous band, not per-bar rects: 2px bars with 1px gaps still
      // read as a dotted line at this height. Build a filled polygon instead.
      const t=Date.now()/220, x0=44, x1=w-4, step=2;
      const amp=i=>0.22+0.62*Math.abs(Math.sin(t+i*0.55)*Math.sin(t*0.5+i*0.13));
      ctx.beginPath();
      ctx.moveTo(x0,mid);
      for(let x=x0;x<=x1;x+=step){
        const bh=Math.max(2,(h-8)*amp((x-x0)/step));
        ctx.lineTo(x,mid-bh/2);
      }
      for(let x=x1;x>=x0;x-=step){
        const bh=Math.max(2,(h-8)*amp((x-x0)/step));
        ctx.lineTo(x,mid+bh/2);
      }
      ctx.closePath();
      const lg=ctx.createLinearGradient(x0,0,x1,0);
      lg.addColorStop(0,'rgba(34,211,238,.55)');
      lg.addColorStop(1,'rgba(34,211,238,.95)');
      ctx.fillStyle=lg; ctx.fill();
      return;
    }
    ctx.strokeStyle='rgba(120,130,150,.35)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(4,mid); ctx.lineTo(w-4,mid); ctx.stroke();
    return;
  }
  const n=pk.length, gap=1, bw=Math.max(1,(w-gap*(n-1))/n);
  const md=(window.MIX&&window.MIX[deck])||{};
  const pos=md.pos||0;
  const dur=md.duration||d.dur||0;
  const prog=dur>0?Math.min(1,pos/dur):0;
  if(!(dur>0)){
    // Live stream: the newest audio is at the right edge, so there is no
    // meaningful playhead. Tint the right side as "now" instead.
    const g2=ctx.createLinearGradient(0,0,w,0);
    g2.addColorStop(0,'rgba(120,130,160,.30)');
    g2.addColorStop(0.82,'rgba(34,211,238,.45)');
    g2.addColorStop(1,'rgba(34,211,238,.95)');
    // Continuous mirrored band. Per-bar rects (2px on, 1px off) read as a
    // dotted centre line, which is what the reviewer kept seeing on a live
    // source (duration 0 lands in this branch).
    ctx.beginPath(); ctx.moveTo(0,mid);
    for(let i=0;i<n;i++){
      const a=Math.max(0.02,pk[i]);
      ctx.lineTo(i*(bw+gap),mid-Math.max(1.5,a*(h-6))/2);
    }
    for(let i=n-1;i>=0;i--){
      const a=Math.max(0.02,pk[i]);
      ctx.lineTo(i*(bw+gap),mid+Math.max(1.5,a*(h-6))/2);
    }
    ctx.closePath(); ctx.fillStyle=g2; ctx.fill();
    // Solid baseline so the band never disappears into the plate.
    ctx.strokeStyle='rgba(34,211,238,.5)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(0,mid+.5); ctx.lineTo(w,mid+.5); ctx.stroke();
    // No playhead on a live stream: a line at the right edge looked like the
    // end of a track. Label the band LIVE instead.
    // Knock a gap in the bars behind the label: drawn straight over them the
    // text read as struck through by a dotted line.
    ctx.font='700 11px system-ui,sans-serif';
    ctx.textAlign='center'; ctx.textBaseline='middle';
    const lw=ctx.measureText('LIVE').width;
    ctx.fillStyle='rgba(10,12,16,.92)';
    ctx.fillRect(w/2-lw/2-6,mid-9,lw+12,18);
    ctx.fillStyle='rgba(223,229,240,.85)';
    ctx.fillText('LIVE',w/2,mid);
    return;
  }
  for(let i=0;i<n;i++){
    const a=Math.max(0.02,pk[i]);
    const bh=Math.max(1.5,a*(h-6));
    const x=i*(bw+gap), y=mid-bh/2;
    const played=(i/n)<=prog;
    const grad=ctx.createLinearGradient(0,y,0,y+bh);
    if(played){grad.addColorStop(0,'rgba(34,211,238,.95)');grad.addColorStop(1,'rgba(217,70,239,.95)');}
    else{grad.addColorStop(0,'rgba(120,130,160,.42)');grad.addColorStop(1,'rgba(120,130,160,.42)');}
    ctx.fillStyle=grad;
    ctx.fillRect(x,y,bw,bh);
  }
  if(prog>0&&prog<1){
    const px=prog*w;
    ctx.strokeStyle='rgba(255,255,255,.85)'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(px,2); ctx.lineTo(px,h-2); ctx.stroke();
  }
}
function waveResizeAll(){ for(const k of ['A','B','C','D']){ waveSize(WAVE[k]); waveDraw(k);} }
if(WAVE.A.el||WAVE.B.el||WAVE.C.el||WAVE.D.el){
  waveResizeAll();
  window.addEventListener('resize',waveResizeAll);
}
let pollTimer=null, pollFail=0;
function schedulePoll(){
  clearTimeout(pollTimer);
  if(document.hidden)return;
  const delay=Math.min(2000*Math.pow(1.5,pollFail),15000);
  pollTimer=setTimeout(async()=>{await refresh();schedulePoll();},delay);
}
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(pollTimer);}else{pollFail=0;refresh().then(schedulePoll);}});
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.code==='Space'){e.preventDefault();act('toggle');}
});
refresh().then(schedulePoll);
</script>
</body>
</html>
"""


def main():
    global SONOS_IP, PAGE, WEB_PORT
    ap = argparse.ArgumentParser(
        description="Browser dashboard for a Sonos speaker.")
    ap.add_argument("--host", default=_env("SOLUNA_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 for the LAN)")
    ap.add_argument("--port", type=int, default=WEB_PORT,
                    help=f"HTTP port (default {WEB_PORT})")
    ap.add_argument("--sonos", default=SONOS_IP,
                    help="speaker IP; omit to discover over SSDP")
    args = ap.parse_args()
    SONOS_IP = args.sonos
    WEB_PORT = args.port
    PAGE = (PAGE.replace("__BRAND__", html.escape(BRAND))
                 .replace("__MAXVOL__", str(MAX_VOL)))
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot bind {args.host}:{args.port} ({e.strerror or e}); "
                 f"try --port <other>")
    # When bound to every interface, 127.0.0.1 is useless to a phone on the
    # LAN; print the address the other devices actually need.
    if args.host in ("0.0.0.0", "::"):
        shown = LAN_IP
    else:
        shown = args.host
    print(f"dashboard: http://{shown}:{args.port}/  "
          f"(sonos: {SONOS_IP or 'auto-discover'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
