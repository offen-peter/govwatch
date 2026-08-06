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

# video.py's pointer files, one per video id. Read only from here, and
# only so a dry run can say which videos already have a transcript.
CACHE_VIDEO = STATE / "video"

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
    An artifact in hand means stop looking for it.

    The county news feed and press coverage are both kind "notice", and
    both are stamped with today's date rather than a meeting's, so only
    the newspaper is allowed to close a press window. Otherwise a county
    notice fetched today would close the press window on a county
    meeting held two days ago, which is a different thing entirely.
    """
    artifact = ARTIFACT_OF_KIND.get(doc.kind)
    if not artifact or not doc.meeting_date:
        return
    if artifact == "press" and doc.body != "press":
        return
    key = sched.match_meeting(doc.body, doc.meeting_date)
    if key:
        sched.mark_acquired(key, artifact)


# ============================================================ documents

def _collect_documents(only_bodies: set[str] | None):
    """
    Run the document adapters. Returns (docs, failures, attempted).

    attempted is every body we actually ran, which is not recoverable
    from docs or failures. An adapter that returns an empty list without
    raising appears in neither, and that is the exact shape of a scraper
    that has quietly stopped working.
    """
    lookback = CONFIG.get("lookback_days", 60)
    docs, failures, attempted = [], {}, []
    for adapter in ADAPTERS:
        if only_bodies and adapter.body not in only_bodies:
            continue
        attempted.append(adapter.body)
        try:
            got = adapter.collect(lookback)
        except Exception as e:
            # A source that has started failing silently is the failure
            # mode this project is most exposed to, so record it rather
            # than letting an empty result look like a quiet week.
            failures[adapter.body] = f"{type(e).__name__}: {e}"[:300]
            print(f"adapter failed for {adapter.body}: {e}")
            continue
        docs.extend(got)
    return docs, failures, attempted


def poll(only_bodies: set[str] | None = None, dry: bool = False) -> list[dict]:
    """
    Every document adapter, de-duplicated and extracted.

    Ignores windows entirely. `tick` is the window aware caller and
    passes only_bodies; running this directly is the deliberate override
    for a first run or a backfill.

    dry stops after collection: it reports what a real run would extract
    and returns nothing. See _dry_report_docs for why it writes no state.
    """
    seen = _load(SEEN, {})
    docs, failures, attempted = _collect_documents(only_bodies)
    stubs = [d for d in docs if len(d.text) < MIN_USEFUL_CHARS]
    fresh = [d for d in docs
             if d.uid not in seen and len(d.text) >= MIN_USEFUL_CHARS]
    for d in stubs:
        print(f"placeholder, leaving for a later poll: {d.body} {d.kind} "
              f"{d.meeting_date} ({len(d.text)} chars)")

    if dry:
        _dry_report_docs(docs, fresh, failures, attempted)
        return []

    from govwatch import brief

    records = _load(RECORDS, [])
    watchlist = CONFIG.get("watchlist", [])

    print(f"{len(fresh)} new document(s)")
    new_records, settled = _extract_all(fresh, brief, watchlist)
    for doc in settled:
        _remember(seen, doc)

    _save(SEEN, seen)
    _save(RECORDS, records + new_records)
    _save(FAILURES, {
        "run": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "poll",
        "failures": failures,
    })
    return new_records


# =============================================================== video

def transcribe(only_bodies: set[str] | None = None, dry: bool = False) -> list[dict]:
    """
    Meeting video to committed transcript to extraction record.

    The transcript itself is written by video.py into transcripts/ and is
    the durable artifact here. What comes back is the record, which is
    disposable: it can be rebuilt from the markdown at any time.

    dry stops at discovery, before anything is downloaded or fed to
    Whisper. That is the expensive half, so a dry transcribe is the cheap
    way to answer whether a source is finding video at all.
    """
    vcfg = CONFIG.get("video", {})
    if not vcfg.get("enabled", True):
        print("video is disabled in config.yml")
        return []

    roster = vcfg.get("roster", [])

    if dry:
        _dry_report_videos(only_bodies, vcfg)
        return []

    from govwatch import brief

    seen = _load(SEEN, {})
    records = _load(RECORDS, [])
    watchlist = CONFIG.get("watchlist", [])

    docs = video_source.collect(
        vcfg.get("lookback_days", 21),
        roster,
        only_bodies=only_bodies,
    )

    fresh = [d for d in docs if d.uid not in seen]

    print(f"{len(fresh)} new transcript(s)")
    # Same rule as poll: seen is written from the extraction verdict, not
    # from having tried. Retrying costs nothing extra here, since the
    # transcript markdown and its pointer file already exist, so a retry
    # skips straight past captions and Whisper.
    new_records, settled = _extract_all(fresh, brief, watchlist, roster)
    for doc in settled:
        _remember(seen, doc)

    _save(SEEN, seen)
    _save(RECORDS, records + new_records)
    return new_records


# ============================================================== dry run

# brief.extract skips anything under this and truncates anything over it,
# so the cost estimate below has to apply the same limits to mean
# anything. Keep in step with brief.extract if those numbers move.
MIN_EXTRACT_CHARS = 200
MAX_EXTRACT_CHARS = 180_000

# Below this a document is a placeholder, not a document: a meeting node
# that exists because the meeting is scheduled, with the packet not yet
# attached. The school board publishes these about a week ahead.
#
# They must not be recorded as seen. Doc.uid hashes body, kind and url,
# and the url is stable from the moment the node is created, so marking
# a stub seen means the real agenda inherits the same uid and gets
# skipped as already handled when it finally lands. The document would
# never be read at all. Skipping them entirely leaves the window open so
# the next poll picks up the real thing.
#
# Set from measurement, and the margin is narrow, so move it carefully.
# On 2026-08-05 the school board stub for the 2026-08-17 meeting was 218
# characters, and the shortest genuine document across all four adapters
# was a 378 character committee notice. Too high and real short notices
# get skipped every poll and never recorded at all, which is a worse
# failure than the one this guards against.
MIN_USEFUL_CHARS = 300

# Haiku 4.5 input, dollars per million tokens, per the README's rate
# table. Output is a small fraction of a run's cost and is ignored here,
# so treat the figure as a floor, not a quote.
HAIKU_INPUT_PER_MTOK = 1.00


def _dry_report_docs(docs: list, fresh: list, failures: dict,
                     attempted: list) -> None:
    """
    What a real poll would do, without doing it.

    This writes nothing, and that is the point rather than an
    optimization. A dry run that recorded documents in seen.json would
    make the next real run skip them as already handled, which is the one
    way a preview could do damage. Same reason tick skips mark_checked in
    dry mode: stamping the cadence clock would suppress the real check.
    """
    print("\nDRY RUN. Nothing extracted, nothing written, no API spend.\n")

    # Driven by what ran, not by what came back. A body that returned
    # nothing has to appear here or the empty result is invisible, which
    # is the whole failure mode this report is for.
    bodies = sorted(set(attempted) | {d.body for d in docs} | set(failures))
    if not bodies:
        print("  no adapter ran.")

    fresh_uids = {d.uid for d in fresh}
    for body in bodies:
        mine = [d for d in docs if d.body == body]
        new = [d for d in mine if d.uid in fresh_uids]
        if body in failures:
            print(f"  {body:8s} FAILED: {failures[body]}")
            continue
        if not mine:
            # Not an error, but the shape a silently broken adapter takes,
            # so it is called out rather than left as a blank line.
            print(f"  {body:8s} returned 0 documents, worth a look")
            continue
        print(f"  {body:8s} {len(mine)} document(s), {len(new)} new")
        for d in new:
            print(f"      {d.kind:10s} {d.meeting_date or '????-??-??'} "
                  f"{len(d.text):>7} chars  {d.title[:58]}")

    billable = [min(len(d.text), MAX_EXTRACT_CHARS) for d in fresh
                if len(d.text) >= MIN_EXTRACT_CHARS]
    skipped = len(fresh) - len(billable)
    tokens = sum(billable) // 4
    print(f"\n  {len(fresh)} new document(s) would be extracted"
          + (f", {skipped} skipped as too short" if skipped else ""))
    print(f"  roughly {tokens:,} input tokens, about "
          f"${tokens / 1_000_000 * HAIKU_INPUT_PER_MTOK:.2f} at Haiku input rates.")
    print("  Rough: 4 chars per token, input only, before prompt caching.\n")


def _dry_report_videos(only_bodies: set[str] | None, vcfg: dict) -> None:
    """
    Which videos a real transcribe would pick up.

    Stops at discovery. Nothing is downloaded, no captions are fetched
    and Whisper is never loaded, so this costs a few HTTP requests and
    answers the question that actually matters day to day: is this source
    finding video at all.
    """
    print("\nDRY RUN. Discovery only, nothing downloaded or transcribed.\n")

    lookback = vcfg.get("lookback_days", 21)
    cap = vcfg.get("max_per_run", 4)
    total = 0

    for src in video_source.sources():
        body = getattr(src, "body", str(src))
        if only_bodies and body not in only_bodies:
            continue
        try:
            refs = src.discover(lookback)
        except Exception as e:
            print(f"  {body:8s} discovery FAILED: {type(e).__name__}: {e}")
            continue
        if not refs:
            print(f"  {body:8s} found no video in the last {lookback} days")
            continue
        total += len(refs)
        print(f"  {body:8s} {len(refs)} video(s), {min(len(refs), cap)} would be processed")
        for ref in refs[:cap]:
            done = (CACHE_VIDEO / f"{ref.vid}.json").exists()
            print(f"      {ref.date or '????-??-??'}  "
                  f"{'cached' if done else 'NEW':<6}  {ref.title[:58]}")

    print(f"\n  {total} video(s) discovered. Anything marked cached already has a "
          f"transcript\n  and would be reused rather than fetched again.\n")


def _extract_all(docs, brief, watchlist, roster=None):
    """
    One Haiku call per document. Returns (records, settled).

    settled is the documents that may now be recorded in seen.json, and
    it is deliberately not the same as docs. A document is settled when
    extraction reached a verdict about it, not merely when we tried:

      returned a record   settled, it worked
      returned {}         settled, the content is unusable and always
                          will be, either too short or unparseable
      raised              NOT settled. Auth, network and rate limit
                          failures all land here and all are temporary.
                          Leave it unseen so the next run tries again.

    This distinction is the whole point. Recording a document as seen
    before knowing extraction worked means one bad run silently consumes
    the backlog: the documents are marked handled, the records are
    empty, and nothing ever revisits them. That is exactly what the
    2026-08-05 tick did, 26 documents marked seen against an empty
    records.json, because the API rejected every call and the per
    document except swallowed it.
    """
    out, settled = [], []
    for doc in docs:
        try:
            rec = brief.extract(doc.to_dict(), watchlist, roster)
        except Exception as e:
            print(f"extraction failed, leaving unseen for a retry: "
                  f"{doc.title}: {e}")
            continue
        settled.append(doc)
        if not rec:
            print(f"nothing extractable in {doc.title}, not retrying")
            continue
        rec["_extracted"] = dt.date.today().isoformat()
        out.append(rec)
        _close_window(doc)
        print(f"extracted {doc.body} {doc.kind}: {doc.title}")
    return out, settled


# =============================================================== digest

def digest(force: bool = False, dry: bool = False) -> str | None:
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

    if dry:
        # The gaps are the part worth previewing. They are assembled from
        # config, live adapter failures and still-open windows, so this is
        # the only way to read them without paying Sonnet to write around
        # them.
        print(f"\nDRY RUN. No brief written, no email sent, no API spend.\n")
        print(f"  reporting period: {period}")
        print(f"  {len(window)} record(s) would be synthesized, "
              f"{len(records)} held in total")
        by_body = {}
        for r in window:
            by_body.setdefault(r.get("_source", {}).get("body", "?"), []).append(r)
        for body, rs in sorted(by_body.items()):
            print(f"    {body:8s} {len(rs)} record(s)")
        print(f"\n  Gaps section would carry {len(missing)} item(s):")
        for m in missing:
            print(f"    - {m[:110]}")
        print()
        return None

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
        # Press is excluded. A newspaper owes nobody an article, so an
        # open press window means nothing was written, not that something
        # was missed. Listing it would bury the gaps that do matter.
        if artifact == "press":
            continue
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

def tick(dry: bool = False) -> None:
    """
    The scheduled entrypoint. Does only what the open windows justify.

    Most runs find nothing open and exit in about a second, which is the
    whole point: the calendar is known, so there is no reason to poll
    around the clock for something that cannot exist yet.

    dry previews the whole decision: which windows are open, what each
    would collect, and what it would cost. It never marks a window
    checked, so a preview cannot suppress the real run that follows it.
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
        new_records += poll(only_bodies=bodies, dry=dry)
        if not dry:
            for artifact in doc_artifacts:
                sched.mark_checked(artifact, windows[artifact])

    if "video" in windows:
        bodies = {m.body for m in windows["video"]}
        print(f"open video window for {', '.join(sorted(bodies))}")
        new_records += transcribe(only_bodies=bodies, dry=dry)
        if not dry:
            # Marked whether or not anything was found. The cadence counts
            # attempts, not successes, otherwise a body that has not posted
            # yet gets hammered every run.
            sched.mark_checked("video", windows["video"])

    if new_records:
        alert(new_records)

    if digest_day:
        digest(dry=dry)


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
    "tick":       lambda dry: tick(dry=dry),
    "schedule":   lambda dry: print(sched.describe()),
    "poll":       lambda dry: alert(poll(dry=dry)),
    "transcribe": lambda dry: alert(transcribe(dry=dry)),
    "digest":     lambda dry: digest(force=True, dry=dry),
    "probe":      lambda dry: probe(),
    "discover":   lambda dry: discover(),
}

