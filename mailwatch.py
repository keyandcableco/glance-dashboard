"""Watch a Thunderbird inbox (its local mbox file) and turn matching emails into clock alerts.

Configured per clock in config.toml [clocks.<name>.mail]: inbox path, sender, and subject rules.
Thunderbird appends new mail to the mbox file; we read only the bytes added since last look.
"""
import asyncio
import email
import email.header
import email.policy
import logging
import os
import re

POLL_SECONDS = 20

log = logging.getLogger("glanced.mail")


def decode(value):
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def alert_for(msg, cfg):
    """cfg: sender, rules [{match, label, color, anim, sound}]; first matching rule wins."""
    if cfg["sender"].lower() not in decode(msg["From"]).lower():
        return None
    subject = " ".join(decode(msg["Subject"]).split())
    for rule in cfg["rules"]:
        m = re.match(rule["match"], subject, re.I)
        if m:
            who = (m.group(1) or "").strip() if m.groups() else ""
            look = {k: rule[k] for k in ("color", "anim", "sound", "priority") if k in rule}
            return {"text": f"{{bell}} {rule['label']} {who}".strip()[:120], **look}
    return None


def split_mbox(chunk):
    # mbox messages start with "From " at the beginning of a line
    parts = re.split(rb"(?:^|\n)(?=From )", chunk)
    return [email.message_from_bytes(p, policy=email.policy.compat32) for p in parts if p.startswith(b"From ")]


async def watch(clock, cfg):
    inbox = cfg["inbox"]
    offset = os.path.getsize(inbox) if os.path.exists(inbox) else 0
    log.info("%s: watching %s from byte %d", clock.name, inbox, offset)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            size = os.path.getsize(inbox)
        except OSError:
            continue
        if size < offset:  # Thunderbird compacted the folder; start over from the new end
            offset = size
            continue
        if size == offset:
            continue
        with open(inbox, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
        # Thunderbird may still be writing the last message; leave a partial tail for next time
        if not chunk.endswith(b"\n"):
            continue
        offset = size
        for msg in split_mbox(chunk):
            alert = alert_for(msg, cfg)
            if not alert:
                continue
            try:
                await clock.alert("mail", keep=True, **alert)
                log.info("mail alert: %s", alert["text"])
            except Exception as e:
                log.warning("mail alert failed (%s): %s", e, alert["text"])
