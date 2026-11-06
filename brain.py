"""Loose spoken commands -> clock actions, with Claude Haiku and tool use.

Only called when clockvoice's patterns don't recognise the phrasing. The API key is read from
anthropic_api_key next to this file (or ANTHROPIC_API_KEY); without one, understand() returns None.
"""
import datetime
import json
import logging
import os
from pathlib import Path

import anthropic

MODEL = "claude-haiku-5-5"
KEY_FILE = Path(__file__).parent / "anthropic_api_key"

SYSTEM = """You turn a short spoken command into exactly one call to one of your tools. The words were \
transcribed from speech across a workshop, so expect missing punctuation and misheard words; go by \
the likely meaning. Times: give `minutes` for a duration from now ("in half an hour" = 30, "45 seconds" = 0.75) or `at` \
as 24-hour HH:MM for a clock time ("after lunch" = 13:00, "end of day" = 17:00); set the other to 0 or \
empty. Timer, pickup and stop labels are short (a few words) and in capitals; text for the shop app \
(switch_job, wait_on_customer, customer_replied) stays as said, so names still match, and clock_out's \
`done` is what the speaker says they did, in their own words. `clock` is empty unless the speaker \
names one of the clocks (for change and cancel: the clock the item is on; move_to is where it goes). \
For change and cancel, `what` is words copied from that item's text under "Set now", times as \
written there (12-hour, e.g. 2:30). "More time", "snooze" and "five more minutes" are add_minutes, \
never minutes. If the request doesn't fit any tool, don't call one; reply with one short sentence \
saying what you can do instead."""

# Plain types with empty defaults: strict tools allow only 16 nullable fields in all
WHEN = {
    "minutes": {"type": "number", "description": "Minutes from now (a fraction for seconds: 0.5 = 30 seconds), or 0"},
    "at": {"type": "string", "description": "24-hour HH:MM clock time, or empty"},
}


