"""
Meeting calendar and watch windows.

One correction before the code. The Mobilize feeds do not carry the
government meeting schedule. You established that while building the
events widget: government meetings stay hardcoded in the widget config
because no API publishes them. So the canonical schedule this pipeline
should follow is the one you already maintain in tcdp-shared.js on
Netlify, not Mobilize.

That is the better source anyway. It keeps your single source of truth
single: you edit one file when the county cancels a meeting, and both
the website widget and this pipeline pick it up. Mobilize is still read
as a supplement, because occasionally a meeting does get staged there as
an event, but it is never the authority.

Resolution order, first hit wins per meeting:

    1. tcdp-shared.js          your hardcoded schedule, authoritative
    2. official body sites     county events page, school board upcoming
    3. Mobilize                supplement only, for staged meetings
    4. recurrence rules        2nd and 4th Mondays, and so on

Why windows instead of a fixed cron

Nothing here can push to us. GitHub Actions cannot be triggered by a
county clerk uploading a PDF. What we can do is stop polling when
nothing could possibly have appeared, and poll tightly when something is
expected. Each meeting opens three windows:

    agenda    T-7 to T          packets post a few days ahead
    video     T to T+4          same evening or the next morning
    minutes   T+14 to T+75      approved at a later meeting, so the lag
                                is structural and long

Outside every window the run exits in seconds and costs nothing. Inside
a window it polls every couple of hours. Once an artifact is acquired,
that window closes and stays closed.
"""

from __future__ import annotations

import re
import json
import datetime as dt
from pathlib import Path
from dataclasses import dataclass, field, asdict

import requests
import yaml

from govwatch.sources import UA

ROOT = Path(__file__).parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yml").read_text())
SCHED_CFG = CONFIG.get("schedule", {})
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)

CALENDAR = STATE / "calendar.json"
ACQUIRED = STATE / "acquired.json"
CHECKED = STATE / "last_checked.json"

BODIES = ("county", "city", "schools")


@dataclass
class Meeting:
    body: str
    date: str            # ISO
    title: str = ""
    cancelled: bool = False
    source: str = ""     # which resolver supplied it
    key: str = field(default="")

    def __post_init__(self):
        if not self.key:
            self.key = f"{self.body}:{self.date}"

    def to_dict(self):
        return asdict(self)


# ====================================================== schedule sources

def from_shared_js() -> list[Meeting]:
    """
    Read the hardcoded government meeting schedule out of tcdp-shared.js.

    The parser is deliberately tolerant, because I have not seen the file
    and do not want to guess at its internals and then fail silently. It
    looks for a named array and JSON-parses it. If your current structure
    does not match, the cleanest fix is to publish a small sibling file
    next to tcdp-shared.js in exactly this shape, and have the widget
    read the same file:

        // govMeetings, single source of truth for widget and govwatch
        const govMeetings = [
          {"body": "county",  "date": "2026-08-24", "title": "Board of Commissioners, 6pm"},
          {"body": "county",  "date": "2026-07-27", "cancelled": true},
          {"body": "schools", "date": "2026-08-17", "title": "Board of Education"},
          {"body": "city",    "date": "2026-08-03", "title": "City Council"}
        ];

    Adding one line when a meeting is cancelled then updates the website
    and stops this pipeline polling for materials that will never exist.
    """
    url = SCHED_CFG.get("shared_js_url")
    if not url:
        return []
    try:
        js = requests.get(url, headers=UA, timeout=30).text
    except Exception as e:
        print(f"shared.js unreachable: {e}")
        return []

    for name in SCHED_CFG.get("array_names", ["govMeetings", "governmentMeetings", "GOV_MEETINGS"]):
        m = re.search(rf"{name}\s*=\s*(\[.*?\]);", js, re.S)
        if not m:
            continue
        raw = m.group(1)
        raw = re.sub(r",\s*([\]}])", r"\1", raw)            # trailing commas
        raw = re.sub(r"(?m)^\s*//.*$", "", raw)             # line comments
        raw = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', raw)  # bare keys
        raw = raw.replace("'", '"')
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"found {name} but could not parse it: {e}")
            continue
        return [
            Meeting(
                body=_norm_body(it.get("body") or it.get("entity") or ""),
                date=_norm_date(it.get("date") or it.get("start") or ""),
                title=it.get("title") or it.get("name") or "",
                cancelled=bool(it.get("cancelled") or it.get("canceled")),
                source="tcdp-shared.js",
            )
            for it in items
            if _norm_body(it.get("body") or it.get("entity") or "") in BODIES
            and _norm_date(it.get("date") or it.get("start") or "")
        ]
    print("no government meeting array found in shared.js")
    return []


