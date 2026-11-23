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
        return _mix_post("/mix/deck", {"deck": deck, "action": "load",
                                       "src": uri, "title": name, "live": True})
    if kind == "file":
        path = data.get("path", "")
        if not path or not os.path.exists(path):
            return {"error": "File not found"}
        return _mix_post("/mix/deck", {"deck": deck, "action": "load",
                                       "src": path, "title": data.get("title") or os.path.basename(path),
                                       "live": False})
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
            "mix_running": daemon_running(MIX_LABEL),
            "mix": _mix_status(),
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
        return {"online": False, "error": str(e), "max_vol": MAX_VOL,
                "source": None, "radio_key": None, "world_hold": os.path.exists(HOLD),
                "dj_running": daemon_running(DJ_LABEL),
                "world_running": daemon_running(WORLD_LABEL),
                "mix_running": daemon_running(MIX_LABEL), "mix": _mix_status(),
                "radios": {k: v[0] for k, v in RADIOS.items()}}


def set_uri(uri, title, cls="object.item.audioItem.audioBroadcast", art=""):
    s = sonos()
    s.avTransport.SetAVTransportURI([("InstanceID", 0), ("CurrentURI", uri),
                                     ("CurrentURIMetaData", didl(uri, title, cls, art))])
    s.play()


def _is_world(s, track):
    return f":{WORLD_PORT}/track/" in (track.get("uri") or "")


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
        if st == "PLAYING":
            ti = s.get_current_track_info()
            if _is_world(s, ti):
                with open(PAUSE, "w") as f:
                    f.write("pause")
            s.pause()
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
    elif cmd == "deck":
        return deck_load(data)
    elif cmd == "deckctl":
        deck = (data.get("deck") or "").upper()
        act = data.get("act")
        if deck not in ("A", "B", "C", "D") or act not in ("play", "pause", "seek", "volume"):
            return {"error": "Invalid deck/act"}
        payload = {"deck": deck, "action": act}
        if act in ("seek", "volume"):
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
                data = r.read(3_000_000)
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
        if self.path.startswith("/api/art"):
            self._serve_art()
            return
        m = re.match(r"^/yt/([A-Za-z0-9_-]+)\.mp3$", self.path.split("?")[0])
        if m:
            self._serve_yt(m.group(1))
            return
        self.send_error(404)

    def do_HEAD(self):
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
        return {"online": False, "error": "timeout", "max_vol": MAX_VOL,
                "source": None, "radio_key": None, "world_hold": os.path.exists(HOLD),
                "dj_running": daemon_running(DJ_LABEL),
                "world_running": daemon_running(WORLD_LABEL),
                "mix_running": daemon_running(MIX_LABEL), "mix": _mix_status(),
                "radios": {k: v[0] for k, v in RADIOS.items()}}


PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark" data-size="md">
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
  --g1:34,211,238;--g2:217,70,239;--glow:.30;--panel:rgba(14,17,25,.66);--inset:rgba(0,0,0,.35);
}
html[data-theme="light"]{
  --bg:#eef1f7;--card:#ffffff;--card2:#f4f6fb;--line:#d6dce8;--fg:#0f1522;--mut:#5b6475;
  --g1:34,211,238;--g2:217,70,239;--glow:.16;--panel:rgba(255,255,255,.82);--inset:rgba(0,0,0,.05);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{height:100%}
body{
  margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;color:var(--fg);
  background:
    radial-gradient(120% 50% at 50% -8%,rgba(var(--g1),var(--glow)),transparent 60%),
    radial-gradient(120% 50% at 50% 108%,rgba(var(--g2),calc(var(--glow)*.9)),transparent 60%),
    var(--bg);
  background-attachment:fixed;overflow-x:hidden;overflow-y:auto;transition:background .6s ease;
}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);
  background-size:44px 44px;mask-image:radial-gradient(circle at 50% 30%,#000,transparent 75%);-webkit-mask-image:radial-gradient(circle at 50% 30%,#000,transparent 75%)}
.app{position:relative;z-index:1;display:flex;flex-direction:column;gap:8px;width:100%;max-width:none;margin:0;
  padding:calc(6px + env(safe-area-inset-top)) env(safe-area-inset-left) calc(10px + env(safe-area-inset-bottom)) env(safe-area-inset-right)}
.offline{position:fixed;left:0;right:0;top:0;background:var(--err);color:#fff;text-align:center;font-size:12px;padding:6px;z-index:40;display:none}
.offline.show{display:block}

/* header */
.top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.brand{display:flex;align-items:center;gap:9px;min-width:0}
.brandtxt{display:flex;flex-direction:column;line-height:1.1;min-width:0}
.brandtxt #name{font-size:14px;font-weight:800;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brandtxt small{font-size:8px;letter-spacing:2.5px;color:var(--mut);font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);flex:none}
.dot.ok{background:var(--ok);box-shadow:0 0 10px var(--ok),0 0 20px var(--ok)}
.dot.bad{background:var(--err);box-shadow:0 0 10px var(--err)}
.topbtns{display:flex;gap:6px;flex:none}
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
.artph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10px;letter-spacing:4px;color:rgba(255,255,255,.16);font-weight:700}
.art{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:none}
.art.show{display:block}
#state{position:absolute;top:6px;right:6px;z-index:3}
.badge{font-size:9px;font-weight:800;padding:3px 8px;border-radius:20px;white-space:nowrap;background:rgba(0,0,0,.62);backdrop-filter:blur(6px);color:#cfd6e6}
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
.times{display:flex;justify-content:space-between;font-size:10px;color:var(--mut);margin-top:4px;font-variant-numeric:tabular-nums}

/* waveform */
.wave{display:block;width:100%;height:64px;border-radius:14px;
  background:linear-gradient(180deg,rgba(var(--g1),.06),rgba(var(--g2),.06));
  border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
@media (max-width:470px){.wave{height:52px}}
.dwave{display:block;width:100%;height:54px;border-radius:12px;margin-top:2px;
  background:linear-gradient(180deg,rgba(var(--g1),.05),rgba(var(--g2),.05));
  border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
@media (max-width:470px){.dwave{height:46px}}
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
.djrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.djbtn{min-height:48px;background:var(--card);border:1px solid var(--line);border-radius:13px;color:var(--fg);font-size:11px;font-weight:700;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;transition:.15s;padding:5px}
.djbtn:active{transform:scale(.96)}
.djbtn:disabled{opacity:.4;cursor:not-allowed}
.djbtn.on{background:rgba(var(--g1),.16);border-color:var(--acc);color:#cfe9ff;box-shadow:0 0 14px rgba(var(--g1),.35)}
.djbtn svg{width:18px;height:18px}
.djbtn small{font-size:9px;font-weight:700;letter-spacing:.4px;color:var(--mut)}
.djbtn.on small{color:#cfe9ff}

/* DJ console */
.console{padding:14px;border-radius:20px;background:var(--panel);border:1px solid var(--line);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);display:flex;flex-direction:column;gap:12px;
  box-shadow:0 14px 40px rgba(0,0,0,.4)}
.consolehead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.cTitle{font-size:11px;font-weight:900;letter-spacing:2px;color:var(--acc);text-shadow:0 0 12px rgba(var(--g1),.6)}
.mixstate{font-size:11px;color:var(--mut);font-weight:600}
.mixmode{font-size:10px;font-weight:900;letter-spacing:1.5px;padding:3px 9px;border-radius:20px;cursor:pointer;user-select:none;transition:.15s;border:1px solid var(--line)}
.mixmode.auto{color:#04121a;background:linear-gradient(160deg,var(--acc),var(--acc2));box-shadow:0 0 14px rgba(var(--g1),.5)}
.mixmode.manual{color:var(--acc);background:var(--inset);border-color:var(--acc)}
.bankbtn{font-size:10px;font-weight:900;letter-spacing:1.5px;padding:3px 10px;border-radius:20px;cursor:pointer;border:1px solid var(--line);background:var(--inset);color:var(--fg);transition:.15s}
.bankbtn:active{transform:scale(.95)}
.dcPfl{min-height:44px;min-width:44px;font-size:10px;font-weight:900;letter-spacing:1px;padding:4px 10px;border-radius:12px;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--mut);transition:.12s}
.dcPfl.on{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff;box-shadow:0 0 12px rgba(var(--g1),.6)}
.decks{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.deckcard{background:var(--inset);border:1px solid var(--line);border-radius:16px;padding:9px;display:flex;flex-direction:column;gap:7px;transition:.2s;min-width:0}
.deckcard.active{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc),0 0 22px rgba(var(--g1),.25)}
.dcHead{display:flex;align-items:center;justify-content:space-between}
.dcName{font-size:11px;font-weight:900;letter-spacing:2px;color:var(--acc)}
.dcState{font-size:10px;color:var(--mut);font-weight:700}
.deckcard.active .dcState{color:var(--ok)}
.platter{position:relative;width:100%;aspect-ratio:1;max-width:120px;margin:0 auto;border-radius:50%;
  background:radial-gradient(circle at 50% 50%,#161d2b 0 16%,#0a0e16 17% 58%,#05070c 59%);
  box-shadow:inset 0 0 0 2px rgba(255,255,255,.06),0 10px 26px rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
.platter::before{content:"";position:absolute;inset:9%;border-radius:50%;
  background:repeating-radial-gradient(circle,#0a0e16 0 2px,#0e131e 2px 5px);opacity:.65}
.deckcard.active .platter::before{animation:spin 3.2s linear infinite}
.plabel{font-size:26px;font-weight:900;color:var(--acc);text-shadow:0 0 14px rgba(var(--g1),.8);z-index:1}
.dcPlay{position:absolute;bottom:4px;right:4px;width:48px;height:48px;border-radius:50%;border:1px solid var(--line);
  background:var(--card);color:var(--fg);font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:2;transition:.15s}
.dcPlay:active{transform:scale(.9)}
.dcPlay.on{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff;box-shadow:0 0 16px rgba(var(--g1),.6)}
.dcTitle{font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg);text-align:center}
.dcVol{display:flex;align-items:center;gap:8px}
.dcVol span{font-size:11px;color:var(--mut);font-weight:800;width:30px}
.dcVol input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:3px;background:var(--line);outline:none}
.dcVol input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 1px 5px rgba(0,0,0,.6)}
.dcSrc{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.dcSrc button{min-height:44px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--fg);font-size:11px;font-weight:700;cursor:pointer;transition:.12s}
.dcSrc button:active{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff}
.dcYt{display:flex;gap:6px}
.dcYt input{flex:1;min-width:0;min-height:44px;background:var(--card2);border:1px solid var(--line);border-radius:10px;color:var(--fg);padding:0 10px;font-size:12px;outline:none}
.dcYt input:focus{border-color:var(--acc)}
.dcYt button{flex:none;min-width:50px;min-height:44px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--fg);font-size:12px;font-weight:700;cursor:pointer}
.dcYt button:active{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff}
.pads{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.pad{position:relative;min-height:44px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--mut);
  font-size:12px;font-weight:800;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;transition:.12s;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}
.pad:active{transform:scale(.94)}
.pad .padn{font-size:13px;font-weight:900}
.pad .padt{font-size:10px;font-weight:700;letter-spacing:.3px;opacity:.85}
.pad.set{background:rgba(34,211,238,.15);border-color:var(--acc);color:#cfe9ff;box-shadow:0 0 10px rgba(var(--g1),.3)}
.pad.set .padt{color:#bde9ff}
.pad.hit{background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;color:#fff;box-shadow:0 0 16px rgba(var(--g2),.6)}
.pad.empty .padn{opacity:.5}

/* crossfader */
.xfwrap{display:flex;flex-direction:column;gap:6px}
.xflabels{display:flex;justify-content:space-between;font-size:9px;font-weight:800;letter-spacing:1.5px;color:var(--mut)}
.xflabels span:nth-child(2){color:var(--acc)}
.xfader{-webkit-appearance:none;appearance:none;width:100%;height:12px;border-radius:6px;outline:none;
  background:linear-gradient(90deg,var(--acc),#2a3550 50%,var(--acc2))}
.xfader::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:30px;height:30px;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 0 0 4px rgba(var(--g1),.25),0 3px 10px rgba(0,0,0,.7)}

/* channel mixer */
.mixer{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;padding:14px 10px 12px;border-radius:18px;background:var(--panel);border:1px solid var(--line);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.knobchan{display:flex;flex-direction:column;align-items:center;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:14px;padding:12px 6px 9px}
.klabel{font-size:9px;letter-spacing:1.5px;color:var(--mut);font-weight:800;order:-1}
.cval{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--fg);line-height:1}
.cval[data-sending]{color:var(--acc)}
/* rotary knob */
.rknob{position:relative;width:78px;height:78px;touch-action:none;cursor:grab;outline:none;border-radius:50%}
.rknob:active{cursor:grabbing}
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
.knob-ptr{position:absolute;left:50%;top:9px;width:3px;height:22px;margin-left:-1.5px;border-radius:2px;
  background:linear-gradient(180deg,var(--acc),#0a0e16);box-shadow:0 0 8px rgba(var(--g1),.9);z-index:2}
.knob-cap{position:absolute;left:50%;top:50%;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;z-index:3;
  background:radial-gradient(circle at 40% 35%,#eef2fa,#9aa6bd 70%,#5b6475);
  box-shadow:inset 0 1px 1px rgba(255,255,255,.7),0 2px 5px rgba(0,0,0,.6);pointer-events:none}
.rknob input[type=range]{position:absolute;inset:0;width:100%;height:100%;opacity:0;pointer-events:none}
/* crossfader */
.xfwrap{display:flex;flex-direction:column;gap:8px;margin-top:2px}
.xflabels{display:flex;justify-content:space-between;font-size:9px;font-weight:800;letter-spacing:1.5px;color:var(--mut)}
.xflabels span:nth-child(2){color:var(--acc)}
.xfader-track{position:relative;height:56px;display:flex;align-items:center;padding:0 4px}
.xfader-rail{position:absolute;left:8px;right:8px;top:50%;height:10px;margin-top:-5px;border-radius:6px;
  background:linear-gradient(180deg,#05070c,#1a2130);box-shadow:inset 0 2px 4px rgba(0,0,0,.8),0 1px 0 rgba(255,255,255,.05)}
.xfader-rail::before{content:"";position:absolute;left:0;top:0;bottom:0;width:50%;border-radius:6px 0 0 6px;
  background:linear-gradient(90deg,rgba(var(--g1),.4),transparent)}
.xfader-cap{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:52px;height:34px;border-radius:8px;z-index:3;
  background:linear-gradient(180deg,#f4f7ff,#c3cbe0 55%,#8b96ad);
  box-shadow:0 4px 10px rgba(0,0,0,.6),inset 0 1px 1px rgba(255,255,255,.9),inset 0 -2px 3px rgba(0,0,0,.3);
  display:flex;flex-direction:column;justify-content:center;gap:3px;padding:0 7px;pointer-events:none;transition:left .08s linear}
.xfader-cap span{display:block;height:2px;border-radius:2px;background:rgba(20,30,45,.5)}
.xfader{position:absolute;inset:0;width:100%;height:100%;-webkit-appearance:none;appearance:none;background:transparent;opacity:0;cursor:grab;margin:0}
.xfader:active{cursor:grabbing}

/* tabs */
.tabs{display:flex;flex-direction:column;gap:10px}
.tabbar{display:flex;gap:6px;background:var(--inset);border:1px solid var(--line);border-radius:13px;padding:4px}
.tab{flex:1;min-height:44px;border:none;background:transparent;color:var(--mut);font-size:11px;font-weight:800;letter-spacing:1px;border-radius:10px;cursor:pointer;transition:.15s}
.tab.on{background:var(--card);color:var(--fg);box-shadow:0 2px 10px rgba(0,0,0,.3)}
.tabpanel{display:none;flex-direction:column;gap:9px}
.tabpanel.on{display:flex}
.src{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.src button{min-height:48px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:13px;padding:9px;font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:.15s}
.src button:active{transform:scale(.97)}
.src button:disabled{opacity:.4;cursor:not-allowed}
.src button.on{background:rgba(var(--g1),.16);border-color:var(--acc);color:#cfe9ff;box-shadow:0 0 14px rgba(var(--g1),.3)}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row button{flex:1;min-width:64px;min-height:46px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:13px;padding:10px;font-size:12px;font-weight:700;cursor:pointer;transition:.15s}
.row button:active{transform:scale(.97)}
.row button:disabled{opacity:.4;cursor:not-allowed}
.row button.on{background:rgba(52,211,153,.16);border-color:var(--ok);color:var(--ok)}
.ytrow{display:flex;gap:8px}
.ytrow input{flex:1;min-width:0;min-height:48px;background:var(--card2);border:1px solid var(--line);border-radius:13px;color:var(--fg);padding:0 12px;font-size:13px;outline:none}
.ytrow input:focus{border-color:var(--acc)}
.ytrow input::placeholder{color:var(--mut)}
.ytrow button{flex:none;min-width:70px;min-height:48px;background:linear-gradient(160deg,var(--acc),var(--acc2));border:none;border-radius:13px;color:#fff;font-size:13px;font-weight:800;cursor:pointer;transition:.15s}
.ytrow button:active{transform:scale(.96)}
.ytrow button:disabled{opacity:.5;cursor:not-allowed}
.ythint{color:var(--mut);font-size:11px;line-height:1.5;min-height:15px}
.ythint.err{color:var(--err)}
.ythint.ok{color:var(--ok)}
.hint{color:var(--mut);font-size:10px;line-height:1.6;padding-top:4px}

/* lights / hue */
.hue{padding:12px 14px;border-radius:18px;background:var(--panel);border:1px solid var(--line);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);display:flex;flex-direction:column;gap:10px}
.huehead{display:flex;align-items:center;gap:8px}
.hueTitle{font-size:11px;font-weight:900;letter-spacing:2px;color:var(--warn);text-shadow:0 0 12px rgba(251,191,36,.5)}
.huestate{font-size:10px;color:var(--mut);font-weight:700;margin-left:auto;text-align:right}
.huetoggle{min-width:56px;min-height:44px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--mut);font-size:11px;font-weight:900;letter-spacing:1px;cursor:pointer;transition:.15s}
.huetoggle:active{transform:scale(.95)}
.huetoggle.on{background:linear-gradient(160deg,#fbbf24,#f59e0b);border:none;color:#241a00;box-shadow:0 0 14px rgba(251,191,36,.6)}
.huerow{display:flex;align-items:center;gap:10px}
.huelbl{font-size:11px;color:var(--mut);font-weight:800;width:44px;flex:none}
.huerow input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:44px;border-radius:4px;outline:none;background:transparent;margin:0}
.huerow input[type=range]::-webkit-slider-runnable-track{height:8px;border-radius:4px;
  background:linear-gradient(90deg,#9ecbff,#fff2d6,#ffb86b)}
.huerow input[type=range]::-moz-range-track{height:8px;border-radius:4px;
  background:linear-gradient(90deg,#9ecbff,#fff2d6,#ffb86b)}
.huerow input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:28px;height:28px;margin-top:-10px;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 1px 6px rgba(0,0,0,.6)}
.huerow input[type=range]::-moz-range-thumb{width:28px;height:28px;border:none;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 1px 6px rgba(0,0,0,.6)}
.huerow input[type=range]:focus-visible{box-shadow:0 0 0 3px rgba(251,191,36,.5);border-radius:8px}
.hueval{font-size:12px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--fg);width:34px;text-align:right;flex:none}
.huepresets{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.huepresets button{min-height:44px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:11px;font-weight:700;cursor:pointer;transition:.12s}
.huepresets button:active{background:linear-gradient(160deg,#fbbf24,#f59e0b);border:none;color:#241a00}

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
  .decks{grid-template-columns:1fr 1fr;gap:12px}
  .deckcard{padding:7px;gap:5px;border-radius:12px}
  .platter{max-width:66px}
  .dcTitle{font-size:11px}
  .dwave{height:24px}
  .pads{gap:4px}
  .pad{min-height:44px;font-size:11px;border-radius:9px}
  .dcSrc{gap:5px}
  .dcSrc button{min-height:44px;font-size:11px}
  .dcYt input,.dcYt button{min-height:44px;font-size:12px}
  .dcPlay{width:44px;height:44px;font-size:14px;bottom:2px;right:2px}
  .dcPfl{min-height:44px;min-width:44px;font-size:11px;padding:4px 12px}
  .dcName{font-size:12px;letter-spacing:1px}
  .dcState{font-size:11px}
  .dcVol input[type=range]{height:8px}
  .dcVol input[type=range]::-webkit-slider-thumb{width:24px;height:24px}
  .hero{padding:8px 10px;gap:10px;border-radius:14px}
  .artwrap{width:52px;height:52px}
  .title{font-size:14px}
  .meta{display:none}
  .nowsrc{display:none}
  .seekwrap{margin-top:4px}
  .times{font-size:9px}
  .transport{gap:12px;padding:2px 0}
  .transport .tbtn{width:44px;height:44px}
  #playbtn{width:56px;height:56px}
  .consolehead{gap:6px}
  .consolehead .mixstate{display:none}
}
@media (max-width:380px){
  .mixer{gap:5px;padding:10px 5px 8px}
  .knobchan{padding:9px 3px 7px;gap:5px}
  .rknob{width:62px;height:62px}
  .knob-cap{width:22px;height:22px;margin:-11px 0 0 -11px}
  .knob-ptr{height:17px;top:7px}
  .steps{gap:4px;width:100%}
  .steps .step{flex:1;width:auto;min-width:0;height:44px}
}

/* phone landscape: DJ posture — 4 decks in a row, compact top */

/* iPad / wide: two-column console, full-bleed */
@media (min-width:760px){
  .app{display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:min-content;gap:14px;
    align-items:start;padding:12px max(12px,env(safe-area-inset-left)) 16px max(12px,env(safe-area-inset-right))}
  .top{grid-column:1 / -1}
  .hero{grid-column:1 / -1}
  .wave{grid-column:1 / -1;height:80px}
  .dwave{height:64px}
  .transport{grid-column:1 / -1}
  .console{grid-column:1 / -1}
  .decks{grid-template-columns:repeat(4,1fr)}
  .platter{max-width:120px}
  .mixer{grid-column:1}
  .tabs{grid-column:2}
  .djrow{grid-column:1 / -1}
  .hue{grid-column:1 / -1}
  .err{grid-column:1 / -1}
}
@media (min-width:1100px){
  .app{max-width:1180px;margin:0 auto}
  .hero{padding:12px 16px;gap:14px}
  .artwrap{width:96px;height:96px}
  .title{font-size:19px}
  .transport{gap:14px}
  .decks{grid-template-columns:1fr 1fr 1fr 1fr}
  .platter{max-width:120px}
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
  .console{order:3;padding:8px;border-radius:14px;gap:8px}
  .mixer{order:4}
  .tabs{order:5}
  .djrow{order:6}
  .err{order:7}
  .consolehead{flex-wrap:nowrap}
  .consolehead .mixstate{display:none}
  .decks{grid-template-columns:repeat(4,1fr);gap:6px}
  .deckcard{padding:5px;gap:4px;border-radius:10px}
  .platter{max-width:52px}
  .dcTitle{font-size:10px}
  .dwave{height:20px}
  .pads{grid-template-columns:repeat(4,1fr);gap:2px}
  .pad{min-height:44px;font-size:10px;border-radius:7px}
  .dcSrc{gap:3px}
  .dcSrc button{min-height:44px;font-size:10px}
  .dcYt input,.dcYt button{min-height:44px;font-size:10px}
  .dcPlay{width:44px;height:44px;font-size:13px;bottom:2px;right:2px}
  .dcPfl{min-height:44px;min-width:44px;font-size:10px;padding:2px 8px}
  .dcName{font-size:11px;letter-spacing:1px}
  .dcState{font-size:10px}
  .xfwrap{margin-top:4px}
}
</style>
</head>
<body>
<div class="offline" id="offline">Can't reach the Sonos — retrying…</div>
<div class="app">
  <div class="top">
    <div class="brand"><span class="dot" id="dot"></span><span class="brandtxt"><span id="name">Sonos</span><small>__BRAND__ DJ</small></span></div>
    <div class="topbtns">
      <button class="icobtn" id="themebtn" onclick="toggleTheme()" aria-label="Toggle theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 000 18z" fill="currentColor" stroke="none"/></svg></button>
      <button class="icobtn" id="sizebtn" onclick="toggleSize()" aria-label="Text size" style="font-weight:800;font-size:15px">A</button>
    </div>
  </div>

  <div class="hero">
    <div class="artwrap" id="artwrap" onclick="act('toggle')">
      <div class="vinyl"></div>
      <div class="artph">__BRAND__</div>
      <img class="art" id="art" alt="Album art" referrerpolicy="no-referrer" onerror="this.classList.remove('show')">
      <span class="badge" id="state">—</span>
    </div>
    <div class="deckinfo">
      <div class="title" id="title">—</div>
      <div class="meta" id="meta"></div>
      <div class="nowsrc" id="nowsrc" aria-live="polite"></div>
      <div class="seekwrap">
        <div class="seekbar disabled" id="seekbar" role="slider" aria-label="Playback position" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" tabindex="0">
          <div class="fill" id="seekfill"></div>
          <div class="knob" id="seekknob"></div>
        </div>
        <div class="times"><span id="tpos">0:00</span><span id="tdur">—</span></div>
      </div>
    </div>
  </div>

  <div class="transport">
    <button class="tbtn" onclick="act('previous')" id="prevbtn" aria-label="Previous"><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg></button>
    <button id="playbtn" onclick="act('toggle')" aria-label="Play / pause"><svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor" id="playicon"><path d="M8 5v14l11-7z"/></svg></button>
    <button class="tbtn" onclick="act('next')" id="nextbtn" aria-label="Next"><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg></button>
    <button class="tbtn" onclick="act('mute')" id="mutbtn" aria-label="Mute" aria-pressed="false"><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" id="muteicon"><path d="M4 9v6h4l5 5V4L8 9H4z"/><path d="M16 8a5 5 0 010 8M18.5 5.5a9 9 0 010 13" stroke="currentColor" stroke-width="2" fill="none"/></svg></button>
  </div>

  <div class="console" id="console">
    <div class="consolehead">
      <span class="cTitle">4-DECK MIXER</span>
      <button class="bankbtn" id="bankbtn" onclick="toggleBank()" title="Choose which decks the controller channels drive">BANK A/B</button>
      <span class="mixmode" id="mixmode" role="button" tabindex="0" title="Tap to toggle AUTO / MANUAL">AUTO</span>
      <span class="mixstate" id="mixstate">Load a source into DECK A/B/C/D</span>
    </div>
    <div class="decks" id="decks"></div>
    <div class="xfwrap">
      <div class="xflabels"><span id="xfch1">A</span><span>CROSSFADER</span><span id="xfch2">B</span></div>
      <div class="xfader-track">
        <div class="xfader-rail"></div>
        <div class="xfader-cap" id="xfcap"><span></span><span></span></div>
        <input type="range" min="0" max="100" value="50" id="xfader" class="xfader" oninput="setXfader(this.value)" aria-label="Crossfader">
      </div>
    </div>
  </div>

  <div class="mixer">
    <div class="knobchan">
      <div class="rknob" data-kind="volume" data-min="0" data-max="__MAXVOL__" role="slider" aria-label="Volume" tabindex="0">
        <div class="knob-dial"><span class="knob-ptr"></span></div>
        <div class="knob-cap"></div>
        <input type="range" min="0" max="__MAXVOL__" step="1" id="vol" oninput="setv('volume',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">VOL</div>
      <div class="cval" id="v-vol">—</div>
      <div class="steps"><button class="step" onclick="stepv('volume',-1)" aria-label="Volume down">−</button><button class="step" onclick="stepv('volume',1)" aria-label="Volume up">＋</button></div>
    </div>
    <div class="knobchan">
      <div class="rknob" data-kind="bass" data-min="-10" data-max="10" role="slider" aria-label="Bass" tabindex="0">
        <div class="knob-dial"><span class="knob-ptr"></span></div>
        <div class="knob-cap"></div>
        <input type="range" min="-10" max="10" step="1" id="bass" oninput="setv('bass',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">BASS</div>
      <div class="cval" id="v-bass">—</div>
      <div class="steps"><button class="step" onclick="stepv('bass',-1)" aria-label="Bass down">−</button><button class="step" onclick="stepv('bass',1)" aria-label="Bass up">＋</button></div>
    </div>
    <div class="knobchan">
      <div class="rknob" data-kind="treble" data-min="-10" data-max="10" role="slider" aria-label="Treble" tabindex="0">
        <div class="knob-dial"><span class="knob-ptr"></span></div>
        <div class="knob-cap"></div>
        <input type="range" min="-10" max="10" step="1" id="treble" oninput="setv('treble',this.value)" aria-hidden="true" tabindex="-1">
      </div>
      <div class="klabel">TREBLE</div>
      <div class="cval" id="v-treble">—</div>
      <div class="steps"><button class="step" onclick="stepv('treble',-1)" aria-label="Treble down">−</button><button class="step" onclick="stepv('treble',1)" aria-label="Treble up">＋</button></div>
    </div>
  </div>

  <div class="tabs">
    <div class="tabbar">
      <button class="tab on" data-tab="source" onclick="showTab('source')">SOURCE</button>
      <button class="tab" data-tab="radio" onclick="showTab('radio')">RADIO</button>
      <button class="tab" data-tab="youtube" onclick="showTab('youtube')">YOUTUBE</button>
    </div>
    <div class="tabpanel on" id="tab-source">
      <div class="src">
        <button onclick="act('world')" id="b-world" aria-pressed="false"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/></svg> WORLD</button>
        <button onclick="act('linein')" id="b-linein" aria-pressed="false"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 11h12v2H3zM15 8h2v8h-2zM18 10h3v4h-3z"/></svg> Line-in</button>
      </div>
      <div class="row">
        <button onclick="act('loudness')" id="loudbtn" aria-pressed="false">Loudness OFF</button>
        <button onclick="act('mute')" id="mute2" aria-pressed="false">Mute</button>
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
        <input type="url" id="yturl" inputmode="url" autocomplete="off" placeholder="Paste a YouTube URL" aria-label="YouTube URL">
        <button onclick="playYt()" id="ytbtn">Play</button>
      </div>
      <div class="ythint" id="ythint">Paste a URL and the audio is extracted and played on the Sonos.</div>
    </div>
  </div>

  <div class="djrow">
    <button class="djbtn" onclick="toggleShuffle()" id="shufbtn" aria-label="Shuffle" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>
      <small id="shuflbl">SHUFFLE</small>
    </button>
    <button class="djbtn" onclick="cycleRepeat()" id="repbtn" aria-label="Repeat" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 2l4 4-4 4M3 11V9a4 4 0 014-4h14M7 22l-4-4 4-4M21 13v2a4 4 0 01-4 4H3"/></svg>
      <small id="replbl">REPEAT</small>
    </button>
    <button class="djbtn" onclick="act('crossfade')" id="xfbtn" aria-label="Crossfade" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8h6l6 8h6M3 16h6l6-8h6"/></svg>
      <small id="xflbl">XFADE</small>
    </button>
  </div>

  <div class="hue" id="huepanel">
    <div class="huehead">
      <span class="hueTitle">LIGHTS · Hue</span>
      <span class="huestate" id="huestate">—</span>
      <button class="huetoggle" id="hueon" onclick="hueToggle()" aria-pressed="false">ON</button>
    </div>
    <div class="huerow">
      <span class="huelbl">Bright</span>
      <input type="range" min="1" max="254" step="1" value="140" id="huebri" oninput="hueSet('bri',this.value)" aria-label="Brightness" aria-valuetext="140">
      <span class="hueval" id="v-huebri">—</span>
    </div>
    <div class="huerow">
      <span class="huelbl">Warmth</span>
      <input type="range" min="153" max="500" step="1" value="500" id="huect" oninput="hueSet('ct',this.value)" aria-label="Color temperature (warmer to the right)" aria-valuetext="500">
      <span class="hueval" id="v-huect">—</span>
    </div>
    <div class="huepresets">
      <button onclick="huePreset(500)">Candle</button>
      <button onclick="huePreset(400)">Warm</button>
      <button onclick="huePreset(300)">Daylight</button>
      <button onclick="huePreset(200)">Cool</button>
    </div>
  </div>

  <div class="err" id="err" role="alert" aria-live="assertive"></div>
</div>
<div class="note" id="note" role="status" aria-live="polite"></div>
<script>
let st={online:false}, busy=false, timers={}, noteTimer=null, switchUntil=0;
const dragging={vol:false,bass:false,treble:false};
const pending={vol:false,bass:false,treble:false};
const busyBtns=['playbtn','prevbtn','nextbtn','mutbtn','mute2','loudbtn','b-world','b-linein','r-groovesalad','r-dronezone','r-secretagent','r-lush','shufbtn','repbtn','xfbtn','ytbtn'];
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
function toggleTheme(){
  const h=document.documentElement;
  const next=h.getAttribute('data-theme')==='light'?'dark':'light';
  h.setAttribute('data-theme',next);
  try{localStorage.setItem('soluna-theme',next)}catch(e){}
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
  if(srcCmds.includes(cmd)){document.getElementById('nowsrc').textContent='Switching…';switchUntil=Date.now()+3000;}
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
function setv(kind,value){
  const vid={volume:'v-vol',bass:'v-bass',treble:'v-treble'}[kind]||('v-'+kind);
  const lbl=document.getElementById(vid);
  if(lbl)lbl.textContent=value;
  const sid={volume:'vol',bass:'bass',treble:'treble'}[kind];
  if(sid){const sl=document.getElementById(sid);if(sl&&parseFloat(sl.value)!==parseFloat(value))sl.value=value;const kn=document.querySelector('.rknob[data-kind="'+kind+'"]');if(kn)paintKnob(kn);}
  const pk0={volume:'vol',bass:'bass',treble:'treble'}[kind];
  if(pk0)pending[pk0]=true;
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
  if(!url){showErr('Enter a YouTube URL');return;}
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
let bankValue=0;
function toggleBank(){
  bankValue=bankValue?0:1;
  deckPost({cmd:'bank',value:bankValue});
}
function buildDecks(){
  const wrap=document.getElementById('decks');
  if(!wrap)return;
  const DECKS=['A','B','C','D'];
  const radios=[['groovesalad','Groove'],['dronezone','Drone'],['secretagent','Agent'],['lush','Lush']];
  for(const d of DECKS){
    const card=document.createElement('div');
    card.className='deckcard';card.id='d'+d;
    let srcBtns=radios.map(([k,l])=>`<button onclick="deckRadio('${d}','${k}')">${l}</button>`).join('');
    card.innerHTML=
      `<div class="dcHead"><span class="dcName">DECK ${d}</span>`+
        `<span class="dcState" id="d${d}state">Stopped</span>`+
        `<button class="dcPfl" id="d${d}pfl" onclick="deckPfl('${d}')" title="PFL (headphone cue)">PFL</button></div>`+
      `<div class="platter"><span class="plabel">${d}</span>`+
        `<button class="dcPlay" onclick="deckPlay('${d}')" id="d${d}play" aria-label="DECK ${d} play / pause">▶</button></div>`+
      `<div class="dcTitle" id="d${d}title">No track</div>`+
      `<canvas class="dwave" id="wave${d}" aria-label="DECK ${d} waveform"></canvas>`+
      `<div class="dcVol"><span>VOL</span><input type="range" min="0" max="100" value="100" id="d${d}vol" oninput="deckVol('${d}',this.value)" aria-label="DECK ${d} volume"></div>`+
      `<div class="pads" id="pads${d}" aria-label="DECK ${d} hot cues"></div>`+
      `<div class="dcSrc">${srcBtns}</div>`+
      `<div class="dcYt"><input type="url" id="d${d}yt" placeholder="YouTube → ${d}" aria-label="DECK ${d} YouTube"><button onclick="deckYt('${d}')">Load</button></div>`;
    wrap.appendChild(card);
  }
}
buildDecks();
let deckVolTimers={};
function deckVol(d,v){
  clearTimeout(deckVolTimers[d]);
  deckVolTimers[d]=setTimeout(()=>{deckPost({cmd:'deckctl',deck:d,act:'volume',value:parseInt(v)/100});},140);
}
let xfTimer=null;
let xfUserTouched=false;
function setXfader(v){
  paintXfader();
  if(!xfUserTouched){xfUserTouched=true;setMixMode(false);}
  clearTimeout(xfTimer);
  xfTimer=setTimeout(()=>{deckPost({cmd:'crossfader',value:parseInt(v)/100});},140);
}
function setMixMode(auto){
  const el=document.getElementById('mixmode');
  if(el){el.textContent=auto?'AUTO':'MANUAL';el.className='mixmode '+(auto?'auto':'manual');}
  deckPost({cmd:'mixauto',value:auto});
}
function renderMix(m){
  const stateEl=document.getElementById('mixstate');
  window.MIX=m||null;
  if(!m){
    stateEl.textContent='Mixer not running (decks unavailable)';
    return;
  }
  const anyPlaying=(m.A&&m.A.playing)||(m.B&&m.B.playing)||(m.C&&m.C.playing)||(m.D&&m.D.playing);
  stateEl.textContent=(m.clients>0?'Streaming to Sonos':'Idle')+' · '+(anyPlaying?'Playing':'Stopped');
  if(typeof m.bank==='number')bankValue=m.bank;
  const bankBtn=document.getElementById('bankbtn');
  if(bankBtn)bankBtn.textContent='BANK '+(bankValue?'C/D':'A/B');
  const ch1=document.getElementById('xfch1'),ch2=document.getElementById('xfch2');
  if(ch1)ch1.textContent=bankValue?'C':'A';
  if(ch2)ch2.textContent=bankValue?'D':'B';
  for(const d of ['A','B','C','D']){
    const deck=m[d]||{};
    const card=document.getElementById('d'+d);
    const btn=document.getElementById('d'+d+'play');
    const stEl=document.getElementById('d'+d+'state');
    const tEl=document.getElementById('d'+d+'title');
    if(!card)continue;
    if(deck.playing){card.classList.add('active');btn.classList.add('on');btn.textContent='⏸';}
    else{card.classList.remove('active');btn.classList.remove('on');btn.textContent='▶';}
    stEl.textContent=deck.playing?'Playing':(deck.src?'Stopped':'Empty');
    tEl.textContent=deck.title||(deck.src?'Loaded':'No track');
    const volEl=document.getElementById('d'+d+'vol');
    if(volEl&&document.activeElement!==volEl&&typeof deck.volume==='number')volEl.value=Math.round(deck.volume*100);
    const pflBtn=document.getElementById('d'+d+'pfl');
    if(pflBtn)pflBtn.classList.toggle('on',!!deck.pfl);
    waveFetch(d,deck.src||'');
    waveDraw(d);
  }
  const xf=document.getElementById('xfader');
  const isAuto=!!m.auto;
  const modeEl=document.getElementById('mixmode');
  if(modeEl){modeEl.textContent=isAuto?'AUTO':'MANUAL';modeEl.className='mixmode '+(isAuto?'auto':'manual');}
  if(xf&&typeof m.crossfader==='number'){
    const nv=Math.round(m.crossfader*100);
    // In AUTO, always follow the server sweep. In MANUAL, only when not being dragged.
    if(isAuto||(document.activeElement!==xf&&!draggingXf)){
      if(parseFloat(xf.value)!==nv){xf.value=nv;paintXfader();}
    }
  }
  for(const d of ['A','B','C','D'])renderPads(d,m[d]&&m[d].cues);
}
const PAD_COUNT=8;
function fmtCue(sec){
  sec=Math.max(0,Math.round(sec));
  const m=Math.floor(sec/60),s=sec%60;
  return m+':'+String(s).padStart(2,'0');
}
function renderPads(d,cues){
  const wrap=document.getElementById('pads'+d);
  if(!wrap)return;
  cues=cues||{};
  for(let i=0;i<PAD_COUNT;i++){
    let el=wrap.children[i];
    if(!el){
      el=document.createElement('button');
      el.className='pad';
      el.dataset.deck=d;el.dataset.slot=i;
      el.innerHTML='<span class="padn">'+(i+1)+'</span><span class="padt">—</span>';
      bindPad(el);
      wrap.appendChild(el);
    }
    const has=Object.prototype.hasOwnProperty.call(cues,String(i));
    el.classList.toggle('set',has);
    el.classList.toggle('empty',!has);
    const t=el.querySelector('.padt');
    if(t)t.textContent=has?fmtCue(cues[String(i)]):'—';
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
  const setPreview=p=>{seekPreview=p;document.getElementById('seekfill').style.width=(p*100)+'%';document.getElementById('seekknob').style.left=(p*100)+'%';};
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
  const dial=el.querySelector('.knob-dial');
  if(dial)dial.style.transform='rotate('+knobAngle(el)+'deg)';
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
  if(xf&&cap){const p=parseFloat(xf.value);cap.style.left='calc(8px + '+(p/100)+' * (100% - 16px))';}
}
{
  const xf=document.getElementById('xfader');
  if(xf){
    xf.addEventListener('input',paintXfader);
    xf.addEventListener('pointerdown',()=>{draggingXf=true;});
    const endXf=()=>{draggingXf=false;};
    xf.addEventListener('pointerup',endXf);
    xf.addEventListener('pointercancel',endXf);
    xf.addEventListener('change',endXf);
    paintXfader();
  }
  const modeEl=document.getElementById('mixmode');
  if(modeEl){
    const toggle=()=>{const cur=modeEl.textContent==='AUTO';const next=!cur;xfUserTouched=!next;setMixMode(next);};
    modeEl.addEventListener('click',toggle);
    modeEl.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});
  }
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
  if(lbl)lbl.textContent=value;
  const sl=document.getElementById(kind==='bri'?'huebri':'huect');
  if(sl)sl.setAttribute('aria-valuetext',value);
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
  document.getElementById('v-huect').textContent=ct;
  huePost({ct:ct,on:true});
}
function renderHue(h){
  const panel=document.getElementById('huepanel');
  if(!panel)return;
  if(!h||!h.configured){panel.style.display='none';return;}
  panel.style.display='flex';
  const stEl=document.getElementById('huestate');
  const onBtn=document.getElementById('hueon');
  if(!h.online){
    stEl.textContent='Connecting';
    onBtn.className='huetoggle';onBtn.textContent='OFF';onBtn.setAttribute('aria-pressed','false');
    return;
  }
  stEl.textContent=(h.reachable?'Connected':'No response')+' · '+(h.reachable_count??h.lights.length)+'/'+(h.total??h.lights.length)+' lights';
  onBtn.className='huetoggle'+(h.on?' on':'');onBtn.textContent=h.on?'ON':'OFF';
  onBtn.setAttribute('aria-pressed',h.on?'true':'false');
  if(!hueDragging.bri&&typeof h.bri==='number'){
    document.getElementById('huebri').value=h.bri;
    document.getElementById('huebri').setAttribute('aria-valuetext',h.bri);
    document.getElementById('v-huebri').textContent=h.bri;
  }
  if(!hueDragging.ct&&typeof h.ct==='number'){
    document.getElementById('huect').value=h.ct;
    document.getElementById('huect').setAttribute('aria-valuetext',h.ct);
    document.getElementById('v-huect').textContent=h.ct;
  }
}
async function refresh(){
  try{
    const r=await fetch('/api/status');const s=await r.json();st=s;
    pollFail=0;
    document.getElementById('offline').className=s.online?'offline':'offline show';
    document.body.classList.toggle('offline-on',!s.online);
    document.getElementById('dot').className='dot '+(s.online?'ok':'bad');
    // Feature-detect optional integrations so a plain Sonos setup is not shown
    // a console full of dead controls. Runs before the offline early-return.
    const consoleEl=document.getElementById('console');
    if(consoleEl)consoleEl.style.display=s.mix_running?'':'none';
    const worldBtn=document.getElementById('b-world');
    if(worldBtn)worldBtn.style.display=s.world_running?'':'none';
    const ctrls=['vol','bass','treble','loudbtn','playbtn','mutbtn','mute2','prevbtn','nextbtn','b-world','b-linein','r-groovesalad','r-dronezone','r-secretagent','r-lush','shufbtn','repbtn','xfbtn'];
    if(!s.online){
      document.getElementById('name').textContent='Sonos';
      document.getElementById('nowsrc').textContent='';
      for(const id of ctrls)document.getElementById(id).disabled=true;
      renderMix(s.mix);
      renderHue(s.hue);
      return;
    }
    for(const id of ctrls)document.getElementById(id).disabled=false;
    if(busy)for(const id of busyBtns.concat(['vol','bass','treble'])){const b=document.getElementById(id);if(b)b.disabled=true;}
    document.getElementById('name').textContent=(s.name||'Sonos')+(s.model?' · '+s.model:'');
    const srcLabel={world:'WORLD',radio:'Radio',linein:'Line-in (DJ)',youtube:'YouTube',mix:'Mix',other:'—'}[s.source]||'—';
    const radioName=s.source==='radio'&&s.radio_key&&s.radios?s.radios[s.radio_key]:'';
    const switching=s.stale||Date.now()<switchUntil;
    document.getElementById('title').textContent=switching?'Switching…':(s.title||radioName||srcLabel);
    document.getElementById('meta').textContent=switching?'':(s.artist||'');
    const nowsrcEl=document.getElementById('nowsrc');
    const isPlaying=s.state==='PLAYING';
    const verb=isPlaying?'Playing':(s.state==='PAUSED_PLAYBACK'?'Paused':(s.state==='STOPPED'?'Stopped':'—'));
    const nowsrcTxt=switching?'Switching…':verb+': '+srcLabel+(s.source==='radio'&&s.radio_key&&s.radios?' ('+s.radios[s.radio_key]+')':'');
    if(nowsrcEl.textContent!==nowsrcTxt)nowsrcEl.textContent=nowsrcTxt;
    const dur=toSecs(s.duration), pos=toSecs(s.position);
    const seekable=!!s.seekable && dur>0 && !switching;
    const bar=document.getElementById('seekbar');
    bar.className='seekbar'+(seekable?'':' disabled');
    bar.setAttribute('aria-valuemax',dur||100);
    bar.setAttribute('aria-valuenow',pos);
    document.getElementById('tpos').textContent=fmtTime(s.position||'0:00');
    document.getElementById('tdur').textContent=s.duration?fmtTime(s.duration):'—';
    if(!seekDragging){
      const pct=dur>0?Math.min(100,(pos/dur)*100):0;
      document.getElementById('seekfill').style.width=pct+'%';
      document.getElementById('seekknob').style.left=pct+'%';
    }
    const pm=s.play_mode||'NORMAL';
    const shuf=pm==='SHUFFLE'||pm==='SHUFFLE_NOREPEAT';
    const repAll=pm==='REPEAT_ALL', repOne=pm==='REPEAT_ONE';
    const sb=document.getElementById('shufbtn');
    sb.className='djbtn'+(shuf?' on':'');sb.setAttribute('aria-pressed',shuf?'true':'false');
    document.getElementById('shuflbl').textContent=shuf?'SHUFFLE ON':'SHUFFLE';
    const rb=document.getElementById('repbtn');
    rb.className='djbtn'+((repAll||repOne)?' on':'');rb.setAttribute('aria-pressed',(repAll||repOne)?'true':'false');
    document.getElementById('replbl').textContent=repOne?'REPEAT 1':(repAll?'REPEAT ALL':'REPEAT');
    const xb=document.getElementById('xfbtn');
    xb.className='djbtn'+(s.crossfade?' on':'');xb.setAttribute('aria-pressed',s.crossfade?'true':'false');
    document.getElementById('xflbl').textContent=s.crossfade?'XFADE ON':'XFADE';
    const stEl=document.getElementById('state');
    const stMap={PLAYING:'Playing',PAUSED_PLAYBACK:'Paused',STOPPED:'Stopped',TRANSITIONING:'Switching',BUFFERING:'Buffering'};
    stEl.textContent=switching?'Checking':(stMap[s.state]||s.state||'—');
    stEl.className='badge'+(s.state==='PLAYING'&&!switching?' on':'');
    const artEl=document.getElementById('art');
    if(!switching&&s.artwork&&/^https?:/.test(s.artwork)){
      const px=artProxy(s.artwork);
      if(artEl.getAttribute('src')!==px){artEl.src=px;}
      artEl.classList.add('show');
      applyArtworkColor(px);
    }else{
      artEl.classList.remove('show');
    }
    const playing=s.state==='PLAYING';
    document.getElementById('playicon').innerHTML=playing?'<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>':'<path d="M8 5v14l11-7z"/>';
    document.getElementById('playbtn').setAttribute('aria-label',playing?'Pause':'Play');
    document.getElementById('muteicon').innerHTML=s.mute?'<path d="M4 9v6h4l5 5V4L8 9H4z"/><path d="M22 9l-6 6M16 9l6 6" stroke="currentColor" stroke-width="2" fill="none"/>':'<path d="M4 9v6h4l5 5V4L8 9H4z"/><path d="M16 8a5 5 0 010 8M18.5 5.5a9 9 0 010 13" stroke="currentColor" stroke-width="2" fill="none"/>';
    document.getElementById('mutbtn').className='tbtn'+(s.mute?' on':'');
    const m2=document.getElementById('mute2');
    if(m2){m2.className=s.mute?'on':'';m2.textContent=s.mute?'Muted':'Mute';m2.setAttribute('aria-pressed',s.mute?'true':'false');}
    if(typeof s.max_vol==='number'){document.getElementById('vol').max=s.max_vol;}
    const notDragging=id=>!dragging[id]&&!pending[id]&&document.activeElement!==document.getElementById(id);
    if(typeof s.volume==='number'&&notDragging('vol')){document.getElementById('vol').value=Math.min(s.volume,s.max_vol||40);document.getElementById('v-vol').textContent=s.volume;}
    if(typeof s.bass==='number'&&notDragging('bass')){document.getElementById('bass').value=s.bass;document.getElementById('v-bass').textContent=s.bass;}
    if(typeof s.treble==='number'&&notDragging('treble')){document.getElementById('treble').value=s.treble;document.getElementById('v-treble').textContent=s.treble;}
    paintAllKnobs();
    document.getElementById('loudbtn').className=s.loudness?'on':'';
    document.getElementById('loudbtn').textContent='Loudness '+(s.loudness?'ON':'OFF');
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
      if(yt.status==='downloading'){yh.className='ythint';yh.textContent='Loading YouTube…';}
      else if(yt.status==='error'){yh.className='ythint err';yh.textContent='Failed: '+(yt.error||'');}
      else if(s.source==='youtube'&&yt.title){yh.className='ythint ok';yh.textContent='Playing: '+yt.title;}
    }
    document.getElementById('foot').textContent='DJ bridge: '+(s.dj_running?'running':'stopped')+' · Auto-return: '+(s.world_running?(s.world_hold?'held':'on'):'stopped');
    renderMix(s.mix);
    renderHue(s.hue);
  }catch(e){
    pollFail++;
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
    ctx.strokeStyle='rgba(120,130,150,.35)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(4,mid); ctx.lineTo(w-4,mid); ctx.stroke();
    return;
  }
  const n=pk.length, gap=1, bw=Math.max(1,(w-gap*(n-1))/n);
  const md=(window.MIX&&window.MIX[deck])||{};
  const pos=md.pos||0;
  const dur=md.duration||d.dur||0;
  const prog=dur>0?Math.min(1,pos/dur):0;
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
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"dashboard: http://{shown}:{args.port}/  "
          f"(sonos: {SONOS_IP or 'auto-discover'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
