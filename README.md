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
  priority. Long text is split into pieces the clock shows in turn. The clock reads text as UTF-8 and draws
  ASCII, every Latin-1 letter (À-ÿ), ° € ¢ £ and its icons; other letters lose their accents (ł as l),
  curly quotes and dashes go as straight ones, and emoji are left out. The panel's play button plays a sound on the clock.
- **Sources**, per clock: new emails in a Thunderbird inbox matching subject rules; desktop notifications
  from chosen people (e.g. texts via Google Messages for Web); Google Calendar events (15 min, 5 min, now)
  from any number of calendars, added and removed on the panel; anything that can `curl` (a Claude Code
  hook is in `contrib/`).
- **Timers, pickups and hard stops**, with the clock's own **ring countdown**. A hard stop nags at 30, 15
  and 5 minutes, then repeats "STOP NOW" every 5 minutes for half an hour unless cleared.
- **Light flash** for anything due now (a timer, pickup or stop going off, a calendar event starting): all
  four rings flash red and white for 3 seconds before the text. `/notify` takes `"flash": true` too.
- **Schedule ring**: the next 11 hours of calendar events (aqua), pickups (gold) and hard stops (red) as
  blocks around the dial at their times, drawn by the clock's own appointments display. It stops at
  midnight.
- **Staged timers** on the clock's own multi-interval timer: each stage counts down on the ring with its
  label on the text line. Cap reforming ("50V 15 → 75V 15 → 100V 15 → 120V 15"), glue-ups, burn-in,
  lacquer coats, or focus rhythms ("hey glance, focus" = WORK 25 / BREAK 5, four rounds ending on WORK).
  Presets on the panel. Up to 20 stages and 24 hours; labels up to 24 characters. A running staged timer
  takes the ring ahead of other countdowns (their alerts still fire), alerts once at the end, and can be
  renamed or cancelled but not moved.
- **Forecast graph**: the next 24 hours of humidity, temperature or chance of rain drawn around the ring
  by the clock's own forecast display, with the current value on the text line (US National Weather
  Service, or Open-Meteo for non-commercial use elsewhere). It's on the clock part of the time, by
  default 2 minutes in every 10 (`every_minutes`, `show_minutes`; `every_minutes = 0` for always).
- **Unread list** that replays silently every 5 minutes until cleared.
- **Status ring**, always on under the clock's time and date: one colored segment per unread item on the
  outer ring (the oldest 12; the oldest one pulses), and, while break reminders are on, your current active
  stretch growing toward the break nudge on the inner ring. It steps aside for alerts.
- **Animation screen**: the clock's own fire animation over its time while a burn-in runs (a timer or
  staged timer with "burn" in its name or any stage), and a holiday animation on holidays.
- **Screensaver** (off unless switched on): something different each time its screen comes round: the
  clock's own animations (fire, flowers, fan, wheel, sun, storm, clouds) in changing colours; rain, snow
  and fog; drawn ones where every LED has its own colour (a rainbow with a wave over it, confetti, a
  sweep, a rainbow painting itself in).
- **Claude's face** (with a session tracker set up, see `config.example.toml`): an emoji face on the
  rings for your Claude Code sessions: dizzy and red when a turn died on an error, wide-eyed and blinking
  when one waits on you, a winking smile when one finished since you last looked, eyes looking around
  while they work, and a pip at 12 o'clock for each session that needs you and you haven't looked at
  yet. Nothing to show, no screen.
- **Words on the rings**: a screen of two lines drawn in letters round the four rings (a 3×4 font, about
  6 letters a half; the bottom line is turned over so it reads upright). It says what a words source says:
  a command of your own, run every few minutes, that prints the few words that matter (your next to-do,
  say; see `config.example.toml`). Words typed on the panel show instead, for the minutes you pick or until
  you clear them. Nothing to say, no screen.
