# now-spinning

A "now playing" screen for a record player.

Vinyl has no metadata stream — the only thing a turntable emits is sound. So
`now-spinning` listens. It runs on a Raspberry Pi mounted near the deck, hears the
music through a microphone, identifies it, and puts the cover art on the attached
display with the track, artist, and album.

![The display showing an album cover in a record sleeve beside the track, artist and album](docs/screenshot.png)

Everything on that screen is configurable: which lines of text appear and what
they say, the fonts and their colours, whether the record shows behind the cover,
the shadow under it, and whether the background is flat or a blurred wash of the
artwork. Switch all the text off and the cover fills the panel on its own.

## How it works

```
mic ─▶ capture ─▶ music gate ─▶ 8s clip ─▶ Shazam ─▶ cover art
                                                        │
                                                   NowPlaying
                                                   ┌────┴────┐
                                              pygame       web
```

The engine only sends audio anywhere once it is confident music is actually
playing, and a match then buys a quiet period with no further lookups. That keeps
request volume low and stops one side of a record from generating dozens of calls.

The parts that make it usable next to a real turntable are all in the policy:

- **A missed match never blanks the screen.** Fingerprinting fails on worn
  pressings, long fades, and drum solos. The last known track stays up.
- **Quiet passages are not the end of the record.** Closing the gate takes 20
  seconds of sustained silence, with a lower threshold than opening it.
- **Side flips are expected.** After the music stops the display lingers for 90
  seconds before going idle.
- **Room noise is not music.** A level threshold alone would fire every time the
  furnace kicks on, so the gate also measures spectral flatness — noise is flat,
  music is peaky.
- **Track changes are caught.** Once the quiet period is over it re-identifies on
  a cadence, so an album progresses without anyone touching it.

## Requirements

- **A 64-bit OS.** `shazamio-core` ships `manylinux_2_28_aarch64` wheels, so
  64-bit Raspberry Pi OS (Bookworm or later) installs with no Rust toolchain.
  32-bit `armv7l` has no wheel and is not supported.
- **Python 3.10–3.13.** On 3.13 an extra dependency (`audioop-lts`) is installed
  automatically, because a transitive dependency still imports the stdlib
  `audioop` module that 3.13 removed. Not 3.14: `shazamio-core` has no wheel for
  it yet.
- A Raspberry Pi 3 or newer (a Pi Zero 2 W works), a USB microphone, and a display.
- `libportaudio2` for microphone capture.

Any microphone PortAudio can open will do. Capture negotiates its format at
startup rather than demanding one, so a 16 kHz USB conference mic and a 96 kHz
audio interface both work with no configuration — it tries the configured rate,
then the device's own, then 48000, 44100, 32000, 24000, 22050, 16000, 96000,
88200, 20000, 11025 and 8000, and logs what it settled on. Mono and stereo inputs
are both fine; stereo is downmixed.

## Install

```bash
sudo apt install -y python3-venv libportaudio2
git clone https://github.com/untraceablez/now-spinning.git
cd now-spinning
python3 -m venv .venv
.venv/bin/pip install -e '.[all]'
```

Extras: `[pygame]` for the framebuffer display, `[web]` for the browser display,
`[all]` for both.

> **Boot to a console, not a desktop.** The display draws straight to the
> framebuffer through KMS/DRM, and a desktop compositor holds that device
> exclusively — SSHing in does not change that. `sudo raspi-config` → System
> Options → Boot / Auto Login → **Console Autologin**. Running inside a desktop
> session works too, but only from inside it, not over SSH.

## First run

Work up from the microphone. Each step tells you whether to bother with the next.

**1. Find the microphone.**

```bash
.venv/bin/now-spinning devices
```

**2. Check the levels it actually sees**, with a record playing:

```bash
.venv/bin/now-spinning calibrate --device "USB Audio"
```

Music should sit comfortably above `detect.start_threshold_dbfs` and the room
below `detect.silence_threshold_dbfs`. Adjust until `MUSIC` appears when a record
is on and `quiet` when it is not.

**3. Confirm recognition works** before involving the display at all:

```bash
.venv/bin/now-spinning identify --device "USB Audio"
```

**4. Run it.**

```bash
.venv/bin/now-spinning run                       # the display, fullscreen
.venv/bin/now-spinning run --backend web --port 8000
.venv/bin/now-spinning run --backend both
.venv/bin/now-spinning run --demo --windowed     # no microphone, placeholder tracks
```

`--demo` is the quickest way to see layout changes without waiting for a match.

## Configuration

Copy `config.example.yaml` to `config.yaml` and change what you need — every key
has a working default, so the file only needs your overrides. That file documents
every option inline and is the complete reference; the tables below cover the
ones worth knowing about first.

