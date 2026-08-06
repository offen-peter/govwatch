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
import re
import sys
import json
import math
import smtplib
import collections
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

# The durable archive, and the only thing `ask` reads.
TRANSCRIPTS = ROOT / "transcripts"

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
    new_records = _extract_all(fresh, brief, watchlist, None, seen, records)

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
    # skips straight past captions and Whisper and only repeats the
    # Haiku call.
    return _extract_all(fresh, brief, watchlist, roster, seen, records)


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


def _extract_all(docs, brief, watchlist, roster, seen: dict, records: list):
    """
    One Haiku call per document. Returns the records added this call.

    Owns its own persistence, and writes after every document rather
    than once at the end. A document is recorded as seen when extraction
    reached a verdict about it, not merely when we tried:

      returned a record   seen, it worked
      returned {}         seen, the content is unusable and always will
                          be, either too short or unparseable
      raised              NOT seen. Auth, network and rate limit
                          failures all land here and all are temporary.
                          Leave it unseen so the next run tries again.

    Two orderings here are load bearing, and both were learned by
    getting them wrong.

    Persist before closing the window. The window is the pipeline's
    claim that it already holds an artifact, so closing one without
    having saved the record means the pipeline stops looking for
    something it cannot show you. Saving first makes the failure mode
    harmless: a window that fails to close just gets rechecked, and
    seen.json stops the document being extracted twice.

    Save every document, not every run. On 2026-08-06 a transcribe run
    extracted three transcripts, closed their video windows, then died
    before the single save at the end. acquired.json had advanced,
    records.json had not, and three meetings were marked as held while
    the records for them were gone. Batching the write meant a crash
    discarded every success that preceded it.

    _close_window is also inside a try now. It reads the calendar, which
    can touch the network, and no bookkeeping step should be able to
    kill a run that has already done the expensive part.
    """
    added = []
    for doc in docs:
        try:
            rec = brief.extract(doc.to_dict(), watchlist, roster)
        except Exception as e:
            print(f"extraction failed, leaving unseen for a retry: "
                  f"{doc.title}: {e}")
            continue

        _remember(seen, doc)
        if rec:
            rec["_extracted"] = dt.date.today().isoformat()
            records.append(rec)
            added.append(rec)

        # Durable before bookkeeping, and before the next API call.
        _save(SEEN, seen)
        _save(RECORDS, records)

        if not rec:
            print(f"nothing extractable in {doc.title}, not retrying")
            continue

        try:
            _close_window(doc)
        except Exception as e:
            print(f"window bookkeeping failed for {doc.title}, "
                  f"it will be rechecked: {e}")
        print(f"extracted {doc.body} {doc.kind}: {doc.title}")
    return added


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

def _watchlist_terms(values, watchlist: list[str]):
    """
    Reduce whatever extraction returned down to the configured terms it
    actually names. Returns (terms, unmatched_count).

    The prompt asks for terms from the watchlist and nothing else, and
    document extractions obey it: "stormwater", "decorum", "public
    comment". Transcript extractions drift. The 2026-06-22 county record
    came back with 20 entries shaped like "landfill capacity declines
    and decisions about future of solid waste system" rather than
    "landfill".

    Inside records.json that phrasing is useful context and is left
    alone. In an alert it is the difference between something scannable
    in a notification and twenty sentences nobody reads. So the
    normalizing happens here, at the alert boundary, and the record
    keeps what the model wrote.

    A string naming no configured term is dropped rather than passed
    through. The prompt forbids inventing hits, and an alert firing on
    something that is not on the list is exactly what teaches a reader
    to stop opening alerts.
    """
    terms, unmatched = set(), 0
    for raw in values:
        low = str(raw).lower()
        found = [t for t in watchlist if t.lower() in low]
        if found:
            terms.update(found)
        else:
            unmatched += 1
    return sorted(terms), unmatched


