#!/usr/bin/python3
"""Spoken clock commands: the text of a dictation that starts with "clock" (or follows a "hey glance" wake word).

    clock, timer 30 minutes                      clock, remind me in 20 minutes to call Sam
    clock, timer an hour and a half for lunch    clock, Sam is picking up the Juno at 2:30
    clock, pickup Sam in 40 minutes              clock, show back in 5
    clock, Sam's dropping off a Rhodes at 3      clock, Sam dropped off the Rhodes   (confirms it)
    clock, Dean's coming at 3                    clock, Bill will be here Friday around noon  (a later day: the shop app)
    clock, clear                                 clockvoice.py --dry-run "clock, timer 30 minutes"
    clock, family, timer 10 minutes              (a clock's name first sends it to that clock)
    clock, focus 50                              (staged: WORK 50 / BREAK 10, three rounds, ending on WORK)

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
    t = re.sub(r"\bhalf a minute\b", "30 seconds", t)
    t = re.sub(r"\b(\d+) and a half hours?\b", lambda m: f"{int(m[1]) * 60 + 30} minutes", t)

    def number(m):
        return str(sum(TENS.get(p, UNITS.get(p, 0)) for p in m[0].replace("-", " ").split()))
    names = "|".join(sorted(list(TENS) + list(UNITS), key=len, reverse=True))
    t = re.sub(rf"\b({names})(?:[ -]({'|'.join(UNITS)}))?\b(?= (?:minutes?|mins?|hours?|hrs?|seconds?)\b)", number, t)
    # clock times said as words: "at five", "around five thirty", "five pm", "noon", "half past four"
    t = re.sub(r"\b(?:noon|midday)\b", "12:00pm", t)
    hours = "|".join(sorted(HOURS, key=len, reverse=True))
    mins = r"(?:oh )?(?:(?:twenty|thirty|forty|fifty)(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?|ten|fifteen|five)"
    t = re.sub(rf"\b(?:half past|quarter past) ({hours})\b",
               lambda m: f"{HOURS[m[1]]}:{'30' if m[0].startswith('half') else '15'}", t)

    def clock_words(m):
        minute = sum(TENS.get(p, UNITS.get(p, 0)) for p in (m[3] or "").replace("-", " ").split() if p != "oh")
        return f"{m[1] or ''}{HOURS[m[2]]}{f':{minute:02d}' if m[3] else ''}"
    t = re.sub(rf"\b((?:at|around|about|by|after|till|until) )({hours})(?: ({mins}))?\b(?! (?:minutes?|mins?|hours?|hrs?|seconds?)\b| (?:past|to|after|of|till|before) (?:the )?(?:hour|\d|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b)",
               clock_words, t)
    t = re.sub(rf"\b()({hours})(?: ({mins}))?(?= ?(?:a\.?m\.?|p\.?m\.?|o'?clock)\b)", clock_words, t)
    return " ".join(t.split())


HOURS = {w: UNITS[w] for w in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                               "eleven", "twelve")}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_WORDS = re.compile(r"\b(?:(?:on|this|next|for)\s+)?(today|tonight|tomorrow|" + "|".join(DAYS) + r")"
                       r"(?:\s+(morning|afternoon|evening|night))?\b")


def day_said(t):
    """'tomorrow', 'friday', 'today' (also for 'tonight'), or '' when no day is said; and 'am'/'pm' when
    the part of the day says which ('tomorrow afternoon', 'tonight'), else ''."""
    m = DAY_WORDS.search(t)
    if not m:
        return "", ""
    part = "pm" if m[1] == "tonight" or m[2] in ("afternoon", "evening", "night") else "am" if m[2] == "morning" else ""
    return ("today" if m[1] == "tonight" else m[1]), part


def duration(t):
    """'1 hour 30 minutes' / '45 minutes' / '2 hours' / '30 seconds' -> bridge 'when' like '1h30' / '45m' / '30s', else None."""
    h = re.search(r"(\d+) ?(?:hours?|hrs?)\b", t)
    m = re.search(r"(\d+) ?(?:minutes?|mins?)\b", t)
    s = re.search(r"(\d+) ?(?:seconds?|secs?)\b", t)
    if s:
        return f"{int(h[1] if h else 0) * 3600 + int(m[1] if m else 0) * 60 + int(s[1])}s"
    if not h and not m:
        return None
    return f"{h[1]}h{m[1] if m else ''}" if h else f"{m[1]}m"


def clock_time(t, part=""):
    """'2:30', '2:30 pm', '14:30', '3 pm', '3 o'clock', 'at 3', 'around 5' -> bridge 'when', else None.
    part ('am'/'pm', from 'tomorrow afternoon' or 'tonight') goes on a time said without one."""
    for m in re.finditer(r"(?:\b(at|around|about|by|after|till|until)\s+)?\b(\d{1,2})(?!\d)(?::(\d{2}))? ?"
                         r"(a\.?m\.?|p\.?m\.?|o'?clock)?(?!\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)\b)", t):
        if not (m[1] or m[3] or m[4]):
            continue
        suffix = (m[4] or "").replace(".", "")
        suffix = "" if "clock" in suffix else suffix
        if not suffix and int(m[2]) <= 12:
            suffix = part
        return f"{m[2]}:{m[3] or '00'}{suffix}"
    return None


def name_and_day(label):
    """A pickup name with a day left in it ('SCOTT BUTCH - TOMORROW', from Haiku) -> ('SCOTT BUTCH', 'tomorrow');
    a name with none comes back as it was, with ''."""
    said, _part = day_said(label.lower())
    if not said:
        return label, ""
    rest = re.sub(r"\s*-\s*$|^\s*-\s*", "", DAY_WORDS.sub(" ", label.lower()).strip())
    return who_and_what(rest) or label, said


# What's left of a pickup or drop-off sentence once the time and day are out, word by word: everything
# here goes, so "Joey Wheaton's gonna pick up in about" is JOEY WHEATON. Never the first word (Will, Bill).
FILLER = {"is", "are", "was", "will", "would", "should", "could", "be", "gonna", "going", "to", "come", "comes",
          "coming", "came", "in", "by", "over", "back", "pick", "picks", "picking", "picked", "up", "pickup",
          "pickups", "here", "there", "stop", "stops", "stopping", "swing", "swinging", "show", "showing", "due",
          "about", "around", "roughly", "approximately", "ish", "at", "sometime", "get", "grab", "collect",
          "drop", "drops", "dropping", "off", "dropoff", "bring", "brings", "bringing", "ok", "okay", "so", "and",
          "probably", "maybe", "supposed", "planning", "expected", "expecting", "then", "for", "or", "so", "just",
          "morning", "afternoon", "evening", "night", "said", "says", "today", "tonight", "tomorrow", "on",
          "this", "next", *DAYS}
LEAD = re.compile(r"^(?:(?:um|uh|ok|okay|so|hey|and|in|about|around|at|by)\s+)*"
                  r"(?:(?:set|add|put|make|book|schedule|new)\s+)?(?:(?:a|an|the|me)\s+)?"
                  r"(?:(?:pick\s*-?\s*ups?|drop\s*-?\s*offs?)\s+)?(?:(?:for|with|from)\s+)?")
# a sentence about the speaker or a question, not someone coming: "I'll be here till 6", "who's coming at 3"
NOT_WHO = re.compile(r"(?:i|i'm|i'll|im|ill|we|we're|we'll|who|who's|whos|what|when|you|it|it's|there|they|he|she|"
                     r"come|get|go|hard stop)\b")
ARTICLE = {"the", "his", "her", "their", "a", "an", "my", "some", "its"}


def who_and_what(rest):
    """'joey wheaton's gonna pick up about' -> 'JOEY WHEATON'; 'sam is picking up the juno' -> 'SAM - JUNO'."""
    rest = re.sub(r"[,.;:!?]", " ", rest)
    rest = re.sub(r"'(?:s|ll|re)\b(?=\s+(?:gonna|going|coming|come|picking|pick|be|here|stopping|stop|swinging|"
                  r"swing|showing|show|due|about|supposed|planning|dropping|drop|bringing|bring|probably)\b)", "", rest)
    rest = re.sub(r"\b(?:what|which|that)\b.*", "", rest)  # "Bob's picking up what Sam dropped off"
    words = LEAD.sub("", " ".join(rest.split())).split()
    if not words:
        return ""
    words = words[:1] + [w for w in words[1:] if w not in FILLER]
    split = next((i for i, w in enumerate(words) if i and w in ARTICLE), None)
    if split is None:
        return tidy(" ".join(words))
    who, what = " ".join(words[:split]), " ".join(w for w in words[split + 1:] if w not in ARTICLE)
    return tidy(f"{who} - {what}" if what else who)


