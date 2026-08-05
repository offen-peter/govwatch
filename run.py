"""
GovWatch entrypoint.

Seven modes, one process. The scheduled one is `tick`: it asks
schedule.py what could plausibly have appeared since the last run and
does only that work, which is what keeps a twice daily cron cheap. The
rest are either manual overrides that ignore the calendar (poll,
transcribe, digest) or one-time setup helpers that inspect a remote
system and print what they found (probe, discover).

brief.py is imported lazily inside the functions that need it, not at
module scope. It builds an Anthropic client at import time, so importing
it eagerly would make `probe`, `discover` and `schedule` fail on a
machine with no API key set, and those are exactly the commands you run
before setup is finished.

State, all under state/ and all committed back by the workflow:

    seen.json       document uid -> what it was and when we first saw it.
                    De-duplication happens here, so every adapter can be
                    re-run freely.
    records.json    extraction records, append only. The audit trail: a
                    claim in a brief traces back to one of these.
    failures.json   which adapters could not reach their source on the
                    last poll. Feeds the digest's Gaps section, so a
                    scraper failing quietly still shows up in the brief.

The calendar bookkeeping (calendar.json, acquired.json,
last_checked.json) belongs to schedule.py and is written there.
"""

from __future__ import annotations

import os
import sys
import json
import smtplib
import datetime as dt
from pathlib import Path
from email.message import EmailMessage

import yaml

from govwatch import schedule as sched
from govwatch import video as video_source
from govwatch.sources import ADAPTERS

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))

STATE = ROOT / "state"
BRIEFS = ROOT / "briefs"
STATE.mkdir(exist_ok=True)
BRIEFS.mkdir(exist_ok=True)

SEEN = STATE / "seen.json"
RECORDS = STATE / "records.json"
FAILURES = STATE / "failures.json"

# Doc.kind is what an adapter produces. Window artifacts are what
# schedule.py watches for. They are not the same vocabulary, because a
# school board packet and a county agenda close the same window.
ARTIFACT_OF_KIND = {
    "agenda": "agenda",
    "packet": "agenda",
    "minutes": "minutes",
    "transcript": "video",
    "notice": "press",
}


# ================================================================ state

def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"{path.name} is corrupt, starting from empty")
        return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _remember(seen: dict, doc) -> None:
    seen[doc.uid] = {
        "body": doc.body,
        "kind": doc.kind,
        "title": doc.title,
        "meeting_date": doc.meeting_date,
        "url": doc.url,
        "first_seen": dt.date.today().isoformat(),
    }


def _close_window(doc) -> None:
    """
    An artifact in hand means stop looking for it. Only fires when the
    document maps onto a scheduled meeting: the county news feed and
    press coverage are not tied to one, so they never close anything.
    """
    artifact = ARTIFACT_OF_KIND.get(doc.kind)
    if not artifact or not doc.meeting_date:
        return
    key = sched.match_meeting(doc.body, doc.meeting_date)
    if key:
        sched.mark_acquired(key, artifact)


# ============================================================ documents

def poll(only_bodies: set[str] | None = None) -> list[dict]:
    """
    Every document adapter, de-duplicated and extracted.

    Ignores windows entirely. `tick` is the window aware caller and
    passes only_bodies; running this directly is the deliberate override
    for a first run or a backfill.
    """
    from govwatch import brief

    seen = _load(SEEN, {})
    records = _load(RECORDS, [])
    watchlist = CONFIG.get("watchlist", [])
    lookback = CONFIG.get("lookback_days", 60)

    fresh, failures = [], {}
    for adapter in ADAPTERS:
        if only_bodies and adapter.body not in only_bodies:
            continue
        try:
            docs = adapter.collect(lookback)
        except Exception as e:
            # A source that has started failing silently is the failure
            # mode this project is most exposed to, so record it rather
            # than letting an empty result look like a quiet week.
            failures[adapter.body] = f"{type(e).__name__}: {e}"[:300]
            print(f"adapter failed for {adapter.body}: {e}")
            continue
        for doc in docs:
            if doc.uid in seen:
                continue
            _remember(seen, doc)
            fresh.append(doc)

    print(f"{len(fresh)} new document(s)")
    new_records = _extract_all(fresh, brief, watchlist)

    _save(SEEN, seen)
    _save(RECORDS, records + new_records)
    _save(FAILURES, {
        "run": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "poll",
        "failures": failures,
    })
    return new_records