def alert(new_records: list[dict]) -> None:
    """
    Watchlist hits between digests.

    These are only worth having while they stay rare. If they start
    arriving daily the answer is to prune config.yml's watchlist, not to
    filter here.
    """
    if not CONFIG.get("alerts", {}).get("enabled", True):
        return

    watchlist = CONFIG.get("watchlist", [])
    blocks, tally, dropped = [], collections.Counter(), 0
    for rec in new_records:
        raw = rec.get("watchlist_hits") or []
        if not raw:
            continue
        terms, unmatched = _watchlist_terms(raw, watchlist)
        dropped += unmatched
        if not terms:
            continue
        tally.update(terms)
        src = rec.get("_source", {})
        blocks.append(
            f"{', '.join(terms)}\n"
            f"  {src.get('body', '')} {src.get('kind', '')}: {src.get('title', '')}\n"
            f"  {src.get('url', '')}"
        )

    if dropped:
        print(f"{dropped} reported watchlist hit(s) named no configured term "
              f"and were ignored for alerting")
    if not blocks:
        return

    # The terms belong in the subject. That is the part read on a phone
    # without opening anything, and it is what decides whether this is
    # worth interrupting for.
    #
    # Ordered by how often each term came up, not alphabetically. Sorted
    # by name the line opened with "DEQ, DSS, Department of Environmental
    # Quality" regardless of what the cycle was actually about. Held to
    # three terms because mail clients cut a subject around 78
    # characters, and a term truncated mid word is worse than a count.
    named = [t for t, _ in tally.most_common()]
    subject = "GovWatch alert: " + ", ".join(named[:3])
    if len(named) > 3:
        subject += f", and {len(named) - 3} more"

    send_email(subject,
               "\n\n".join(blocks) + "\n\nFull context lands in the next brief.\n")


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


# ================================================================== ask

# Words too common to narrow anything down. Deliberately short: this is a
# filter on query terms, not an attempt at linguistics.
ASK_STOPWORDS = {
    "the", "and", "for", "was", "were", "what", "who", "when", "where",
    "did", "does", "any", "anyone", "about", "said", "say", "says", "this",
    "that", "with", "from", "have", "has", "had", "how", "why", "are",
    "our", "their", "there", "they", "them", "been", "being", "would",
    "could", "should", "will", "can", "all", "some", "more", "meeting",
}

# One question's worth of excerpts. Far below the extraction cap on
# purpose: an answer built from 100,000 characters the reader will never
# check is worth less than one built from 10,000 they might.
ASK_MAX_CHARS = 120_000

# A second bound, on count rather than size. The character budget alone
# would happily pass sixty weak matches, and an answer assembled from
# sixty loosely related paragraphs is worse than one from the fifteen
# that actually bear on the question, as well as costing more.
ASK_MAX_PARAGRAPHS = 40