def from_official_sites() -> list[Meeting]:
    """
    Scrape the bodies' own published schedules. Slower and more fragile
    than shared.js, but it is the only source that reflects a change the
    county made this morning.
    """
    out = []

    # County publishes its full year on the recurring event page.
    try:
        html = requests.get(
            "https://www.transylvaniacounty.org/events/board-commissioners-meeting",
            headers=UA, timeout=30,
        ).text
        for m in re.finditer(
            r"(?:Monday|Tuesday),\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*@?\s*(\d{1,2}:\d{2}\s*[APM]{2})?",
            html,
        ):
            when = _parse_long(m.group(1))
            if when:
                out.append(Meeting("county", when.isoformat(),
                                   f"Board of Commissioners {m.group(2) or ''}".strip(),
                                   source="county site"))
    except Exception as e:
        print(f"county schedule unavailable: {e}")

    # School board lists upcoming meetings on every page.
    try:
        html = requests.get("https://transylvania.schoolboard.net/", headers=UA, timeout=30).text
        for m in re.finditer(r"(?:Mon|Tue|Wed|Thu|Fri),\s+(\d{2}/\d{2}/\d{4})", html):
            try:
                when = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date()
            except ValueError:
                continue
            out.append(Meeting("schools", when.isoformat(),
                               "Board of Education", source="schoolboard.net"))
    except Exception as e:
        print(f"school board schedule unavailable: {e}")

    return out