def strip_time(t):
    t = re.sub(r"\b(?:and\s+)?(?:in|for)?\s*\d+ ?(?:seconds?|secs?)\b", " ", t)
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
    if m := re.fullmatch(r"(?:start (?:a )?)?focus(?: (?:for|session))?(?: (\d+)(?: minutes?)?)?"
                         r"(?:,? (?:with|and) (\d+)(?: minutes?)? breaks?)?", t):
        work = int(m[1] or 25)
        if not 1 <= work <= 240:
            return None  # leave odd lengths to the bridge, which says what's wrong
        short = work <= 30  # like the panel presets: 25/5 four rounds, 50/10 three
        rest, rounds = int(m[2] or (5 if short else 10)), 4 if short else 3
        stages = [("WORK", work), ("BREAK", rest)] * rounds
        return "/stages", {"name": "FOCUS", "stages": stages[:-1]}, None  # end on the last WORK, not a break
    # "clear notifications" is how Greg says it: instant here, not Haiku's ~2 s
    if re.fullmatch(r"(?:clear|clear (?:it|all|everything|(?:all |the |my )?notifications)|all clear|dismiss(?: all)?)", t):
        return "/unread/clear", {}, "Cleared."
    if m := re.match(r"(?:note|make a note|take a note|jot down|note to self)\b[\s,:;-]*(?:that\s+|to\s+)?(.+)", t):
        return "/note", {"text": m[1]}, None
    if m := re.match(r"(?:show|say|display|put up)\s+(.+)", t):
        return "/notify", {"text": m[1].upper()[:120], "color": "White", "sound": "NoneSound",
                           "priority": "critical"}, "On the clock."
    # a drop-off: with a time it's a new one; without, "Sam dropped off the Rhodes" / "the drop-off's here"
    # confirms one. Never inside a timer or reminder ("remind me in 20 minutes to drop off the invoice") or
    # a pickup ("Bob's picking up what Sam dropped off at 5").
    errand = re.search(r"\b(?:timer|remind|alarm|wake me|ping me|nudge me)\b|\bpick(?:ing)?\s*-?\s*up\b|\bpickup\b", t)
    day, part = day_said(t)
    when = duration(t) or clock_time(t, part)
    booked = {"day": day} if day and not duration(t) else {}
    odd_day = re.search(r"\bday after\b|\bweeks?\b|\bnext (?:" + "|".join(DAYS) + r")\b|\d+ (?:past|to|till|before) \d", t)  # "in 40 minutes" is from now, whatever day is said
    if not errand and (re.search(r"\bdropped\b(?:\s+\S+){0,4}?\s+off\b", t) or not when and re.search(
            r"\bdrop\s*-?\s*off(?: is|'s)? (?:here|in)\s*$", t)):
        what = re.sub(r"\b(?:dropped|drop\s*-?\s*off|off|is|'s|here|in|just|has|have|already)\b", " ", t)
        return "/dropoff/arrived", {"what": " ".join(what.replace("'s", " ").split())}, None
    if not errand and re.search(r"\bdrop(?:ping|s)?\s*-?\s*off\b", t):
        if not when:
            return None
        if odd_day:
            return None
        rest = DAY_WORDS.sub(" ", strip_time(t))
        return "/pickup", {"kind": "dropoff", "name": who_and_what(rest) or "someone", "when": when, **booked}, None
    # a pickup: said outright, or someone coming by ("Dean's coming at 3", "Bill will be here Friday around
    # noon"), the loose ones only outside a timer or reminder sentence
    timerish = re.search(r"\b(?:timer|remind|alarm|wake me|ping me|nudge me)\b", t)
    if re.search(r"\bpick(?:ing)?\s*-?\s*up\b|\bpickup\b", t) or (not timerish and not NOT_WHO.match(t) and re.search(
            r"\bcom(?:e|es|ing)\b(?!\s+home)|\bbe (?:here|by)\b|\b(?:stop|swing)(?:s|ping|ging)? by\b|"
            r"\bshow(?:s|ing)? up\b", t)):
        if not when or odd_day or NOT_WHO.match(t):
            return None
        rest = DAY_WORDS.sub(" ", strip_time(t))
        return "/pickup", {"kind": "pickup", "name": who_and_what(rest) or "someone", "when": when, **booked}, None
    if re.search(r"\b(?:timer|remind|alarm|wake me|ping me|nudge me)\b", t):
        when = duration(t) or clock_time(t)
        if not when or (day and not duration(t)):  # "remind me tomorrow at 5": timers are today's only
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
    if item["kind"] == "stages":
        first = item["stages"][0][0]
        count = len(item["stages"])
        return (f"{count} stage{'s' if count != 1 else ''} for {item.get('name') or 'you'}, {first} first, "
                f"done at {item['text'].rsplit(' ', 1)[-1]}.")
    label = {"pickup": "Pickup", "dropoff": "Drop-off", "timer": "Timer", "stop": "Hard stop"}.get(item["kind"], "Set")
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