Searched in order:

1. `--config PATH`
2. `$XDG_CONFIG_HOME/now-spinning/config.yaml` (usually `~/.config/…`)
3. `~/.now-spinning.yaml`
4. **`config.yaml` in the working directory** — the checkout, or whatever the
   systemd unit's `WorkingDirectory` points at
5. `/etc/now-spinning/config.yaml`

On startup the log says which one it read, or lists everywhere it looked:

```
INFO nowspinning: config: /home/pi/now-spinning/config.yaml
```

If a change seems to be ignored, check that line first — it is usually a file in
a place nothing reads.

### Audio

| Key | Default | What it does |
| --- | --- | --- |
| `audio.device` | system default | Device index, or a substring of its name so it survives renumbering |
| `audio.sample_rate` | `16000` | *Preferred* rate; capture negotiates to one the device accepts |
| `audio.clip_seconds` | `8.0` | How much audio each lookup gets |

### The music gate

Tune these with `calibrate`; the right numbers depend entirely on mic gain and
placement.

| Key | Default | What it does |
| --- | --- | --- |
| `detect.start_threshold_dbfs` | `-45.0` | Level at which the gate opens |
| `detect.silence_threshold_dbfs` | `-50.0` | Level below which quiet starts counting |
| `detect.max_flatness` | `0.35` | Above this the input reads as noise, not music |
| `detect.silence_seconds` | `20.0` | Sustained quiet before the gate closes |

### Recognition

| Key | Default | What it does |
| --- | --- | --- |
| `recognizer.quiet_period_seconds` | `60.0` | Lookup-free window after a fresh match |
| `recognizer.recheck_interval_seconds` | `45.0` | Cadence for catching track changes |
| `recognizer.linger_seconds` | `90.0` | How long the last track survives silence |
| `recognizer.stale_after_seconds` | `300.0` | Give up on a track not reconfirmed in this long |

Lowering the first two makes track changes appear sooner at the cost of more
requests. Around 20–30 s is a sensible floor — this is an unofficial endpoint.

### Display and layout

| Key | Default | What it does |
| --- | --- | --- |
| `display.backend` | `pygame` | `pygame`, `web`, `both`, or `none` |
| `display.width` / `.height` | `0` | Render resolution; `0 × 0` uses the panel's native size |
| `display.device_index` | system default | Which `/dev/dri/card` to draw on; needed for a GPIO/SPI panel |
| `display.style` | `sleeve` | `sleeve` (cover in a record sleeve) or `record` (cover on a spinning platter) |
| `display.rpm` | `33.333` | Rotation speed — `45.0` for a single |
| `display.fullscreen` | `true` | |

`0 × 0` asks the panel what it is. Pin both if a panel misreports itself, or to
render below native and save a little CPU.

### The sleeve

| Key | Default | What it does |
| --- | --- | --- |
| `display.show_vinyl` | `true` | The record behind the cover, turning |
| `display.show_gloss` | `true` | The jacket's gloss and shading over the cover |
| `display.show_shadow` | `true` | A soft shadow under the cover |
| `display.shadow_offset_x` / `_y` | `0.008` / `0.016` | Offset, as a fraction of the cover, so it holds its proportions on any panel |
| `display.shadow_blur` | `0.5` | `0` is a hard edge, `1` a wide haze |
| `display.shadow_opacity` / `_color` | `0.5` / `#000000` | |

With the record hidden the cover grows into the space it leaves, so the artwork
stays the same size on screen and the text does not shift.

### Text

| Key | Default | What it does |
| --- | --- | --- |
| `display.show_heading` | `true` | The "NOW SPINNING" label |
| `display.show_title` / `show_artist` / `show_album` | `true` | The three metadata lines |
| `display.heading_text` | auto | Your own words in place of "Now spinning" |
| `display.text_outline` | `false` | Outline every line — worth it over the artwork background |
| `display.text_outline_color` / `_width` | `#000000` / `2` | Colour, and maximum thickness |

Whatever line ends up first sits at the top, so switching one off closes the gap
rather than leaving a hole. `heading_text` only applies while a track is showing —
"Listening" and "Ready" still say what they are doing.

**Turn all four off** and the artwork centres and fills the panel, with no idle
message either. That is the art-only display.

### Background

| Key | Default | What it does |
| --- | --- | --- |
| `display.background` | `#101014` | The flat colour |
| `display.background_mode` | `solid` | `solid`, or `artwork` for a blurred zoom of the cover |
| `display.background_blur` | `0.75` | `0` leaves the cover sharp, `1` is a wash |
| `display.background_dim` | `0.55` | How far to darken it towards `background` |
| `display.foreground` / `accent` | `#f5f2ea` / `#c8a24a` | Default text colours |