def from_mobilize() -> list[Meeting]:
    """
    Supplement only. Government meetings are not staged in Mobilize as a
    rule, but when one is, catching it costs one request.

    Uses the org IDs already wired into the events widget. The
    timeslot_start=gte_now quirk applies here exactly as it does there.
    """
    out = []
    for org in SCHED_CFG.get("mobilize_org_ids", []):
        try:
            r = requests.get(
                f"https://api.mobilize.us/v1/organizations/{org}/events",
                params={"timeslot_start": "gte_now", "per_page": 50},
                headers=UA, timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception:
            continue
        for ev in data:
            title = ev.get("title", "")
            body = _body_from_title(title)
            if not body:
                continue
            for ts in ev.get("timeslots", []) or []:
                when = _epoch_date(ts.get("start_date"))
                if when:
                    out.append(Meeting(body, when.isoformat(), title,
                                       source=f"mobilize:{org}"))
    return out


def from_rules(horizon_days: int = 120) -> list[Meeting]:
    """
    Last resort so the pipeline degrades rather than going blind.

      county   2nd Monday 4pm, 4th Monday 6pm
      city     1st and 3rd Mondays 5:30pm
      schools  3rd Monday 6pm

    These are the standing schedules as published by each body. They are
    right until somebody cancels, which is exactly why shared.js outranks
    them.
    """
    rules = {"county": (2, 4), "city": (1, 3), "schools": (3,)}
    today = dt.date.today()
    out, d = [], today - dt.timedelta(days=90)
    end = today + dt.timedelta(days=horizon_days)
    while d <= end:
        if d.weekday() == 0:
            nth = (d.day - 1) // 7 + 1
            for body, weeks in rules.items():
                if nth in weeks:
                    out.append(Meeting(body, d.isoformat(), "", source="recurrence rule"))
        d += dt.timedelta(days=1)
    return out


# ============================================================== assembly

def build() -> list[Meeting]:
    """Merge all resolvers. Earlier sources win. Cancellations always win."""
    merged: dict[str, Meeting] = {}
    for resolver in (from_rules, from_mobilize, from_official_sites, from_shared_js):
        for m in resolver():
            if not m.date:
                continue
            existing = merged.get(m.key)
            if existing and existing.cancelled and not m.cancelled:
                continue
            if existing and not m.title and existing.title:
                m.title = existing.title
            merged[m.key] = m

    # A meeting the authoritative source dropped entirely is treated as
    # cancelled, so we stop watching for materials that will not appear.
    shared = {m.key for m in from_shared_js()}
    if shared:
        for key, m in merged.items():
            if (m.source == "recurrence rule"
                    and key not in shared
                    and _d(m.date) > dt.date.today()):
                m.cancelled = True
                m.title = (m.title + " (not in shared.js, treated as cancelled)").strip()

    cal = sorted(merged.values(), key=lambda m: (m.date, m.body))
    CALENDAR.write_text(json.dumps([m.to_dict() for m in cal], indent=2))
    return cal


def load_calendar(max_age_hours: int = 24) -> list[Meeting]:
    """Rebuild at most once a day. The schedule does not change hourly."""
    if CALENDAR.exists():
        age = dt.datetime.now() - dt.datetime.fromtimestamp(CALENDAR.stat().st_mtime)
        if age < dt.timedelta(hours=max_age_hours):
            return [Meeting(**m) for m in json.loads(CALENDAR.read_text())]
    return build()


# =============================================================== windows

WINDOWS = {
    # artifact: (days before meeting, days after meeting, hours between checks)
    #
    # A window says an artifact could appear. Cadence says how urgently
    # to look. These are different questions and conflating them is what
    # makes a two-hourly schedule expensive.
    #
    # Video is the one worth chasing: it appears the same evening, it is
    # the only same-week record of the county, and nothing else tells you
    # what was said. So it gets checked every tick while its window is
    # open, which is what "as soon as it is available" actually means
    # when no upstream source can push to you.
    #
    # Minutes sit at the other extreme. The window has to be wide because
    # county minutes are approved at a later meeting, but nothing is
    # gained by asking more than once a day for something that is weeks
    # out. Same for press, which publishes on its own rhythm.
    # Anchored to the meeting, with a bounded retry ladder rather than a
    # blanket poll. Two runs a day is enough to hit every window here.
    #
    # Video starts at T+1, not T+0. All three bodies convene in the
    # evening, 5:30 or 6, and run two to three hours. A check on the day
    # itself is either mid-meeting or after the last scheduled run, and
    # a three hour video needs processing time after upload besides. The
    # realistic first sighting is the next morning.
    #
    # The window runs to T+4 so a late upload still gets caught. That
    # covers the county, which posts slowest, without a single fixed
    # shot that misses everything if the upload slips by a day.
    "agenda":  (-7, 0, 24),
    "video":   (1, 4, 5),
    "minutes": (14, 75, 168),
    "press":   (1, 10, 72),
}


def open_windows(today: dt.date | None = None,
                 respect_cadence: bool = True,
                 now: dt.datetime | None = None) -> dict[str, list[Meeting]]:
    """
    Which artifacts are worth polling for right now, and for which
    meetings. Excludes anything already acquired, anything for a
    cancelled meeting, and anything checked more recently than its
    cadence allows.
    """
    today = today or dt.date.today()
    now = now or dt.datetime.now()
    acquired = json.loads(ACQUIRED.read_text()) if ACQUIRED.exists() else {}
    checked = json.loads(CHECKED.read_text()) if CHECKED.exists() else {}
    cal = load_calendar()
    out: dict[str, list[Meeting]] = {k: [] for k in WINDOWS}

    for m in cal:
        if m.cancelled:
            continue
        when = _d(m.date)
        for artifact, (lo, hi, cadence) in WINDOWS.items():
            if not (when + dt.timedelta(days=lo) <= today <= when + dt.timedelta(days=hi)):
                continue
            if artifact in acquired.get(m.key, []):
                continue
            if respect_cadence and _too_soon(checked, artifact, m.key, cadence, now):
                continue
            out[artifact].append(m)
    return {k: v for k, v in out.items() if v}


def _too_soon(checked: dict, artifact: str, key: str,
              cadence_hours: int, now: dt.datetime) -> bool:
    last = checked.get(f"{artifact}:{key}")
    if not last:
        return False
    try:
        return (now - dt.datetime.fromisoformat(last)) < dt.timedelta(hours=cadence_hours)
    except ValueError:
        return False


def mark_checked(artifact: str, meetings: list["Meeting"],
                 now: dt.datetime | None = None):
    """
    Record the attempt, whether or not it found anything.

    now is injectable so the cadence logic can be tested and so a
    backfill run cannot stamp the future and lock every window shut.
    """
    checked = json.loads(CHECKED.read_text()) if CHECKED.exists() else {}
    stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
    for m in meetings:
        checked[f"{artifact}:{m.key}"] = stamp
    CHECKED.write_text(json.dumps(checked, indent=2, sort_keys=True))


def mark_acquired(meeting_key: str, artifact: str):
    """Close a window. Called when a document or transcript lands."""
    acquired = json.loads(ACQUIRED.read_text()) if ACQUIRED.exists() else {}
    got = set(acquired.get(meeting_key, []))
    got.add(artifact)
    acquired[meeting_key] = sorted(got)
    ACQUIRED.write_text(json.dumps(acquired, indent=2))


def match_meeting(body: str, date_str: str, tolerance_days: int = 2) -> str | None:
    """Map a retrieved artifact back to a scheduled meeting."""
    if not date_str:
        return None
    try:
        when = _d(date_str)
    except ValueError:
        return None
    best, best_gap = None, tolerance_days + 1
    for m in load_calendar():
        if m.body != body:
            continue
        gap = abs((_d(m.date) - when).days)
        if gap < best_gap:
            best, best_gap = m.key, gap
    return best


def describe(today: dt.date | None = None) -> str:
    """Human readable plan, for the `schedule` command."""
    today = today or dt.date.today()
    cal = load_calendar()
    upcoming = [m for m in cal if _d(m.date) >= today][:8]
    windows = open_windows(today)

    lines = [f"Calendar built from {len({m.source for m in cal})} sources, "
             f"{len(cal)} meetings known.", "", "Next meetings:"]
    for m in upcoming:
        flag = "  CANCELLED" if m.cancelled else ""
        lines.append(f"  {m.date}  {m.body:8s} {m.title or ''}{flag}  [{m.source}]")

    lines += ["", "Open windows right now, after cadence filtering:"]
    if not windows:
        lines.append("  none, this run will exit without polling")
    for artifact, meetings in windows.items():
        cadence = WINDOWS[artifact][2]
        for m in meetings:
            lines.append(f"  {artifact:8s} for {m.body} meeting {m.date} "
                         f"(checked at most every {cadence}h)")

    due = open_windows(today, respect_cadence=False)
    suppressed = sum(len(v) for v in due.values()) - sum(len(v) for v in windows.values())
    if suppressed > 0:
        lines.append(f"  {suppressed} window(s) in range but checked recently enough to skip")
    return "\n".join(lines)


# =============================================================== helpers

def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _norm_body(s: str) -> str:
    s = (s or "").lower()
    if "count" in s or "commission" in s:
        return "county"
    if "city" in s or "council" in s or "brevard" in s:
        return "city"
    if "school" in s or "education" in s or "boe" in s:
        return "schools"
    return s if s in BODIES else ""


def _body_from_title(title: str) -> str:
    t = (title or "").lower()
    if "commissioner" in t:
        return "county"
    if "city council" in t:
        return "city"
    if "board of education" in t or "school board" in t:
        return "schools"
    return ""


def _norm_date(s: str) -> str:
    s = (s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _parse_long(s: str):
    try:
        return dt.datetime.strptime(s.replace(",", ""), "%B %d %Y").date()
    except ValueError:
        return None


def _epoch_date(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts)).date()
    except (TypeError, ValueError):
        return None