# =============================================================== video

def transcribe(only_bodies: set[str] | None = None) -> list[dict]:
    """
    Meeting video to committed transcript to extraction record.

    The transcript itself is written by video.py into transcripts/ and is
    the durable artifact here. What comes back is the record, which is
    disposable: it can be rebuilt from the markdown at any time.
    """
    from govwatch import brief

    vcfg = CONFIG.get("video", {})
    if not vcfg.get("enabled", True):
        print("video is disabled in config.yml")
        return []

    seen = _load(SEEN, {})
    records = _load(RECORDS, [])
    watchlist = CONFIG.get("watchlist", [])
    roster = vcfg.get("roster", [])

    docs = video_source.collect(
        vcfg.get("lookback_days", 21),
        roster,
        only_bodies=only_bodies,
    )

    fresh = []
    for doc in docs:
        if doc.uid in seen:
            continue
        _remember(seen, doc)
        fresh.append(doc)

    print(f"{len(fresh)} new transcript(s)")
    new_records = _extract_all(fresh, brief, watchlist, roster)

    _save(SEEN, seen)
    _save(RECORDS, records + new_records)
    return new_records


def _extract_all(docs, brief, watchlist, roster=None) -> list[dict]:
    """
    One Haiku call per document. For transcripts this call does speaker
    identification as well, see brief.extract on why those are not two
    passes any more.
    """
    out = []
    for doc in docs:
        try:
            rec = brief.extract(doc.to_dict(), watchlist, roster)
        except Exception as e:
            print(f"extraction failed for {doc.title}: {e}")
            continue
        if not rec:
            print(f"nothing extracted from {doc.title}, skipping")
            continue
        rec["_extracted"] = dt.date.today().isoformat()
        out.append(rec)
        _close_window(doc)
        print(f"extracted {doc.body} {doc.kind}: {doc.title}")
    return out


# =============================================================== digest

def digest(force: bool = False) -> str | None:
    """
    Synthesize the records inside the reporting window into one brief,
    write it to briefs/, and email it.

    The window is on extraction date, not meeting date. County minutes
    approved six weeks late are news the week they arrive, not the week
    the meeting happened, and windowing on meeting date would drop them
    on the floor.
    """
    from govwatch import brief

    records = _load(RECORDS, [])
    days = CONFIG.get("digest_days", 14)
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=days)).isoformat()

    window = [r for r in records if (r.get("_extracted") or "") >= cutoff]
    if not window and not force:
        print("nothing new in the reporting window, no digest")
        return None

    missing = _gaps()
    period = f"{cutoff} to {today.isoformat()}"
    text = brief.synthesize(window, missing, period)

    path = BRIEFS / f"{today.isoformat()}-brief.md"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} from {len(window)} record(s)")

    send_email(f"GovWatch brief, {period}", text)
    return text


def _gaps() -> list[str]:
    """
    What the brief did not have. Three kinds, and all three belong in the
    Gaps section: the structural lags that are true every cycle, adapters
    that could not reach their source this cycle, and windows still open
    on a meeting that has already happened.

    That last one is the useful one. It is the difference between "no
    county minutes exist yet" and "county minutes exist and we failed to
    get them", which a reader cannot otherwise tell apart.
    """
    gaps = list(CONFIG.get("known_gaps", []))

    for body, err in (_load(FAILURES, {}).get("failures") or {}).items():
        gaps.append(f"The {body} adapter could not reach its source on the "
                    f"last poll: {err}")

    today = dt.date.today()
    try:
        due = sched.open_windows(respect_cadence=False)
    except Exception as e:
        gaps.append(f"The meeting calendar could not be rebuilt: {e}")
        return gaps

    for artifact, meetings in due.items():
        for m in meetings:
            if dt.date.fromisoformat(m.date) <= today:
                gaps.append(f"No {artifact} retrieved for the {m.body} meeting "
                            f"on {m.date}.")
    return gaps