Blur alone does not make pale artwork safe to put text on, which is what
`background_dim` is for. `text_outline: true` pairs well with this mode.

### The browser display

`display.backend: web` or `both` also serves the page at `web.host:web.port`. It
is not a second design — it reads the same config and lays out the same
composition, from the same `sleeve.png` and the same geometry, so the two look
alike. Fonts are served from the Pi's own cache rather than linked from Google,
so a wall-mounted tablet with no route out still gets the right typeface.

Everything on this page applies to it: style, the sleeve layers, the shadow, the
text toggles, the artwork background, and the outline. It updates over
server-sent events, and the record turns with a CSS animation, so an idle
browser is not repainting from JavaScript.

### Typography

Each line picks its own family, weight and slant, so one family can carry the
whole layout:

| Role | Default |
| --- | --- |
| `heading` — the "NOW SPINNING" label | Bitter Regular 400 |
| `title` — the track | Bitter Bold 700 Italic |
| `artist` | Bitter SemiBold 600 |
| `album` | Bitter Light 300 Italic |

Each takes `family`, `weight` (100–900), `italic`, and `color` (`null` to use the
theme's). `fonts.source` decides where the files come from:

- **`google`** (default) downloads each face once and caches it under
  `$XDG_CACHE_HOME/now-spinning/fonts`. Only the first run needs the network, and
  if it cannot get there the display falls back to the built-in font rather than
  refusing to start.
- **`local`** reads `.ttf`/`.otf` out of `fonts.directory`, matched by family,
  weight and slant — `Bitter-BoldItalic.ttf` and `Bitter-700italic.ttf` both
  work, and subfolders are searched.
- **`builtin`** uses the font shipped with pygame and never touches the network.

`display.font_path` overrides every role at once, for setups that want a single
face everywhere.

## Small SPI displays

The cheap 3.5" 480×320 panels that sit on the GPIO header (ILI9486 — Waveshare
"RPi LCD 3.5", and the Inland/GoodTFT clones of it) work, but **not** via the
vendor's `LCD35-show` script. That script installs a legacy `fbtft` framebuffer
driver and edits `/boot/config.txt`, which moved to `/boot/firmware/config.txt`;
on a Pi 5 it typically leaves you with no display output at all. The `fbcp`
mirroring trick is also dead on Pi 5 — it used DispmanX, which that board removed.

Use the in-tree overlay's DRM mode instead. In `/boot/firmware/config.txt`:

```
dtoverlay=piscreen,drm,speed=16000000,fps=30
```

The `drm` flag is the important part: it binds the KMS/DRM `ili9486` driver
rather than `fbtft`, giving a real `/dev/dri/card*` node that SDL can render to.
Then point this program at that card:

```yaml
display:
  device_index: 1
  width: 480
  height: 320
```

There is usually more than one card (`vc4` for HDMI, `v3d` for the GPU, and the
panel), so check which index is the panel rather than assuming:

```bash
for c in /sys/class/drm/card?; do
  printf '%s -> %s\n' "$(basename "$c")" "$(basename "$(readlink -f "$c/device/driver")")"
done
```

**Do not add `rotate=90`.** The DRM driver's native mode is already 480×320
landscape, and `mipi_dbi_rotate_mode()` *swaps* width and height for 90 and 270 —
so `rotate=90` turns the panel portrait, which is the opposite of what the
parameter name suggests. Add it only if you actually want 320×480. (The
parameter reads the other way round for the legacy fbtft driver, which is where
the confusion comes from.)

**If the colours are wrong, suspect `speed` before anything else.** These panels
are driven over SPI with no error checking, so a clock the wiring cannot sustain
corrupts pixel data in transit. In RGB565 a few flipped bits move red into the
blue field, which looks convincingly like an RGB/BGR mismatch but is really
signal integrity. The overlay's own default is 24 MHz and not every board or
ribbon manages it — drop to `speed=16000000`, and lower again if needed. Suspect
a genuine colour-order problem only once a slower clock has been ruled out.

## Running as a service

The unit expects the checkout at `/opt/now-spinning`, which is the tidier place
for something that runs at boot:

```bash
sudo mv ~/now-spinning /opt/now-spinning
sudo chown -R pi:pi /opt/now-spinning
sudo usermod -aG audio,video pi

sudo cp /opt/now-spinning/systemd/now-spinning.service /etc/systemd/system/
sudo systemctl enable --now now-spinning
journalctl -u now-spinning -f
```

**To leave the checkout where it is** — in your home directory, say — override
the paths instead of moving anything:

```bash
sudo systemctl edit now-spinning
```

```ini
[Service]
WorkingDirectory=/home/pi/now-spinning
ExecStart=
ExecStart=/home/pi/now-spinning/.venv/bin/now-spinning run
ProtectHome=
```

