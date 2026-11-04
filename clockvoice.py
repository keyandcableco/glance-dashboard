#!/usr/bin/python3
"""Spoken clock commands: the text of a dictation that starts with "clock" (or follows a "hey glance" wake word).

    clock, timer 30 minutes                      clock, remind me in 20 minutes to call Sam
    clock, timer an hour and a half for lunch    clock, Sam is picking up the Juno at 2:30
    clock, pickup Sam in 40 minutes              clock, show back in 5
    clock, clear                                 clockvoice.py --dry-run "clock, timer 30 minutes"
    clock, family, timer 10 minutes              (a clock's name first sends it to that clock)

Stdlib only (runs under the system python). The bridge's /voice does the work: these
patterns first (instant), Claude Haiku for looser phrasing. The reply is printed and spoken with $GLANCE_SAY (default ~/.local/bin/say-aloud) if it exists.
--dry-run shows only what the patterns make of it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

BRIDGE = os.environ.get("GLANCE_URL", "http://127.0.0.1:8740")
SAY = os.environ.get("GLANCE_SAY", os.path.expanduser("~/.local/bin/say-aloud"))  # any "say this text" command

UNITS = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
         "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def words_to_digits(text):
    """'thirty five minutes' -> '35 minutes', 'an hour and a half' -> '90 minutes'."""
    t = text.lower()
    t = re.sub(r"\b(?:an?|one) (?:hour and a half|and a half hours?)\b", "90 minutes", t)
    t = re.sub(r"\b(?:half an hour|a half hour)\b", "30 minutes", t)
    t = re.sub(r"\b(?:a quarter of an hour|quarter of an hour|a quarter hour)\b", "15 minutes", t)
    t = re.sub(r"\b(\d+) and a half hours?\b", lambda m: f"{int(m[1]) * 60 + 30} minutes", t)

    def number(m):
        return str(sum(TENS.get(p, UNITS.get(p, 0)) for p in m[0].replace("-", " ").split()))
    names = "|".join(sorted(list(TENS) + list(UNITS), key=len, reverse=True))
    t = re.sub(rf"\b({names})(?:[ -]({'|'.join(UNITS)}))?\b(?= (?:minutes?|mins?|hours?|hrs?|seconds?)\b)", number, t)
    return t


def duration(t):
    """'1 hour 30 minutes' / '45 minutes' / '2 hours' -> bridge 'when' like '1h30' / '45m', else None."""
    h = re.search(r"(\d+) ?(?:hours?|hrs?)\b", t)
    m = re.search(r"(\d+) ?(?:minutes?|mins?)\b", t)
    if not h and not m:
        return None
    return f"{h[1]}h{m[1] if m else ''}" if h else f"{m[1]}m"


def clock_time(t):
    """'2:30', '2:30 pm', '14:30', '3 pm', '3 o'clock' -> bridge 'when', else None."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))? ?(a\.?m\.?|p\.?m\.?|o'?clock)?(?!\s*(?:minutes?|mins?|hours?|hrs?)\b)", t)
    if not m or not (m[2] or m[3]):
        return None
    suffix = (m[3] or "").replace(".", "")
    suffix = "" if "clock" in suffix else suffix
    return f"{m[1]}:{m[2] or '00'}{suffix}"


def strip_time(t):
    t = re.sub(r"\b(?:in|for|at|by)?\s*\d+ ?(?:hours?|hrs?)(?:\s*(?:and\s*)?\d+ ?(?:minutes?|mins?))?\b", " ", t)
    t = re.sub(r"\b(?:in|for|at|by)?\s*\d+ ?(?:minutes?|mins?)\b", " ", t)
    t = re.sub(r"\b(?:in|for|at|by|around)?\s*\d{1,2}(?::\d{2})? ?(?:a\.?m\.?|p\.?m\.?|o'?clock)?\b", " ", t)
    return " ".join(t.split())


def tidy(name):
    name = re.sub(r"^(?:to|for|that|about)\s+", "", " ".join(name.split()).strip(" ,.;:!?-"))
    return name.upper()[:40].strip()  # the clock reads best in capitals, like its other labels


