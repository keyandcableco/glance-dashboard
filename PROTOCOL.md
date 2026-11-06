# Glance Clock BLE protocol reference (stock firmware 1.6.7)

This adds to [Hypfer's write-up](https://github.com/Hypfer/glance-clock) and the README's protocol notes. It
covers firmware 1.6.7 (hardware 6.1, model 666), the newest image in Hypfer's glance-clock-assets (dated
2020-11-16). Firmware updates must be signed by Glance, so the stock firmware is what there is.

Every point is marked:

- **seen**: tried on a 1.6.7 clock (bench tests and video, November 2026);
- **expected**: read from the firmware images, not yet tried;
- **Hypfer**: from his write-up, not re-checked.

## Services

Seen in the clock's Bluetooth service list:

| Service | Characteristic | Use |
|---|---|---|
| Device Information (180a) | 2a24-2a29 | maker "Glance Tech Pte Ltd", model, hardware, firmware and software versions (seen) |
| Battery (180f) | 2a19 (read, notify) | battery level (seen: 100) |
| Glance `5075f606-1e0e-11e7-93ae-92361f002671` | `5075fb2e-…` "Data" (read, write) | write = a command (below); read = the current `Settings` (seen) |
| | `5075ffac-…` "Scene" (read, write, notify) | one byte, notified each time the screen changes (seen): the slot of the ring scene showing (2, 4, 8, 9 …), the slot of a Notify too (0 for the bridge's notices; a call in slot 103 reported 0x67), 0x86 for an appointments scene (seen in slots 1 and 7), 0x83 once while a timer ran, 0x81 for the clock's own screens (commands 35, 61, 70, 71 below, the battery screen, button presses, and briefly after some do-not-disturb and mute changes) |
| | `5075fc78-…` "State" (read, notify) | 16 flag bits, little-endian (seen): 0x8000 while a timer runs, 0x4000 while an alarm list is stored (command 21 cleared it, writing a disabled alarm set it again), 0x1000 after the power was plugged back in (charging is a guess; the battery read 100), 0x0200 while external power is connected, 0x0010 permanent do-not-disturb, 0x0008 permanent mute. Idle it read 0x6204 with a disabled alarm stored (0x2204 with none); 0x0004 and 0x2000 aren't worked out, and night mode, in light or dark, didn't change it |
| Nordic buttonless DFU `8e400001-f315-4f60-9fb8-838830daea50` | same UUID | firmware update; leave it alone |

Unplugging the clock's power, and plugging it back in, each put up a battery screen (a battery icon and the level, "100") for about 5 s (seen).

**The button** (seen): the clock has one button besides reset. One press while connected scrolls "I'm already connected!"; quick presses flip to the next screen; a press stops a ringing alarm. Held down, it powers the clock off (a purple ring animation and "BYE!"); a press powers it back on (a green sweep and "HELLO!"), after which it reads the time again when the central reconnects.

The clock is also a client: when it connects, it reads the Current Time Service (1805) from the central
(seen). It does this only on a fresh connection, so a bridge that restarts over a link that stays up doesn't
resync the clock's time; disconnecting and reconnecting does (seen).

## Writing a command

Every command is `[command, b1, b2, b3]` followed by its protobuf payload (if it has one), written to Data.
Send all four header bytes, even for the simple commands: Hypfer's "trailing zeroes can be omitted" doesn't
hold (seen: a bare `[10]` was ignored, `[10, 0, 0, 0]` stopped the timer).

Writes longer than the MTU go as long writes. The whole command must fit in **512 bytes**; a longer one gets
the standard "Prepare Queue Full" error (seen). A command the clock refuses comes back as an ATT error
**0x80 + code**:

| Code | ATT error | Meaning |
|---|---|---|
| 1 | 0x81 | unknown command (expected) |
| 2 | 0x82 | a payload command (0-8) shorter than 4 bytes (expected) |
| 3 | 0x83 | payload didn't decode, or is empty where something is required (seen: an appointments scene with no appointments) |
| 4 | 0x84 | a value out of range (seen: a sound number too big, a Gif type outside 10-17, a ring segment past the inner ring) |
| 5 | 0x85 | counts don't match, or a required part is missing (seen: appointment names not one per appointment) |
| 6 | 0x86 | wrong length (expected) |
| 7 | 0x87 | too many (seen: over 24 appointments, over 20 timer intervals; expected: 20 saved scenes, 5 alarms, 5 field-4 calendar alerts) |
| 8 | 0x88 | busy, or not allowed right now (expected) |

A Timer on the clock takes at most 20 intervals: 21 was refused (0x87), 20 accepted (seen). This repo's staged timers allow 20.

## Commands

Payload commands:

| Command | Header | Payload | What it does |
|---|---|---|---|
| 0 CustomScene | `0, priority, mode, slot` | CustomScene | saves a ring scene in a slot (see the README) (seen) |
| 2 Notify | `2, priority, 0, slot` | Notice | an alert (seen) |
| 3 Timer | `3, 0, 0, 0` | Timer | countdown on the rings (seen) |
| 4 Alarms | `4, 0, 0, 0` | Alarms | replaces the alarm list (below) (seen) |
| 5 Settings | `5, 0, 0, 0` | Settings | replaces the settings (below) (seen) |
| 6 Call | `6, 83, 0, 103` (Hypfer's) | Notice | an incoming call. Seen once with a Pulse-type Notice: all four rings pulsed in its colour with its text scrolling for over 20 s, longer than a normal turn, and stopped when slot 103 was deleted (`33, 0, 0, 103`; a button press came in the same second). The app deletes slot 103 when the call ends (Hypfer) |
| 7 Forecast | `7, priority, mode, slot` | ForecastScene | 12-hour graph plus a text template (seen) |
| 8 Appointments | `8, 0, mode, slot` | AppointmentsScene | schedule blocks on the rings (seen) |

Simple commands, always as four bytes:

| Command | Header | What it does |
|---|---|---|
| 10 TimerStop | `10, 0, 0, 0` | stops the timer (seen) |
| 20 AlarmStop | `20, 0, 0, 0` | accepted, but does **not** silence a ringing alarm (seen) |
| 21 AlarmClear | `21, 0, 0, 0` | clears the alarms (seen) |
| 30 ScenesStop | `30, 0, 0, 0` | blanks the face ~1 s, then moves to a screen and restarts the rotation's timing (seen; Hypfer calls it "previous scene") |
| 31 ScenesStart | `31, 0, 0, 0` | jumps to the next screen at once (seen: twice, within a second each time) |
| 32 ScenesClear | `32, 0, 0, 0` | deletes every saved scene; the clock face hides until something arrives (Hypfer) |
| 33 ScenesDelete | `33, 0, 0, slot` | deletes one slot (seen) |
| 34 ScenesDeleteMany | `34, 0, to, from` | deletes a range of slots (seen once: after `34, 0, 9, 8`, test slots 8 and 9 stopped appearing in the rotation for the 75 s watched, while slots 2 and 4 kept coming round) |
| 46 | `46, 0, 0, 0` | accepted, no visible effect (seen once) |
| 35 UpdateAndRefresh | `35, 0, 0, 0` | shows a cloud, an arrow and a clock for ~2 s, then the screens carry on (seen) |
| 45 Sound | `45, 0, 0, n` | plays sound n; `45, 0, 0, 0` stops it; too big an n is refused with 0x84 (seen) |
| 60, 61 | `60, 0, 0, 0` / `61, 0, 0, 0` | 61 shows the brightness screen, a sun icon and the level, until 60 ends it (seen once, ~16 s, at maximum brightness: "100%") |
| 70 | `70, 0, 0, 0` | shows an 8-digit hex number, scrolled twice (seen). The first six digits changed between readings; the last byte was 0x64 each time, with the battery at 100 each time (not tried at another level); the rest is unknown |
| 71 | `71, 0, 0, 0` | shows the plain time screen (time only, rings blank) for ~5 s (seen): the screen that came at the top of the hour with Settings 15 set |

Not to be sent to a clock in use:

| Command | Why |
|---|---|
| 42 BondsClear | deletes all pairings (Hypfer) |
| 50 ClearUserInfo | deletes all pairings and powers off (Hypfer) |
| 43, 44 | start / confirm hand calibration (Hypfer). They move the hands, and a wrong confirm leaves them off until calibrated again |
| 40, 41 | Hypfer's constants call them ANCS (iPhone notification centre) start/stop, which is the app's "private mode"; his command table calls them automatic night mode on/off (expected: ANCS). Untested |

Any other command byte is refused with 0x81 (expected).

## Settings (command 5)

**A Settings write replaces all the settings.** Read Data first, change only the field you mean, and write the
whole message back. A field left out probably goes back to its default (expected; seen: writing back an
old copy of the settings undid a newer quiet-hours change). A client must keep the fields its proto doesn't
know (15-18 below) when it rewrites Settings, or it resets them. Leaving out field 18 moves the clock.

Fields in Hypfer's proto that behave differently on 1.6.7:

| Field | Behaviour on 1.6.7 |
|---|---|
| 6, 7 mgrSilentIntervalMin/Max | don't exist; setting them does nothing (seen) |
| 13 mgrUserActivityTimeout | seconds, default 600: how long after the last write before the always-on dots go out (expected). Set to 30, nothing visible changed (seen twice, the second time in an empty, dark room for 100 s, with night mode off) |

Fields not in his proto:

| Field | Type | Meaning |
|---|---|---|
| 15 | uint8 | an hourly chime: a sound number for the top of each hour, 0 = off (expected). Seen with 1 set: at 19:00 the plain time screen ~5 s and, in the phone's recording, a ~1 kHz tone just after the hour (found in the audio, not heard by a person). With 6 at 18:00 the same screen but no clear sound. Whether that screen comes every hour without 15 set is unknown |
| 16 | int32, minutes | a scheduled clock shift: how far to move the clock |
| 17 | uint32, seconds | when to make that shift, in local wall time written as if UTC (as for scene timestamps) |
| 18 | int32, minutes | an offset added to the time the clock reads from the central. Changing it moves the clock by the difference at once (seen: +60, then back to 0). Moving back an hour, the hands seemed to go forward 11 hours; the digital time changes at once |

Fields 16 and 17 together (seen once): +60 at a set moment moved the digital time on within seconds of that
moment, and the hands swept forward an hour. A second shift has not worked yet: after the first, the clock's
own count of seconds isn't simply an hour on, so a second moment written that way never came. A fresh
connection puts the time right again (seen). After a shift, set 16 and 17 back to 0. This is probably how the
app handles daylight saving while disconnected (expected).

## Which screen shows

A scene or Notify in mode 0 shows at once only if its priority is higher than the screen showing now;
otherwise it waits its turn in the rotation (seen: a priority-48 Notify gets past a priority-40 scene but
not an 80 one). Mode-8 scenes never jump the queue (seen). While some state flag is set (possibly quiet
hours), a new scene also needs priority 64 or more (expected). Do-not-disturb holds back alerts:
`permanentDND` swallows them, and a recurring quiet-hours window lets only priority 80 through (seen).

## Notice (command 2)

- `type` takes 0 (none), 1 Pulse, 2 Wave, 10-17 (the ring animations Fire … Cloud) and 101-112 (the weather
  animations); anything else is refused with 0x84 (expected).
- **Field 5** (fixed32, seconds in local wall time as if UTC): a time already past shows the notice at
  once (seen); a future time holds it back, and in the one clean try it never came out (seen). Leave it unset.

## CustomScene (command 0)

- **Field 2** (fixed32, seconds in local wall time as if UTC) is a **start time**: the scene is saved but
  first shows at that time. Seen once, in slot 1 at priority 80, mode 0: sent at 5:27:31 for 5:29, it
  appeared at 5:29 within seconds.

## Alarms (command 4)

`Alarms { repeated AlarmData alarm = 1 }` with Hypfer's AlarmData (enabled, days, time, sound, text), at most
5 (expected). Each write replaces the whole list. An empty list is refused (seen), so to clear send command
21 (seen) or one disabled alarm (accepted, seen).

- `days` is a weekday mask. Seen: with one alarm per bit from 2 to 6, only bit 3 rang on a Thursday. So bit 0
  is Monday and bit 6 Sunday (expected from the order); 0xFF rings every day (seen).
- At the alarm minute the clock rings with the alarm's sound and shows its text, and keeps ringing until the
  button on the side of the clock is pressed (seen). Command 20 doesn't stop it (seen); rewriting the list
  while it rings is untried.

## Appointments (command 8)

The appointment format is in the README. 1.6.7 ignores the reminder: every appointment alert comes at the
appointment's start, whatever the reminder says (seen: reminders of 3 and 10 minutes did nothing early).
Firmware 1.5.9 and 1.5.10 alerted at start − reminder (expected). Two extra fields:

- **Field 4** (repeated uint32, up to 5): `(minute of the day << 8) | weekday mask`. At each such minute the
  clock breaks in with the schedule: the scene's sound, the face dark except the appointment's block
  blinking at its place on the dial, and its name scrolling (seen with one appointment and five entries for
  consecutive minutes, mask 0x7F: each came 0.4-8 s after its minute). The mask's bit order is unverified
  (probably as for alarms). Probably the app's "On Time" calendar mode (a guess from Glance's help pages).
- **Field 5** (uint32, four bytes): the same schedule alert, repeating (seen in four trials):
  - `0x7F | 76 << 8 | 80 << 16 | 1 << 24` (19:00-20:00, every 15 min): alerts at 19:15 and 19:30, each about
    14 s after the quarter hour;
  - slot 7, header mode 0, `0x7F | 80 << 8 | 88 << 16 | 1 << 24` (20:00-22:00): at 20:45, 11 s after;
  - slot 7, header mode 8, `0x7F | 0 << 8 | 95 << 16 | 4 << 24` (the whole day, every hour): at 21:00, 2 s after.

  Reading the bytes as a weekday mask (byte 0), a window of the day in 15-minute slots (bytes 1 and 2) and a
  step in slots (byte 3) fits all four, but hasn't been checked byte by byte (no trial at a time it should
  skip). In mode 0 the appointments scene never joined the normal rotation; in mode 8 it did, as a screen of
  its own (Hypfer: 0 = alerts only, 8 = alerts and the clock face). A nonzero byte 0 needs a nonzero byte 3,
  or the scene is refused with 0x85 (expected). Probably the app's "Recurring" calendar mode (a guess from
  Glance's help pages).

