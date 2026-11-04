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
the likely meaning. Times: give `minutes` for a duration from now ("in half an hour" = 30) or `at` \
as 24-hour HH:MM for a clock time ("after lunch" = 13:00, "end of day" = 17:00); set the other to null. \
Labels and names are short (a few words) and in capitals. `clock` is null unless the speaker names \
one of the clocks. If the request doesn't fit any tool, don't call one; reply with one short sentence \
saying what you can do instead."""

WHEN = {
    "minutes": {"type": ["integer", "null"], "description": "Minutes from now, or null"},
    "at": {"type": ["string", "null"], "description": "24-hour HH:MM clock time, or null"},
}


def tools(clock_names):
    clock = {"anyOf": [{"type": "string", "enum": list(clock_names)}, {"type": "null"}],
             "description": "Which clock, only if the speaker named one"}

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
        tool("cancel", "Cancel or stop a timer, reminder, pickup or hard stop that's set or going off.",
             {"what": {"type": "string", "description": "Words from its label or name; empty for the most recent"}}),
        tool("add_note", "Save a note to self for later, with no time attached (an idea, something to order, "
             "something to remember). Use set_timer instead when a time is given.",
             {"text": {"type": "string", "description": "The note, in the speaker's words, tidied"}}),
        tool("clock_in", "Clock in: start the shop's work clock.", {}),
        tool("clock_out", "Clock out: stop the shop's work clock.", {}),
        tool("clear_unread", "Clear everything waiting on the clock (only when asked to clear all of it).", {}),
    ]


def client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return anthropic.AsyncAnthropic(api_key=key, timeout=15.0, max_retries=1) if key else None


async def understand(api, text, clock_names):
    """Spoken text -> ("tool", name, input) or ("reply", sentence), or None if no API key or on error."""
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
            messages=[{"role": "user", "content": f"It is {now}. Clocks: {', '.join(clock_names)}.\n\nSaid: {text}"}],
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