def parse(text, names=()):
    """Spoken text (after 'clock') -> (path, body, spoken reply) or None. A leading clock name
    (one of `names`) picks that clock."""
    t = words_to_digits(text).strip(" ,.;:!?")
    t = re.sub(r"^(?:hey\s+)?(?:clock|glance)\b[\s,.:;!-]*", "", t)
    target = next((n for n in names if re.match(rf"{re.escape(n.lower())}(?:'s)?(?: clock)?\b", t)), None)
    if target:
        t = re.sub(rf"^{re.escape(target.lower())}(?:'s)?(?: clock)?\b[\s,.:;!-]*", "", t)
    parsed = parse_command(t)
    if parsed and parsed[0] == "/note":  # keep the note as said, not lowercased
        m = re.search(r"(?:note to self|make a note|take a note|jot down|note)\b[\s,:;-]*(?:that\s+|to\s+)?(.+)",
                      text, re.IGNORECASE)
        parsed[1]["text"] = m[1].strip(" .,;:!?") if m else parsed[1]["text"]
    if parsed and target:
        parsed[1]["clock"] = target
    return parsed


# Changing or cancelling something already set: left to the bridge's Haiku. Only at the start, so
# "remind me in 20 minutes to change the strings" is still an instant new timer.
EDIT = re.compile(r"^(?:please\s+)?(?:cancel|stop the|kill|delete|remove|never ?mind|turn off|shut off|rename|call (?:it|that|the)|"
                  r"push|move|change|snooze|extend|make (?:it|that|the)|give (?:it|me|that|the)|"
                  r"(?:\w+\s+)?more (?:minutes?|time))\b")


def parse_command(t):
    if not re.match(r"(?:note|make a note|take a note|jot down|note to self)\b", t) and EDIT.match(t):
        return None
    if re.fullmatch(r"(?:clear|clear (?:it|all|everything)|all clear|dismiss(?: all)?)", t):
        return "/unread/clear", {}, "Cleared."
    if m := re.match(r"(?:note|make a note|take a note|jot down|note to self)\b[\s,:;-]*(?:that\s+|to\s+)?(.+)", t):
        return "/note", {"text": m[1]}, None
    if m := re.match(r"(?:show|say|display|put up)\s+(.+)", t):
        return "/notify", {"text": m[1].upper()[:120], "color": "White", "sound": "NoneSound",
                           "priority": "critical"}, "On the clock."
    if re.search(r"\bpick(?:ing)?\s*-?\s*up\b|\bpickup\b|\bcoming (?:in|by)\b", t):
        when = duration(t) or clock_time(t)
        if not when:
            return None
        rest = strip_time(t)
        rest = re.sub(r"\b(?:is|are|will be|'s)?\s*(?:picking up|pick up|pickup|coming in|coming by|coming)\b", " ", rest)
        return "/pickup", {"kind": "pickup", "name": tidy(rest) or "someone", "when": when}, None
    if re.search(r"\b(?:timer|remind|alarm|wake me|ping me|nudge me)\b", t):
        when = duration(t) or clock_time(t)
        if not when:
            return None
        rest = strip_time(t)
        rest = re.sub(r"\b(?:set\s+)?(?:a\s+)?(?:timer|alarm)\b|\bremind me\b|\bping me\b|\bnudge me\b", " ", rest)
        return "/pickup", {"kind": "timer", "name": tidy(rest), "when": when}, None
    return None


def say(text):
    """Speak the reply with SAY if there is one; print it either way."""
    print(text)
    if shutil.which(SAY):
        subprocess.Popen([SAY, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def post(path, body):
    req = urllib.request.Request(BRIDGE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:  # /voice may wait on Haiku
        return json.loads(r.read())


def spoken_due(item):
    label = {"pickup": "Pickup", "timer": "Timer", "stop": "Hard stop"}.get(item["kind"], "Set")
    name = f" for {item['name']}" if item.get("name") and item["kind"] == "timer" else (f", {item['name']}" if item.get("name") else "")
    hhmm = item["text"].rsplit(" ", 1)[-1]
    return f"{label}{name}, at {hhmm}."


def clock_names():
    try:
        with urllib.request.urlopen(BRIDGE + "/status", timeout=5) as r:
            return [c["name"] for c in json.loads(r.read())["clocks"]]
    except Exception:
        return []


def main():
    """Hand the words to the bridge (patterns there first, then Claude Haiku) and say its reply."""
    text = sys.argv[-1]
    if sys.argv[1] == "--dry-run":
        print(parse(text, clock_names()))
        return
    try:
        say(post("/voice", {"text": text})["reply"])
    except Exception:
        say("The clock bridge isn't answering.")
        sys.exit(1)


if __name__ == "__main__":
    main()