## Text

The text line decodes **UTF-8** (seen, from notices of four bytes each):

- ASCII letters and `{ | } ~` show as themselves (the rest of the printable range wasn't swept); 0x7F
  shows `?`.
- Single bytes 0x80-0x99 are the icons in Hypfer's list: house, phone, clock, plug, smartphone, sun,
  smiley, a full block (0x87), blank (0x88), batteries empty to full (0x89-0x8C), bell crossed out, speaker
  crossed out, thermometer, drop, heart, barometer, `|<`, `>|`, umbrella, bell, speaker, wind, cloud.
  0xA2 shows ¢, 0xA3 £ and 0xB0 ° (Hypfer's 176). The rest of 0x9A-0xBF show `?`.
- Sequences: `C2 B0` shows °, `C3 A9` é, `E2 82 AC` € and `C3 C4` Ä (the decoder doesn't check that the
  second byte is a proper continuation byte). Every Latin-1 letter U+00C0-00FF (× and ÷ not tried) sent
  as proper UTF-8 (`C3 80`-`C3 BF`) drew as its own accented glyph (seen, 2026-11-05 21:35, five letters a
  notice). Other two- and three-byte sequences tried showed one `?` each; valid four-byte ones weren't tried. An unfinished sequence at the end of the text is dropped, and
  0xFB-0xFF showed nothing.
- Control bytes: a leading 0x0A hid the whole text. 0x14-0x1D draw wider icons that scroll, some of them
  animated: batteries that look low or charging, with waves moving to and from them; a boxed Z; music notes
  bouncing with sound waves moving out (around 0x1A-0x1D); possibly three dots in turn (unsure). 0x01-0x13 (but
  0x0A) and 0x1E-0x1F show `?`.

## Not worked out yet

State bits 0x0004 and 0x2000; command 70's middle digits; whether Settings 15 picks the chime's sound; the exact icons
behind control bytes 0x14-0x1D; how the built-in demo, hand calibration and pairing screens are triggered,
and what the reset button does; how night mode uses the light sensor beyond hiding the dots (seen: with night mode on and the lights off, the 12 dots went out within ~50 s and stayed out for the 20 s watched after the lights came back; any dimming can't be judged on an auto-exposed camera); what Settings 13 changes (nothing seen with night mode on either).