- **Screens**: the clock shows its time and date, the status ring, the forecast, the schedule ring, the
  animation screen, the screensaver, Claude's face and the words in turn, about 15 seconds each (the clock
  picks the next one, often the same one again). Switch each of the last seven on or off per clock on the
  panel (it's kept in `screens-<name>.json`); `status_ring = false`, `schedule_ring = false`,
  `animation_screen = false`, `screensaver = true` or `words_screen = false` in a clock's section only sets
  where it starts.
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
- For break nudges: an X11 session and libXss (Debian: `libxss1`). The bridge uses `$DISPLAY`, or `:0` when
  it has none (a user service started at boot), read by a short-lived helper process every 30 s; with no
  desktop (before login, after logout) it counts only `$GLANCE_VOICE_FILE`.
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
A user service runs only while you're logged in; `loginctl enable-linger` starts it at boot instead,
and keeps it running after you log out.

The bridge skips clocks that aren't paired (connecting to an unpaired one makes many desktops pop a
Bluetooth notice every few seconds) and checks again every minute.

To use the panel from another device, put a password in `panel_password` (any username works) and restart
the bridge. Without that file, only the computer running the bridge can use the panel and API. It's plain
HTTP: use it on a network you trust, or over a VPN such as Tailscale. Requests from the bridge's own computer
skip the password, so don't put a reverse proxy on that computer in front of it (every request would look
local). Pages from other sites open in a browser are refused, on this computer or another, even with the password saved.

### A shop app (optional)

With `[shop_app] url` set, voice commands can clock in and out (`GET /api/clock`, `POST /api/clock/toggle`),
say what you're working on (`POST /api/clock/job {"text"}`), and track waiting on a customer
(`POST /api/followups {"text", "days"}`, `POST /api/followups/resolve {"text"}`). Follow-ups that come due
(`GET /api/followups/due`, items with `id, label, waiting_on`) are nagged on the first clock at
`followup_hours` (default 10 and 15) and dropped from the clock once resolved. Any app with these endpoints
works (ours is a private Flask shop system), given these replies: `GET /api/clock` → `{"clocked_in", "since"}`;
the toggle → `{"since", "to_assign"}`; other POSTs → an object whose `say` is spoken (or `"ok": false` on
failure); `/api/followups/due` → `{"due": [...]}`.

Pickups are shared with it too (`pickupsync.py`, every minute): one added or changed on a clock is sent
(`POST /api/pickups {"name", "at", "source": "glance", "ref"}`, `"at"` ISO 8601 with offset, `"ref"` the clock
item's id so a resend updates the same one, plus `"id"` once known; the reply's `pickup.id` is kept), one
cleared before its time is cancelled (`POST /api/pickups/cancel {"id"}`), and `GET /api/pickups`
(`{"pickups": [{"id", "name", "at", "source"}], "cancelled": [ids]}`) brings the shop app's own ones onto the
first clock and takes the cancelled ones off; one it doesn't list at all is sent again. A shop app without
these endpoints (404) is left alone.
Calendar events with a UID like `pickup-12@keyandcable-shop` (our shop app's calendar feed) are skipped by the
calendar watcher, since those pickups are already on the clock.

### Environment variables

`GLANCE_CONFIG` (path to the config), `GLANCE_URL` (bridge address for `pickup`, `clockvoice.py` and the
hook; default `http://127.0.0.1:8740`), `GLANCE_SAY` (a command that speaks its argument), `GLANCE_VOICE_FILE`
(a file a dictation listener touches when it hears speech, counted as activity), `GLANCE_REPLAY_MINUTES`
(how often unread items replay, default 5), `ANTHROPIC_API_KEY`.

## API

All on `http://127.0.0.1:8740`; every call takes an optional `clock` (name), defaulting to the first clock.

| Call | Body / query | |
|---|---|---|
| `POST /notify` | `text, color, anim, sound, mode, priority, keep, flash` | Show an alert; `keep` adds it to unread, `flash` opens with the light flash |
| `POST /pickup` | `kind` (`pickup`, `timer` or `stop`), `name`, `when` (`40m`, `30s`, `1h30`, `2:30pm`, `14:30`) | |
| `POST /stages` | `name`, `stages` (`[["50V", 15], …]` or `[{"label", "minutes"}]`), `repeat` | Up to 20 stages |
| `POST /voice` | `text` | A spoken sentence; returns `reply` to say back |
| `GET /unread`, `POST /unread/clear` | `id` (none: all) | |
| `GET/POST /notes` | `text` to add; `id` with `done` or `delete` | |
| `GET/POST /settings` | `brightness, night_mode, dnd, dnd_from, dnd_till` | The clock's own settings |
| `GET/POST /nudges` | `breaks, break_minutes, pause_minutes, closing, closing_days, snooze_minutes` | |
| `GET/POST /calendars` | `add: {name, url}` (a secret iCal address, checked by reading it once) or `remove: id` | A clock's calendars by name and host; the addresses stay in `calendars-<name>.json` (mode 0600) and are never sent back |
| `GET/POST /message` | `top, bottom, top_color, bottom_color, minutes` (default 5; 0: until cleared), or `clear: true` | Typed words for the words screen, in place of the words source's; up to 16 characters a line. Returns `typed`, `said` (the words source's last words), `source`, `error` |
| `GET/POST /screens` | `status, forecast, schedule, animation, screensaver, claude, words` (each `true` or `false`) | Which screens the clock shows in turn; applied within half a minute |
| `POST /sound` | `sound` (a name; `NoneSound` stops it) | Play one of the clock's sounds; the bridge stops it after 5 s |
| `GET /status`, `GET /options` | | Connection, unread counts, the active stretch toward a break nudge (`focus`); the names of colors, sounds, icons |