def tools(clock_names):
    clock = {"type": "string", "enum": [*clock_names, ""],
             "description": "Which clock, only if the speaker named one; else empty"}

    def tool(name, description, props):
        props = {**props, "clock": clock}
        return {"name": name, "description": description, "strict": True,
                "input_schema": {"type": "object", "properties": props,
                                 "required": list(props), "additionalProperties": False}}
    return [
        tool("set_timer", "A timer or reminder: count down and alert when it's due.",
             {**WHEN, "label": {"type": "string", "description": "What it's for, or empty"}}),
        tool("add_pickup", "Someone is coming to pick something up (or drop by) at a time.",
             {**WHEN, "name": {"type": "string", "description": "Who, and what if said"}}),
        tool("show_message", "Put a short message up on the clock.",
             {"text": {"type": "string"}}),
        tool("set_hard_stop", "The speaker must stop working at a time (leave, close up, quit for the "
             "night). Nags at 30, 15 and 5 minutes before, then repeats for half an hour unless cleared.",
             {**WHEN, "label": {"type": "string", "description": "Why, or empty"}}),
        tool("start_stages", "A run of labelled stages back to back on one timer, e.g. reforming capacitors "
             "('50V 15 min, then 75V, 100V, 120V'), glue-ups ('clamp 45 then unclamp'), burn-in, lacquer coats, "
             "or focus rhythms ('focus 25' = WORK 25 / BREAK 5, 4 rounds). Labels are what the clock "
             "shows during each stage: short, capitals. Put ONE round in stages and the number of rounds in "
             "repeat; never write the rounds out. Whole minutes; at most 20 stages and 24 hours in all.",
             {"name": {"type": "string", "description": "What it's for, e.g. CAPS or FOCUS"},
              "stages": {"type": "array", "minItems": 1, "description": "One round of stages, in order",
                         "items": {"type": "object", "additionalProperties": False, "required": ["label", "minutes"],
                                   "properties": {"label": {"type": "string"}, "minutes": {"type": "integer"}}}},
              "repeat": {"type": "integer", "description": "How many times to run the list; 1 for once"}}),
        tool("change", "Change a timer, reminder, pickup or hard stop that's already set or going off: rename it, "
             "move it to a new time, give it more time ('five more minutes', 'snooze it 10'), or move it "
             "to another clock. Never use set_timer to change an existing one.",
             {"what": {"type": "string", "description": "Words from its text as listed under Set now (e.g. '8:53' or 'GLUE'); empty for the one going off or the newest"},
              "new_label": {"type": "string", "description": "New label/name, or empty to keep it"},
              "minutes": {"type": "integer", "description": "New time as minutes from now, or 0"},
              "at": {"type": "string", "description": "New time as 24-hour HH:MM, or empty"},
              "add_minutes": {"type": "integer", "description": "Minutes to add (snooze/extend), or 0"},
              "move_to": {"type": "string", "enum": [*clock_names, ""], "description": "Clock to move it to, or empty"}}),
        tool("cancel", "Cancel or stop a timer, reminder, pickup or hard stop that's set or going off.",
             {"what": {"type": "string", "description": "Words from its text as listed under Set now; empty for the one going off or the newest"}}),
        tool("add_note", "Save a note to self for later, with no time attached (an idea, something to order, "
             "something to remember). Use set_timer instead when a time is given.",
             {"text": {"type": "string", "description": "The note, in the speaker's words, tidied"}}),
        tool("switch_job", "Say which instrument (or 'shop time') the speaker is working on now, e.g. "
             "'working on the Juno', 'switching to Alex's Rhodes'. Clocks in if needed.",
             {"text": {"type": "string", "description": "The instrument as said, e.g. Alex's Rhodes"}}),
        tool("wait_on_customer", "The speaker is waiting on a customer for something (approval, a decision, "
             "payment, a part they're sending). The shop app reminds them to follow up after some days.",
             {"text": {"type": "string", "description": "Who/which instrument and what, e.g. Sam's approval for the Juno"},
              "days": {"type": "integer", "description": "Days until the reminder, if said; else 0"}}),
        tool("customer_replied", "A customer got back about something the speaker was waiting on.",
             {"text": {"type": "string", "description": "Who/which instrument, e.g. Sam about the Juno"}}),
        tool("clock_in", "Clock in: start the shop's work clock.", {}),
        tool("clock_out", "Clock out: stop the shop's work clock, with what the speaker did if they say it "
             "('clocking out, recapped the power supply on the Juno').",
             {"done": {"type": "string", "description": "What they say they did this session, as said; else empty"}}),
        tool("clear_unread", "Clear everything waiting on the clock (only when asked to clear all of it).", {}),
    ]


def client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return anthropic.AsyncAnthropic(api_key=key, timeout=15.0, max_retries=1) if key else None


async def understand(api, text, clock_names, set_now=""):
    """Spoken text -> ("tool", name, input) or ("reply", sentence), or None if no API key or on error.
    set_now lists the timers, pickups and stops already set, so "the hour 37 timer" or "the bread
    one" can be found; for change and cancel, `what` should be words from that item's text."""
    if api is None:
        return None
    now = datetime.datetime.now().strftime("%A %H:%M")
    try:
        response = await api.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=SYSTEM,
            tools=tools(clock_names),
            messages=[{"role": "user", "content": f"It is {now}. Clocks: {', '.join(clock_names)}.\n"
                       f"Set now:\n{set_now or '(nothing)'}\n\nSaid: {text}"}],
        )
    except anthropic.APIError as e:
        logging.getLogger("glanced.brain").warning("Haiku call failed: %s", e)
        return None
    if response.stop_reason == "refusal":
        return ("reply", "I can't do that one.")
    for block in response.content:
        if block.type == "tool_use":
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return ("tool", block.name, args)
    words = " ".join(b.text for b in response.content if b.type == "text").strip()
    return ("reply", words or "Sorry, I didn't get that.")
