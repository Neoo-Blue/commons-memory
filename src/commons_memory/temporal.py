"""Resolve relative time words against a memory's own date, at read time: "yesterday" in a note written on 8 May 2023
becomes "yesterday (7 May 2023)". mem0 gets this by rewriting every write with an LLM; this is a few regexes, costs
nothing per write, and never alters what was stored."""
from __future__ import annotations

import re
import time

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
       "nine": 9, "ten": 10, "couple of": 2, "few": 3}
_N = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a couple of|a few)"
PAT = re.compile(
    r"\b(yesterday|today|tonight|this morning|this afternoon|this evening|tomorrow|"
    r"last (?:week|weekend|month|year|night|" + "|".join(DAYS) + r")|"
    r"next (?:week|weekend|month|year|" + "|".join(DAYS) + r")|"
    r"this (?:week|weekend|month|year)|"
    + _N + r" (day|week|month|year)s? ago)\b", re.I)


def _d(ts: float) -> str:
    return time.strftime("%-d %B %Y", time.gmtime(ts))


def _month(ts: float, delta: int) -> str:
    t = time.gmtime(ts)
    y, m = t.tm_year, t.tm_mon + delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return time.strftime("%B %Y", time.struct_time((y, m, 1, 0, 0, 0, 0, 1, 0)))


def resolve(phrase: str, ts: float) -> str | None:
    p, day = phrase.lower(), 86400
    wd = time.gmtime(ts).tm_wday
    if p == "yesterday" or p == "last night":
        return _d(ts - day)
    if p in ("today", "tonight", "this morning", "this afternoon", "this evening"):
        return _d(ts)
    if p == "tomorrow":
        return _d(ts + day)
    if p == "last week":
        return f"the week before {_d(ts)}"
    if p == "next week":
        return f"the week after {_d(ts)}"
    if p == "this week":
        return f"the week of {_d(ts)}"
    if p in ("last weekend", "this weekend", "next weekend"):
        back = (wd - 5) % 7 or 7 if p == "last weekend" else None
        if p == "last weekend":
            sat = ts - back * day
        else:
            sat = ts + ((5 - wd) % 7) * day + (7 * day if p == "next weekend" else 0)
        return f"the weekend of {_d(sat)}"
    if p in ("last month", "next month", "this month"):
        return _month(ts, {"last month": -1, "next month": 1, "this month": 0}[p])
    if p in ("last year", "next year", "this year"):
        return str(time.gmtime(ts).tm_year + {"last year": -1, "next year": 1, "this year": 0}[p])
    m = re.match(r"(last|next) (\w+)$", p)
    if m and m.group(2) in DAYS:
        target = DAYS.index(m.group(2))
        delta = -((wd - target) % 7 or 7) if m.group(1) == "last" else ((target - wd) % 7 or 7)
        return _d(ts + delta * day)
    m = re.match(_N + r" (day|week|month|year)s? ago$", p)
    if m:
        n = NUM.get(m.group(1).replace("a couple of", "couple of").replace("a few", "few"), None)
        n = int(m.group(1)) if m.group(1).isdigit() else n
        if not n:
            return None
        unit = m.group(2)
        if unit == "day":
            return _d(ts - n * day)
        if unit == "week":
            return f"about {_d(ts - n * 7 * day)}"
        if unit == "month":
            return _month(ts, -n)
        return str(time.gmtime(ts).tm_year - n)
    return None


def annotate(text: str, ts: float | None) -> str:
    """Append the absolute date after each relative time phrase: 'went yesterday' -> 'went yesterday (7 May 2023)'.
    COMMONS_RESOLVE_DATES=0 turns it off."""
    import os
    if not text or not ts or os.environ.get("COMMONS_RESOLVE_DATES") == "0":
        return text or ""

    def sub(m):
        r = resolve(m.group(0), ts)
        return f"{m.group(0)} ({r})" if r else m.group(0)
    return PAT.sub(sub, text)


if __name__ == "__main__":  # self-check: 8 May 2023 was a Monday
    t = time.mktime(time.strptime("2023-05-08 13:56", "%Y-%m-%d %H:%M")) - time.timezone
    for phrase, want in [("yesterday", "7 May 2023"), ("last week", "the week before 8 May 2023"),
                         ("last friday", "5 May 2023"), ("last weekend", "the weekend of 6 May 2023"),
                         ("last year", "2022"), ("two days ago", "6 May 2023"), ("last month", "April 2023"),
                         ("next month", "June 2023"), ("a few days ago", "5 May 2023")]:
        got = resolve(phrase, t)
        assert got == want, (phrase, got, want)
    assert annotate("I went to a support group yesterday!", t) == "I went to a support group yesterday (7 May 2023)!"
    print("temporal ok")