`glance.py --list` prints the colors, animations, sounds and icons (`{bell}`, `{heart}`, …). `/notify` refuses
an unknown `{name}`; everywhere else (email subjects, calendar titles, names) it's shown as plain text.

## Protocol notes (beyond Hypfer's write-up)

The full reference, with every command, error code and setting found so far, is in [`PROTOCOL.md`](PROTOCOL.md). The essentials:

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
- **Appointments** (`8,0,8,7` + `AppointmentsScene`; Hypfer's notes leave the data format open).
  `appointmentData` is 8 bytes per appointment: a little-endian uint32 `start | length<<11 | reminder<<22`
  (11, 11 and 10 bits: start in minutes after the scene's `timestamp`, length in minutes, reminder in
  minutes before, 1023 = none), then blue, green, red, and the height (3, 6, 9 or 12 = one to four rings,
  from the outside in), with one `appointmentNames` entry each. All 144 appointments in his
  `sample_appointments.js` captures re-encode exactly. The bridge writes local midnight (as if UTC) as the
  timestamp, which draws correctly on a real clock; the app also used noon, with starts past 24 hours.
  Overlapping appointments get stepped heights (12, 9, 6, 3), taller ones first, so all stay visible.
- **Light flash**: a CustomScene with a Fill and an `AreaAnimation` of type `LightFlash` over the same
  area (`flashLight.color`, `speed`). Like any scene it's saved and keeps cycling, so delete its slot
  after it has played.
- The clock reads the Current Time Service from the central when it connects, and only then, so the
  bridge drops its link once a night (3:30, after a daylight-saving change) and reconnects to set the time.
- A short-lived second BLE client to the same clock (a one-off script like `set_setting.py`) drops the
  bridge's link when it disconnects; the bridge reconnects within seconds.

## Files

`glanced.py` the bridge · `glance.py` protocol helpers + one-shot CLI · `config.py` · `mailwatch.py`,
`textwatch.py`, `calwatch.py` sources · `pickups.py` timers/pickups/stops · `unread.py` · `nudges.py` ·
`notes.py` · `statusring.py` (and the light flash) · `schedulering.py` · `forecast.py` · `animscreen.py` · `screensaver.py` · `claudeface.py` Claude's face · `screens.py` · `calendars.py` · `ringwords.py` words on the rings · `shopapp.py` optional shop app · `brain.py` Haiku · `clockvoice.py` voice front end · `panel.html` · `pickup` CLI ·
`set_setting.py` · `contrib/` · `PROTOCOL.md` the protocol reference

## License

MIT (see `LICENSE`). `glance/glance.proto` is based on Hypfer's `Glance.proto`, released under the WTFPL.
