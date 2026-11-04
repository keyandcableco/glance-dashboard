# glance-dashboard

A cloudless bridge that brings the Glance Clock (a smart wall clock whose company and app are gone) back to life as a
workshop dashboard, on its **stock firmware**, over Bluetooth LE. No app, no cloud account.

It turns the clock into the thing it was always meant to be: a glanceable display for what needs
you right now, and a nudge out of hyperfocus when time's running out.

Made by [The Key & Cable Company](https://keyandcable.com), a vintage synth and keyboard repair shop,
for its own workbench.

The Bluetooth protocol was reverse-engineered by **Hypfer** in
[Hypfer/glance-clock](https://github.com/Hypfer/glance-clock) (WTFPL). `glance/glance.proto` is a copy
of his `Glance.proto`, fixed so `protoc` accepts it. Everything here builds on that work.

## What it does

- **Alerts** on the 32×8 display and the four 48-LED rings: text, built-in icons, color, animation, sound,
  priority. Long text is split into pieces the clock shows in turn.
- **Sources**, per clock: new emails in a Thunderbird inbox matching subject rules; desktop notifications
  from chosen people (e.g. texts via Google Messages for Web); Google Calendar events (15 min, 5 min, now);
  anything that can `curl` (a Claude Code hook is in `contrib/`).
- **Timers, pickups and hard stops**, with the clock's own **ring countdown**. A hard stop nags at 30, 15
  and 5 minutes, then repeats "STOP NOW" every 5 minutes for half an hour unless cleared.
- **Staged timers** on the clock's own multi-interval timer: each stage counts down on the ring with its
  label on the text line. Cap reforming ("50V 15 → 75V 15 → 100V 15 → 120V 15"), glue-ups, burn-in,
  lacquer coats, or focus rhythms ("hey glance, focus" = WORK 25 / BREAK 5, four rounds ending on WORK).
  Presets on the panel. Up to 24 stages and 24 hours; labels up to 24 characters. A running staged timer
  takes the ring ahead of other countdowns (their alerts still fire), alerts once at the end, and can be
  renamed or cancelled but not moved.
- **Forecast graph**: the next 24 hours of humidity, temperature or chance of rain drawn around the ring
  by the clock's own forecast display, with the current value on the text line (US National Weather
  Service, or Open-Meteo for non-commercial use elsewhere).
- **Unread list** that replays silently every 5 minutes until cleared.
- **Status ring**, always on under the clock's time and date: one colored segment per unread item on the
  outer ring (the oldest 12), and, while break reminders are on, your current active stretch growing
  toward the break nudge on the inner ring. It steps aside for alerts and countdowns.
  Set `status_ring = false` in a clock's section to turn it off (the next start removes it from the clock).
- **Hyperfocus nudges**: a break reminder after a long stretch of keyboard/mouse activity, wrap-up and
  closing-time nudges. Snooze from the panel.
- **Voice**: `clockvoice.py` takes a transcribed sentence ("Sam's swinging by for the Juno after lunch").
  Common phrasings are matched instantly; anything looser goes to Claude Haiku with a small set of tools:
  timers, pickups, hard stops, staged timers, messages, notes, changing what's set ("rename the 8:53 timer to bake",
  "push Sam back to 3", "five more minutes", "move that to the kitchen clock"), cancelling, clearing,
  and, with an optional shop app,
  clocking in and out, the job you're on now, and waiting-on-customer follow-ups. Hook it to whatever dictation you use; it prints the reply and speaks it with
  `$GLANCE_SAY` (default `~/.local/bin/say-aloud`) if that command exists.
- **Notes to self** ("note: order more 2.2k resistors") kept as a checklist.
- **Several clocks** by name, each with its own sources, unread list and settings.
- **Web control panel** at `http://localhost:8740/`, optionally reachable from your phone with a password.
- Keeps the clock's time set (it asks the connected computer for the time; the bridge answers).

## Requirements

- Linux with BlueZ ≥ 5.50 (`bluetoothctl`) and a Bluetooth LE adapter. Python ≥ 3.11.
  Developed on Debian 12 with BlueZ 5.66.
- For text alerts from desktop notifications: `dbus-monitor` (Debian: `dbus-bin`).
- For break nudges: an X11 session and libXss (Debian: `libxss1`). The bridge needs `DISPLAY`: if it runs
  as a user service that starts before your desktop, run `systemctl --user import-environment DISPLAY XAUTHORITY`
  from your session startup and restart it, or break nudges silently fall back to `$GLANCE_VOICE_FILE` only.
- For loose voice phrasing: an Anthropic API key (`anthropic_api_key` file or `ANTHROPIC_API_KEY`).

## Setup

```bash
git clone https://github.com/keyandcableco/glance-dashboard ~/glance-clock && cd ~/glance-clock
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then edit
```

### Pairing (the fiddly part)

1. **Power-cycle the adapter first.** Scans can miss the clock entirely until you do:
   `bluetoothctl` → `power off`, `power on`, `agent on`, `default-agent`, `menu scan`, `transport le`, `back`, `scan on`.
2. The clock shows up as `GlanceClock`. **Press the clock's button** (it shows a clock-and-phone animation),
   then **immediately** `pair <address>`. Wait too long and you get `ConnectionAttemptFailed`.
3. Put the address in `config.toml`.

### Settings worth checking on a used clock

- **Permanent Do Not Disturb** (hidden setting) silently swallows every alert. Turn it off:
  `.venv/bin/python set_setting.py permanentDND false`
- **Brightness**: the low byte of `displayBrightness` is a fixed level; 0 means the light sensor (dim).
  Set it on the panel.