# =============================================================== alerts

def alert(new_records: list[dict]) -> None:
    """
    Watchlist hits between digests.

    These are only worth having while they stay rare. If they start
    arriving daily the answer is to prune config.yml's watchlist, not to
    filter here.
    """
    if not CONFIG.get("alerts", {}).get("enabled", True):
        return

    hits = []
    for rec in new_records:
        terms = rec.get("watchlist_hits") or []
        if not terms:
            continue
        src = rec.get("_source", {})
        hits.append(
            f"{', '.join(terms)}\n"
            f"  {src.get('body', '')} {src.get('kind', '')}: {src.get('title', '')}\n"
            f"  {src.get('url', '')}"
        )

    if not hits:
        return
    send_email(f"GovWatch alert: {len(hits)} watchlist hit(s)",
               "\n\n".join(hits) + "\n\nFull context lands in the next brief.\n")


# ================================================================ email

def send_email(subject: str, body: str) -> None:
    """
    Gmail SMTP with an app password. Never fatal: a send failure must not
    lose the brief, which is already written to briefs/ by this point.
    """
    ecfg = CONFIG.get("email", {})
    if not ecfg.get("enabled", True):
        print("email is disabled in config.yml")
        return

    to = [a for a in ecfg.get("to", []) if a and "CHANGE_ME" not in a]
    if not to:
        print("no email recipients configured, skipping send")
        return

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and password):
        print("GMAIL_USER or GMAIL_APP_PASSWORD not set, skipping send")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"emailed {len(to)} recipient(s): {subject}")
    except Exception as e:
        print(f"email send failed: {e}")


# ================================================================= tick

def tick() -> None:
    """
    The scheduled entrypoint. Does only what the open windows justify.

    Most runs find nothing open and exit in about a second, which is the
    whole point: the calendar is known, so there is no reason to poll
    around the clock for something that cannot exist yet.
    """
    windows = sched.open_windows()
    today = dt.date.today()
    digest_day = today.weekday() == CONFIG.get("digest_weekday", 4)

    if not windows and not digest_day:
        print("no open windows, nothing to do")
        return

    new_records: list[dict] = []

    doc_artifacts = [a for a in ("agenda", "minutes", "press") if a in windows]
    if doc_artifacts:
        bodies = {m.body for a in doc_artifacts for m in windows[a]}
        if "press" in windows:
            # Press coverage is not a body on the calendar, it is the
            # newspaper covering one. An open press window means run the
            # newspaper adapter too.
            bodies.add("press")
        print(f"open document windows: {', '.join(doc_artifacts)} "
              f"for {', '.join(sorted(bodies))}")
        new_records += poll(only_bodies=bodies)
        for artifact in doc_artifacts:
            sched.mark_checked(artifact, windows[artifact])

    if "video" in windows:
        bodies = {m.body for m in windows["video"]}
        print(f"open video window for {', '.join(sorted(bodies))}")
        new_records += transcribe(only_bodies=bodies)
        # Marked whether or not anything was found. The cadence counts
        # attempts, not successes, otherwise a body that has not posted
        # yet gets hammered every run.
        sched.mark_checked("video", windows["video"])

    if new_records:
        alert(new_records)

    if digest_day:
        digest()


# =============================================================== setup

def probe() -> None:
    """Which of the county's Granicus endpoints answer. Setup only."""
    from govwatch.video import GranicusSource
    print(json.dumps(GranicusSource().probe(), indent=2))


def discover() -> None:
    """What the CivicClerk Events collection actually returns. Setup only."""
    from govwatch.sources import CityCouncil
    print(json.dumps(CityCouncil().discover(), indent=2))


# ================================================================= main

MODES = {
    "tick":       lambda: tick(),
    "schedule":   lambda: print(sched.describe()),
    "poll":       lambda: alert(poll()),
    "transcribe": lambda: alert(transcribe()),
    "digest":     lambda: digest(force=True),
    "probe":      lambda: probe(),
    "discover":   lambda: discover(),
}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if mode not in MODES:
        print(f"unknown mode: {mode}")
        print("modes: " + ", ".join(MODES))
        return 2
    MODES[mode]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