# schedule, probe and discover are already read only and already free, so
# --dry-run would mean nothing on them. Rejecting it is better than
# accepting it silently, which would teach the flag as a habit that
# happens to be a no-op here and is load bearing elsewhere.
DRY_CAPABLE = ("tick", "poll", "transcribe", "digest")


def _require_api_key() -> None:
    """
    Fail fast, before any collection, when the key is missing.

    GitHub substitutes an empty string for ${{ secrets.NAME }} when no
    secret of that name exists, so the variable arrives present and
    blank rather than absent. brief.py's os.environ[...] therefore does
    not raise, anthropic.Anthropic constructs happily on an empty key,
    and every call fails auth. The per document except in _extract_all
    then turns that into one swallowed line per document: a run that
    fetches every PDF, extracts nothing, and exits 0.

    That happened twice, on 2026-08-05 and 2026-08-06. Checking here
    turns 25 identical errors and a wasted crawl into one message.
    """
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return
    raise SystemExit(
        "ANTHROPIC_API_KEY is empty or unset, stopping before doing any work.\n"
        "\n"
        "In GitHub Actions an empty value means no repository secret of that\n"
        "exact name exists. GitHub substitutes an empty string rather than\n"
        "failing, so the variable is present and blank.\n"
        "\n"
        "Check Settings, Secrets and variables, Actions, Repository secrets.\n"
        "The name must match ANTHROPIC_API_KEY exactly, with no trailing\n"
        "space, and be a repository secret rather than an environment or\n"
        "organization one unless the workflow declares that environment."
    )


def main() -> int:
    args = list(sys.argv[1:])
    dry = False
    for flag in ("--dry-run", "--dry", "-n"):
        while flag in args:
            args.remove(flag)
            dry = True

    mode = args[0] if args else "tick"
    if mode not in MODES:
        print(f"unknown mode: {mode}")
        print("modes: " + ", ".join(MODES))
        return 2
    if dry and mode not in DRY_CAPABLE:
        print(f"--dry-run does nothing for {mode}, it already reads "
              f"without writing or spending.")
        print("it applies to: " + ", ".join(DRY_CAPABLE))
        return 2

    # DRY_CAPABLE is exactly the set of modes that reach the API, so it
    # doubles as the set that needs a key. A dry run never calls out.
    if mode in DRY_CAPABLE and not dry:
        _require_api_key()

    MODES[mode](dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