- **Night mode** dims the clock and hides the hour dots; **quiet hours** hold back all but critical alerts.
  Both are on the panel.

### Run

```bash
.venv/bin/python glanced.py
```

or as a user service: `cp contrib/glanced.service ~/.config/systemd/user/ && systemctl --user enable --now glanced`.

The bridge skips clocks that aren't paired (connecting to an unpaired one makes many desktops pop a
Bluetooth notice every few seconds) and checks again every minute.

To use the panel from another device, put a password in `panel_password` (any username works) and restart
the bridge. Without that file, only the computer running the bridge can use the panel and API. It's plain
HTTP: use it on a network you trust, or over a VPN such as Tailscale. Requests from the bridge's own computer
skip the password, so don't put a reverse proxy on that computer in front of it (every request would look
local). Web pages open in your browser can't use that shortcut: other sites' requests are refused.

### A shop app (optional)

With `[shop_app] url` set, voice commands can clock in and out (`GET /api/clock`, `POST /api/clock/toggle`),
say what you're working on (`POST /api/clock/job {"text"}`), and track waiting on a customer
(`POST /api/followups {"text", "days"}`, `POST /api/followups/resolve {"text"}`). Follow-ups that come due
(`GET /api/followups/due`, items with `id, label, waiting_on`) are nagged on the first clock at
`followup_hours` (default 10 and 15) and dropped from the clock once resolved. Any app with these endpoints
works (ours is a private Flask shop system), given these replies: `GET /api/clock` → `{"clocked_in", "since"}`;
the toggle → `{"since", "to_assign"}`; other POSTs → an object whose `say` is spoken (or `"ok": false` on
failure); `/api/followups/due` → `{"due": [...]}`.

### Environment variables

`GLANCE_CONFIG` (path to the config), `GLANCE_URL` (bridge address for `pickup`, `clockvoice.py` and the
hook; default `http://127.0.0.1:8740`), `GLANCE_SAY` (a command that speaks its argument), `GLANCE_VOICE_FILE`
(a file a dictation listener touches when it hears speech, counted as activity), `GLANCE_REPLAY_MINUTES`
(how often unread items replay, default 5), `ANTHROPIC_API_KEY`.

## API

All on `http://127.0.0.1:8740`; every call takes an optional `clock` (name), defaulting to the first clock.

| Call | Body / query | |
|---|---|---|
| `POST /notify` | `text, color, anim, sound, mode, priority, keep` | Show an alert; `keep` adds it to unread |
| `POST /pickup` | `kind` (`pickup`, `timer` or `stop`), `name`, `when` (`40m`, `1h30`, `2:30pm`, `14:30`) | |
| `POST /stages` | `name`, `stages` (`[["50V", 15], …]` or `[{"label", "minutes"}]`), `repeat` | Up to 24 stages |
| `POST /voice` | `text` | A spoken sentence; returns `reply` to say back |
| `GET /unread`, `POST /unread/clear` | `id` (none: all) | |
| `GET/POST /notes` | `text` to add; `id` with `done` or `delete` | |
| `GET/POST /settings` | `brightness, night_mode, dnd, dnd_from, dnd_till` | The clock's own settings |
| `GET/POST /nudges` | `breaks, break_minutes, pause_minutes, closing, closing_days, snooze_minutes` | |
| `GET /status`, `GET /options` | | Connection, unread counts; the names of colors, sounds, icons |

`glance.py --list` prints the colors, animations, sounds and icons (`{bell}`, `{heart}`, …). `/notify` refuses
an unknown `{name}`; everywhere else (email subjects, calendar titles, names) it's shown as plain text.

## Protocol notes (beyond Hypfer's write-up)

- **Custom scenes** show only with a nonzero priority byte: `[0, priority, mode, slot]`. Mode 8 draws on the
  rings while keeping the digital time and date; mode 0 takes the whole display.
- **Ring segments** pack as `color<<16 | extra<<14 | length<<8 | offset<<6 | start`: start 0 is
  12 o'clock running clockwise, 48 pixels per ring, length is pixels − 1, offset picks the starting ring
  (0 = outermost, 3 = innermost; the 12 hour-marker LEDs sit outside ring 0), extra is how many more rings
  inward (0 = one ring, 3 = all four). offset + extra over 3 is rejected (GATT error 0x84). Several Fill
  objects can go in one scene; a Text object can't.
- **One display at a time**: a Notify replaces a ring scene and vice versa.
- **Text**: about 32 bytes of one text piece fit; a Notice can carry several pieces, shown in turn.
  The default `Repeat` modifier shows each piece twice.
- **Timer** (`3,0,0,0` + `Timer`) counts down on the ring; with several `intervals`, each with a TextData
  label, it runs them back to back, showing each label. **TimerStop must be 4 bytes** (`10,0,0,0`).
- The clock reads the Current Time Service from the central when it connects.
- A short-lived second BLE client to the same clock (a one-off script like `set_setting.py`) drops the
  bridge's link when it disconnects; the bridge reconnects within seconds.

## Files

`glanced.py` the bridge · `glance.py` protocol helpers + one-shot CLI · `config.py` · `mailwatch.py`,
`textwatch.py`, `calwatch.py` sources · `pickups.py` timers/pickups/stops · `unread.py` · `nudges.py` ·
`notes.py` · `statusring.py` · `forecast.py` · `shopapp.py` optional shop app · `brain.py` Haiku · `clockvoice.py` voice front end · `panel.html` · `pickup` CLI ·
`set_setting.py` · `contrib/`

## License

MIT (see `LICENSE`). `glance/glance.proto` is based on Hypfer's `Glance.proto`, released under the WTFPL.