`ExecStart=` has to be cleared before being set again or systemd appends to it,
and `ProtectHome=` has to be cleared or the unit cannot read its own venv.

The unit sets `SDL_VIDEODRIVER=kmsdrm`, so the Pi has to boot to a console — see
the note under [Install](#install). It also points `XDG_CACHE_HOME` at a
directory systemd creates for it, because cover art and downloaded fonts would
otherwise land in `~/.cache`, which `ProtectHome=read-only` makes unwritable.

## Troubleshooting

**No input device found.** `now-spinning devices` shows nothing → install
`libportaudio2`, confirm `arecord -l` sees the mic, and check group membership.

**`Package 'now-spinning' requires a different Python`.** Your `python3` is newer
than the supported range — most likely 3.14, since 3.13 is supported. Check with
`python3 --version`, then build the venv with an interpreter in range:
`sudo apt install python3.13-venv && python3.13 -m venv .venv`.

**`Invalid sample rate [PaErrorCode -9997]`.** The device runs at one fixed rate
(48 kHz on most USB interfaces) and PortAudio opens it through ALSA's raw device,
which does not resample. Capture negotiates this automatically — so if you still
see this, every format was refused. Check whether another program has the device
open, and try the other index the same interface exposes: ALSA usually lists a
card several times and only one entry is usable.

**Never matches anything.** Run `now-spinning identify` — it prints the captured
level. Below about −40 dBFS is too quiet; raise the gain in `alsamixer` or move
the mic closer to the speakers. Recognition is much better on the loud middle of a
track than on a fade-in.

**Config changes seem to be ignored.** Check the `config:` line in the startup
log. It names the file that was read, or lists every path it tried.

**`pygame.error: kmsdrm not available`.** A desktop compositor (wayfire, labwc,
Xorg) already holds the DRM device, and SDL cannot take it while that is running.
This is the normal state on a Raspberry Pi OS *desktop* image, and SSHing in does
not change it. Boot to the console instead — `sudo raspi-config` → System Options
→ Boot / Auto Login → **Console Autologin**. Running from inside the desktop
session works too: the display falls back to the `wayland` driver when
`SDL_VIDEODRIVER` is not pinned.

**Matches, but the screen stays blank.** Check the backend: `--backend both` and
load `http://<pi>:8000` to see whether the problem is recognition or rendering.

**Black screen on a Pi with no desktop.** The service user needs `video` group
membership and access to `/dev/dri/card*`.

**Black screen on a GPIO/SPI panel.** A SPI panel is a *second* DRM device —
HDMI is `card0`, the panel is usually `card1` — and SDL takes `card0` unless told
otherwise, so it renders to the port nobody is looking at. Check `ls /dev/dri/`,
then set `display.device_index: 1`. See [Small SPI displays](#small-spi-displays).

**Gate never opens.** Run `calibrate`. If levels look right but the gate stays
shut, the input is probably failing the flatness test — raise `max_flatness`
toward `0.5`. The threshold does not need adjusting for a different sample rate:
flatness is measured over a fixed 50 Hz–8 kHz band precisely so that one setting
means the same thing on a 16 kHz mic and a 96 kHz interface.

**Cover art never appears under the service.** The cache has to be writable. The
shipped unit handles that with `CacheDirectory=` and `XDG_CACHE_HOME`; if you
wrote your own, set `cache_dir` in the config to somewhere the service user can
write.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest
ruff check . && ruff format --check . && mypy nowspinning
```

Nothing in the suite touches the network or a sound card, so it runs anywhere —
and a fixture enforces that, failing any test that reaches for a font download.

Adding another recognition provider means implementing `Recognizer` in
`nowspinning/recognize/base.py` — two methods — and registering it in
`build_recognizer`. Nothing in the engine, the capture layer, or either renderer
needs to know.

`tools/clean_sleeve.py` regenerates the sleeve artwork from the original, and
documents what it removes and why.

## A note on Shazam

Recognition goes through [`shazamio`](https://github.com/shazamio/ShazamIO), an
**unofficial** client. It works well and needs no signup, but it is not a
supported API and could break without warning. This project is for personal use
next to your own turntable. If you need something contractual, ACRCloud and AudD
both sell proper APIs, and the `Recognizer` interface is there so you can drop one
in.

Not affiliated with, endorsed by, or sponsored by Apple Inc. or Shazam
Entertainment Ltd.

## Roadmap

- Additional recognition providers (ACRCloud, AudD)
- Optional scrobbling to Last.fm / ListenBrainz
- Discogs lookup for pressing details
- Play history and per-record statistics

## License

Apache License 2.0 — see [LICENSE](LICENSE).
