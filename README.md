# soluna-web

A single-file browser dashboard for a **Sonos** speaker. Open one page on your
phone or laptop and control playback, radio, line-in, volume, EQ, play mode,
crossfade, and optional Philips Hue lights — without the Sonos app.

**Core needs only a Sonos and `soco`.** The 4-deck mixer console and the WORLD
source button are optional: they light up only when the matching local daemon
is running, and are hidden otherwise. Those daemons are separate programs and
are *not* included here — the dashboard just talks to their HTTP endpoints.

Everything site-specific (speaker IP, ports, helper daemons, Hue pairing) is
configuration, not code. With no configuration at all it discovers a Sonos on
your network over SSDP and serves a working dashboard on `127.0.0.1:8100`.

```
┌─────────────┐   HTTP    ┌──────────────┐   UPnP/SOAP   ┌────────┐
│  browser    │ ────────► │  soluna-web  │ ────────────► │ Sonos  │
└─────────────┘           └──────┬───────┘               └────────┘
                                 │ optional
                    ┌────────────┼─────────────┐
                    ▼            ▼             ▼
              WORLD daemon   mixer daemon   Hue bridge
```

## Features

Core (needs only a Sonos and `soco`):

- play / pause / next / previous, mute, volume with a configurable ceiling
- bass / treble / loudness
- shuffle, repeat, crossfade
- SomaFM radio presets (Groove Salad, Drone Zone, Secret Agent, Lush)
- line-in (DJ) source switching
- now-playing artwork proxied through the dashboard
- live status polling that backs off when the speaker is unreachable

Optional (each one is detected; when absent the UI just shows it as stopped):

- **YouTube** — paste a URL, audio is extracted with `yt-dlp` + `ffmpeg` and
  served to the speaker from the dashboard. Supports HTTP Range requests.
- **Mixer** — a separate local daemon exposing `/mix/*`; when it answers, the
  dashboard shows the 4-deck console, crossfader and live waveforms. Hidden
  when the daemon is not running.
- **WORLD** — a separate local stream daemon; when its launchd label is
  configured and running, the WORLD source button appears.
- **Hue** — brightness, color temperature and presets for the lights you list.

## Requirements

