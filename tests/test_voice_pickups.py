"""Spoken pickups: names without filler, days that set the day (not the name).

    .venv/bin/python -m unittest discover -s tests
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clockvoice  # noqa: E402
import pickups  # noqa: E402
import pickupsync  # noqa: E402

WED = datetime.datetime(2026, 11, 18, 10, 26)  # Wednesday morning, when Scott's went wrong


def said(text):
    return clockvoice.parse(text, ["shop", "family"])


class PatternPickups(unittest.TestCase):
    def check(self, text, name, when, day=None, kind="pickup"):
        path, body, _reply = said(text)
        self.assertEqual(path, "/pickup")
        self.assertEqual((body["kind"], body["name"], body["when"], body.get("day")), (kind, name, when, day))

    def test_scott_tomorrow(self):  # 2026-11-18: went to Haiku, landed today named "SCOTT BUTCH - TOMORROW"
        self.check("clock, Set a pickup for Scott Butch tomorrow at five PM", "SCOTT BUTCH", "5:00pm", "tomorrow")

    def test_scott_tomorrow_around(self):
        self.check("clock, Scott is gonna come tomorrow around five", "SCOTT", "5:00", "tomorrow")

    def test_joey_in_about_an_hour(self):  # 2026-11-18: named "JOEY WHEATON'S GONNA IN ABOUT"
        self.check("clock, Joey Wheaton's gonna pick up in about an hour", "JOEY WHEATON", "1h")

    def test_dean_coming_at_3(self):
        self.check("clock, Dean's coming at 3", "DEAN", "3:00")

    def test_bill_friday_noon(self):
        self.check("clock, Bill will be here Friday around noon", "BILL", "12:00pm", "friday")

    def test_pickup_for_sam(self):
        self.check("clock, pickup for Sam in 40 minutes", "SAM", "40m")

    def test_instrument_after_the(self):
        self.check("clock, Sam is picking up the Juno at 2:30", "SAM - JUNO", "2:30")

    def test_first_word_kept(self):  # "Will" is a name here, not filler
        self.check("clock, Will is coming by tomorrow afternoon at 3 to get his Rhodes", "WILL - RHODES", "3:00pm",
                   "tomorrow")

    def test_tonight(self):
        self.check("clock, Bob's coming back tonight at 8", "BOB", "8:00pm", "today")

    def test_half_past(self):
        self.check("clock, pick up for Ann at half past four", "ANN", "4:30")

    def test_dropoff(self):
        self.check("clock, Sam's dropping off a Rhodes at 3:30pm", "SAM - RHODES", "3:30pm", kind="dropoff")

    def test_minutes_ignore_a_day(self):
        self.check("clock, Joey's coming in about 10 minutes", "JOEY", "10m")

    def test_clock_name(self):
        path, body, _ = said("clock, family, Sam is picking up at 2:30")
        self.assertEqual((body["name"], body["clock"]), ("SAM", "family"))

    def test_reminder_stays_a_timer(self):
        _path, body, _ = said("clock, remind me in 20 minutes that Dean's coming")
        self.assertEqual((body["kind"], body["when"]), ("timer", "20m"))

    def test_dropoff_confirm_unchanged(self):
        self.assertEqual(said("clock, Sam dropped off the Rhodes")[0], "/dropoff/arrived")

    def test_not_a_pickup(self):  # from the review: these go to Haiku, not a pickup named "I"
        for text in ("clock, I'll be in a meeting until 3", "clock, I'll be here till 6", "clock, who's coming at 3",
                     "clock, hard stop at 5 so I can come home", "clock, Dean's coming at ten past three",
                     "clock, Dean's coming the day after tomorrow at 3", "clock, Dean's coming next Tuesday at 3",
                     "clock, remind me tomorrow at five"):
            parsed = said(text)
            self.assertFalse(parsed and parsed[1].get("kind") == "pickup", f"{text!r} -> {parsed}")
            self.assertFalse(parsed and parsed[1].get("day"), f"{text!r} -> {parsed}")

    def test_what_someone_dropped_off(self):
        self.check("clock, Bob's picking up what Sam dropped off at 5", "BOB", "5:00")

    def test_dropoff_confirm_with_a_time(self):
        self.assertEqual(said("clock, Sam dropped off the Rhodes at 3")[0], "/dropoff/arrived")

    def test_show_keeps_its_words(self):
        self.assertEqual(said("clock, show back by noon")[1]["text"], "BACK BY 12:00PM")

    def test_timer_unchanged(self):
        self.assertEqual(said("clock, timer 30 minutes")[1], {"kind": "timer", "name": "", "when": "30m"})


class HaikuNames(unittest.TestCase):
    def test_day_out_of_the_name(self):
        self.assertEqual(clockvoice.name_and_day("SCOTT BUTCH - TOMORROW"), ("SCOTT BUTCH", "tomorrow"))
        self.assertEqual(clockvoice.name_and_day("BILL FRIDAY"), ("BILL", "friday"))

    def test_plain_name_untouched(self):
        self.assertEqual(clockvoice.name_and_day("SAM - JUNO"), ("SAM - JUNO", ""))


class Days(unittest.TestCase):
    def due(self, when, day):
        return datetime.datetime.fromtimestamp(pickups.parse_when(when, WED, pickups.day_of(day, WED)))

    def test_tomorrow_five(self):
        self.assertEqual(self.due("5:00", "tomorrow"), datetime.datetime(2026, 11, 19, 17, 0))
        self.assertEqual(self.due("5:00pm", "tomorrow"), datetime.datetime(2026, 11, 19, 17, 0))
        self.assertEqual(self.due("17:00", "2026-11-19"), datetime.datetime(2026, 11, 19, 17, 0))

    def test_friday_noon(self):
        self.assertEqual(self.due("12:00pm", "friday"), datetime.datetime(2026, 11, 20, 12, 0))

    def test_weekday_today_is_today(self):
        self.assertEqual(self.due("3:00", "wednesday"), datetime.datetime(2026, 11, 18, 15, 0))

    def test_no_day_unchanged(self):
        self.assertEqual(self.due("5:00", ""), datetime.datetime(2026, 11, 18, 17, 0))

    def test_past_day_refused(self):
        with self.assertRaises(ValueError):
            pickups.day_of("2026-11-17", WED)

    def test_later_day(self):
        self.assertTrue(pickups.later_day(datetime.datetime(2026, 11, 19, 17).timestamp(), WED))
        self.assertFalse(pickups.later_day(datetime.datetime(2026, 11, 18, 17).timestamp(), WED))


class Sync(unittest.TestCase):
    def test_held_until_its_day(self):
        due = datetime.datetime(2026, 11, 19, 17).timestamp()
        self.assertFalse(pickupsync.on_its_day(due, WED.timestamp()))
        self.assertFalse(pickupsync.on_its_day(due, datetime.datetime(2026, 11, 19, 0, 5).timestamp()))
        self.assertTrue(pickupsync.on_its_day(due, datetime.datetime(2026, 11, 19, 7, 0).timestamp()))

    def test_early_pickup_an_hour_before(self):
        due = datetime.datetime(2026, 11, 19, 6, 30).timestamp()
        self.assertTrue(pickupsync.on_its_day(due, datetime.datetime(2026, 11, 19, 5, 30).timestamp()))

    def test_instrument_added(self):
        self.assertEqual(pickupsync.with_instrument("JOEY WHEATON", {"instrument": "Korg SV-1"}, "pickup"),
                         "JOEY WHEATON - KORG SV-1")
        self.assertEqual(pickupsync.with_instrument("SAM - JUNO", {"instrument": "Roland Juno-106"}, "pickup"),
                         "SAM - JUNO")
        self.assertEqual(pickupsync.with_instrument("SAM", {"instrument": "Rhodes"}, "dropoff"), "SAM")
        self.assertEqual(pickupsync.with_instrument("SAM", {"instrument": ""}, "pickup"), "SAM")


if __name__ == "__main__":
    unittest.main()