def _ask_terms(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9'\-]{3,}", question.lower())
    terms = [w for w in dict.fromkeys(words) if w not in ASK_STOPWORDS]
    # If the question was nothing but stopwords, search them rather than
    # searching nothing.
    return terms or list(dict.fromkeys(words))


def _frontmatter(raw: str) -> dict:
    if not raw.startswith("---"):
        return {}
    head = raw.split("---", 2)[1]
    out = {}
    for line in head.splitlines():
        key, sep, val = line.partition(":")
        if sep and not key.startswith(" "):
            out[key.strip()] = val.strip()
    return out


def _grep_transcripts(terms: list[str], budget: int = ASK_MAX_CHARS):
    """
    Matching paragraphs from the archive, best match first.

    Paragraphs are what _vtt_to_text already writes, each opening with a
    timestamp, so a match arrives carrying its own citation and needs no
    further processing. That is the practical argument for the archive
    being markdown rather than JSON, and it only pays off here.

    Ranked by how many distinct query terms a paragraph contains, then by
    recency, then truncated to a character budget. Crude, and enough: the
    corpus is a few hundred paragraphs per meeting, not a search engine
    problem.
    """
    paras = []
    for path in sorted(TRANSCRIPTS.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta = _frontmatter(raw)
        body = raw.split("---", 2)[-1] if raw.startswith("---") else raw
        for para in body.split("\n\n"):
            para = para.strip()
            if not para or para.startswith((">", "#")):
                continue
            paras.append((path.name, meta, para, para.lower()))

    # Weight rare terms above common ones, roughly as tf-idf does.
    #
    # Plain term counting ranks badly on real questions. "zoning variance
    # on Broad Street" matched 72 paragraphs across every meeting in the
    # archive, because the county offices are at 101 S. Broad St. and the
    # word street is everywhere, while zoning and variance are the terms
    # that actually carry the question. Counting matches treats all four
    # as equal.
    #
    # A term appearing in most paragraphs is behaving like a stopword for
    # this corpus whatever the dictionary says, so it is dropped outright
    # rather than merely down-weighted.
    total_paras = len(paras) or 1
    df = {t: sum(1 for _n, _m, _p, low in paras if t in low) for t in terms}
    useful = [t for t in terms if 0 < df[t] <= total_paras * 0.5]
    if not useful:
        # Every term is either absent or ubiquitous. Fall back to whatever
        # was present, so a valid question about a common subject still
        # returns something.
        useful = [t for t in terms if df[t]]
    weight = {t: 1.0 / (1 + math.log(df[t])) for t in useful}

    hits = []
    for name, meta, para, low in paras:
        matched = [t for t in useful if t in low]
        if matched:
            hits.append([sum(weight[t] for t in matched), name, meta, para])

    considered = len(hits)
    hits.sort(key=lambda h: h[1], reverse=True)   # newest meeting first
    hits.sort(key=lambda h: -h[0])                # then best match, stably
    hits = hits[:ASK_MAX_PARAGRAPHS]

    kept, total = [], 0
    for h in hits:
        if total + len(h[3]) > budget:
            break
        kept.append(h)
        total += len(h[3])
    return kept, total, considered, df, useful


def _format_excerpts(hits) -> str:
    """Group by meeting so the model sees which body and date it is reading."""
    by_file: dict[str, tuple] = {}
    for _score, name, meta, para in hits:
        by_file.setdefault(name, (meta, []))[1].append(para)

    out = []
    for name, (meta, paras) in by_file.items():
        out.append(
            f"### {meta.get('meeting_date', '?')} {meta.get('body', '?')}: "
            f"{meta.get('title', name)}\n"
            f"source: {meta.get('source', '')}\n"
            f"method: {meta.get('method', 'unknown')}"
        )
        out.extend(paras)
    return "\n\n".join(out)


def ask(question: str, dry: bool = False) -> None:
    """
    Ask a question of the transcript archive.

    Greps transcripts/ for the query terms and sends only the paragraphs
    that match, rather than every transcript in full.

    This is the payoff the README predicted for storing transcripts as
    timestamped markdown. Six meetings is already 768,000 characters, so
    asking one question against the whole archive would cost more than a
    month of everything else the pipeline does. The twenty paragraphs
    that mention the landfill cost a fraction of a cent, and every line
    of the answer can point at a meeting and a timestamp.
    """
    terms = _ask_terms(question)
    hits, total, considered, df, useful = _grep_transcripts(terms)

    # Report each term's standing, because a term the archive has never
    # heard of contributes nothing and the answer then rests entirely on
    # whichever terms did match. Silently answering a narrower question
    # than the one asked is the failure worth guarding against here.
    absent = [t for t in terms if not df.get(t)]
    common = [t for t in terms if df.get(t) and t not in useful]
    print("terms: " + ", ".join(f"{t}({df.get(t, 0)})" for t in terms))
    if absent:
        print(f"  not in the archive at all: {', '.join(absent)}")
    if common:
        print(f"  in over half of all paragraphs, so ignored: {', '.join(common)}")
    if absent and len(absent) == len(terms):
        print("  every term is absent, so there is nothing to answer from")

    print(f"{considered} matching paragraph(s) in the archive, "
          f"{len(hits)} within budget, {total:,} chars "
          f"~ {total // 4:,} tokens")

    if not hits:
        print("\nNothing in the archive matches. Try broader or fewer terms.")
        print(f"The archive holds {len(list(TRANSCRIPTS.glob('*.md')))} meeting(s).")
        return

    meetings = sorted({h[2].get("meeting_date", "?") for h in hits})
    print(f"across {len(meetings)} meeting(s): {', '.join(meetings)}")

    if dry:
        print("\nDRY RUN. No API call, no spend. Best matches:\n")
        for score, name, _meta, para in hits[:8]:
            print(f"  [{score:.2f}] {name}")
            print(f"    {para[:180]}\n")
        return

    from govwatch import brief
    print()
    print(brief.answer(question, _format_excerpts(hits)))
    print()


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
#
# ask is dry capable for a different reason than the rest. A dry ask
# prints which paragraphs the grep found and what sending them would
# cost, which is the cheapest way to tell a badly worded question from
# an archive that genuinely has nothing on the subject.
DRY_CAPABLE = ("tick", "poll", "transcribe", "digest", "ask")


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

    # ask carries a question rather than nothing, so it is dispatched
    # here rather than through MODES, whose entries all take only dry.
    if mode == "ask":
        question = " ".join(args[1:]).strip()
        if not question:
            print("ask needs a question, quoted:")
            print('  python run.py ask "what was said about the landfill"')
            return 2
        if not dry:
            _require_api_key()
        ask(question, dry=dry)
        return 0

    if mode not in MODES:
        print(f"unknown mode: {mode}")
        print("modes: " + ", ".join(MODES) + ", ask")
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