- Python 3.9+
- [`soco`](https://github.com/SoCo/SoCo) (`pip install -r requirements.txt`)
- A Sonos speaker on the same network
- Optional: `yt-dlp` and `ffmpeg` on `PATH` for YouTube

## Install and run

```bash
git clone https://github.com/yukihamada/soluna-web.git soluna-web
cd soluna-web
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# local only, auto-discover the speaker
python3 soluna_web.py

# reachable from your phone on the LAN
python3 soluna_web.py --host 0.0.0.0

# pin the speaker instead of discovering it
python3 soluna_web.py --sonos 192.168.1.50
```

Then open <http://127.0.0.1:8100/> (or `http://<your-lan-ip>:8100/`).

> **No authentication.** The dashboard can change your speaker's volume and
> source. Only bind to `0.0.0.0` on a network you trust.

## Configuration

All settings are environment variables; CLI flags override the two that have
them (`--host`, `--port`, `--sonos`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `SOLUNA_SONOS` | *(empty → SSDP discover)* | Speaker IP address |
| `SOLUNA_HOST` | `127.0.0.1` | Bind address (`--host`) |
| `SOLUNA_WEB_PORT` | `8100` | Dashboard port (`--port`) |
| `SOLUNA_MAX_VOL` | `40` | Hard volume ceiling |
| `SOLUNA_BRAND` | `SOLUNA` | Name shown in the page title and labels |
| `SOLUNA_STATE_DIR` | `$XDG_RUNTIME_DIR/soluna-web` or `$TMPDIR/soluna-web` | Hold files, YouTube cache |
| `SOLUNA_WORLD_PREFIX` | `world-` | Filename prefix for the hold files (`<prefix>hold`, `<prefix>pause`, …) |
| `SOLUNA_YT_CACHE` | `$SOLUNA_STATE_DIR/yt` | Where extracted YouTube audio is cached |
| `SOLUNA_PUBLIC_HOST` | *(auto-detected LAN IP)* | Host the speaker should fetch streams from |
| `SOLUNA_YTDLP` | *(found on `PATH`)* | Path to `yt-dlp` |
| `SOLUNA_FFMPEG_DIR` | *(dir of `ffmpeg` on `PATH`)* | Passed to `yt-dlp --ffmpeg-location` |
| `SOLUNA_HUE_CONFIG` | `~/.config/soluna-web/hue.json` | Hue pairing file |

Optional helper daemons — leave empty unless you run them:

| Variable | Meaning |
| --- | --- |
| `SOLUNA_WORLD_PORT` | Port of the WORLD stream daemon (default `8099`) |
| `SOLUNA_MIX_PORT` | Port of the mixer daemon (default `8110`) |
| `SOLUNA_WORLD_LABEL` | launchd label of the WORLD daemon, for the status readout |
| `SOLUNA_MIX_LABEL` | launchd label of the mixer daemon |
| `SOLUNA_DJ_LABEL` | launchd label of a MIDI/controller bridge |

The `*_LABEL` variables are only probed with `launchctl list` on macOS. On other
platforms they are ignored and the feature reports as stopped.

### Hue

Create `~/.config/soluna-web/hue.json` (mode `600`) after pairing with your
bridge — press the bridge button, then create a user via the Hue API:

```json
{
  "bridge": "192.168.1.20",
  "username": "<application-key-from-the-bridge>",
  "lights": ["1", "2"]
}
```

This file holds a credential. It is git-ignored; never commit it.

### Hold-file protocol (WORLD daemon)

If you run a separate stream daemon, it and the dashboard coordinate through
small files in `SOLUNA_STATE_DIR`:

| File | Meaning |
| --- | --- |
| `<prefix>hold` | Dashboard has taken the speaker; the daemon must not reclaim it |
| `<prefix>pause` | Ask the daemon to pause |
| `<prefix>now` | Currently playing item, written by the daemon |
| `<prefix>current.json` | Structured now-playing state |

`<prefix>` is `SOLUNA_WORLD_PREFIX` (default `world-`). If your daemon already
uses flat names, point both at the same place:

```bash
SOLUNA_STATE_DIR=/tmp SOLUNA_WORLD_PREFIX=soluna-world- SOLUNA_YT_CACHE=/tmp/soluna-yt \
  python3 soluna_web.py
```

Any daemon that honours these files can be used; the dashboard only reads and
writes them.

## Running as a service (macOS)

`docs/local.soluna-web.plist.example` is a launchd template. Copy it to
`~/Library/LaunchAgents/`, replace `__PYTHON__`, `__SCRIPT__` and the
environment values, then:

```bash
launchctl load ~/Library/LaunchAgents/<label>.plist
```

On Linux, the same script works under a systemd unit with
`Environment=` lines for the variables above.

## API

The page talks to a small JSON API, usable on its own:

- `GET /api/status` — full state (source, volume, EQ, mode, mix, Hue, YouTube)
- `POST /api/action` — `{"cmd": "toggle" | "next" | "volume" | ...}`
- `GET /api/art?u=<url>` — artwork proxy (allow-listed hosts only)
- `GET /api/wave?deck=A&n=240` — waveform peaks, when the mixer daemon runs
- `GET /yt/<id>.mp3` — cached YouTube audio with Range support

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The smoke tests start the server on a free port with the speaker pointed at
`127.0.0.1` (nothing listening), so they cover the "Sonos unreachable" path,
brand templating, the artwork allow-list and unknown-command handling. No real
speaker is required.

## Security notes

- No authentication and no TLS. Treat it as a LAN appliance, and only bind to
  `0.0.0.0` on a network you trust.
- Writes (`POST /api/action`) require `Content-Type: application/json` and a
  same-origin `Origin`/`Referer`, so a random web page cannot change your
  speaker through a plain HTML form.
- The artwork proxy only follows redirects to allow-listed hosts and matches
  real domain boundaries (`somafm.com`, `*.somafm.com` — never `evilsomafm.com`).
- The artwork proxy only fetches allow-listed hosts (SomaFM, YouTube image
  hosts, and the configured speaker/LAN address).
- The Hue application key lives in a local file outside the repo.

## License

MIT — see `LICENSE`.
