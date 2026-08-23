"""
Source adapters for Transylvania County local government bodies.

Each adapter returns a list of Doc objects. A Doc is one retrievable
artifact: an agenda, a set of minutes, or a packet. The pipeline
de-duplicates on Doc.uid, so adapters can be re-run freely.

Accessibility notes, learned the hard way:

  School board  : Drupal 7. Fully open. The printer-friendly book export
                  returns an entire agenda packet as one HTML document.
                  Easiest of the three by a wide margin.

  County        : The Granicus portal blocks automated access via
                  robots.txt, but the underlying PDFs sit on the county's
                  own web root at a stable, predictable path. Fetch those
                  directly. Approved minutes lag the meeting by four to
                  six weeks, so agendas are the timely signal.

  City          : The public portal is a JavaScript app and cannot be
                  scraped, but CivicClerk exposes an open OData API at
                  {tenant}.api.civicclerk.com. Confirmed working against
                  another tenant. See CityCouncil.discover() before you
                  trust the Events endpoint shape for Brevard.
"""

from __future__ import annotations

import io
import re
import hashlib
import datetime as dt
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "TCDP-GovWatch/1.0 (civic monitoring; contact: info@transcodems.com)"}
TIMEOUT = 45

# How far back the adapters reach for minutes specifically, regardless of
# the lookback the caller asks for.
#
# schedule.WINDOWS gives minutes a T+14 to T+75 window, and run.py passes
# every adapter config.yml's lookback_days, which is 60. Those two numbers
# disagree by fifteen days, so a meeting between 61 and 75 days old sits in
# a minutes window the pipeline holds open while the adapter that would
# satisfy it can no longer see the meeting at all. The window then stays
# open until T+75, and every digest in between reports the minutes as not
# retrieved.
#
# Measured against the live sources on 2026-08-23: County.collect(60)
# returned zero minutes, while the 2026-06-08 and 2026-06-22 sets were both
# linked from the county's index and both carried extractable text, 19,522
# and 99,789 characters. The 2026-06-22 set was named in that day's brief as
# missing.
#
# 90 is T+75 plus a fortnight of slack, so a poll that skips a week still
# reaches back past the end of the window rather than up to its edge.
# Minutes only, because widening the whole lookback would also re-fetch
# city agenda packets, which run to a quarter of a million characters each.
MINUTES_LOOKBACK_DAYS = 90


@dataclass
class Doc:
    body: str            # "city" | "county" | "schools"
    kind: str            # "agenda" | "minutes" | "packet" | "notice"
    title: str
    meeting_date: str    # ISO date, best effort
    url: str
    text: str = ""
    # True for a page that lives at a fixed URL and changes underneath it,
    # as opposed to a document that is published once. See __post_init__.
    rolling: bool = False
    uid: str = field(default="")

    def __post_init__(self):
        if not self.uid:
            key = f"{self.body}|{self.kind}|{self.url}"
            if self.rolling:
                # A rolling page has a constant URL, so a URL-only uid is
                # recorded as seen on the first run and the page is never
                # read again however much it changes.
                #
                # The county news feed is the one this matters for. It
                # carries cancellations, budget hearing notices and public
                # hearing notices, and is where a schedule change surfaces
                # first. It was extracted once on 2026-08-06 and, before
                # this, would never have been read again.
                #
                # Folding the content in means it re-extracts when, and
                # only when, the page actually changes. Cheaper than a
                # date-based uid, which would re-extract daily whether or
                # not anything had happened.
                key += "|" + hashlib.sha1(self.text.encode()).hexdigest()[:12]
            self.uid = hashlib.sha1(key.encode()).hexdigest()[:16]

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- helpers

def get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


def pdf_text(content: bytes) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def rot47(s: str) -> str:
    """
    The Transylvania Times runs on BLOX, which obfuscates article bodies
    with ROT47 rather than paywalling them outright. Decode for reading
    only. Summarize in your own words, never republish the text.
    """
    return "".join(
        chr(33 + ((ord(c) - 33 + 47) % 94)) if 33 <= ord(c) <= 126 else c
        for c in s
    )


# ------------------------------------------------------------ school board

class SchoolBoard:
    """Transylvania County Board of Education. Third Mondays, 6pm."""

    BASE = "https://transylvania.schoolboard.net"
    body = "schools"

    def collect(self, lookback_days: int = 60) -> list[Doc]:
        docs = []
        for path in ("/group/50/past", "/group/50/upcoming"):
            try:
                page = get(self.BASE + path).text
            except Exception:
                continue
            for nid, title, when in self._rows(page):
                if not self._recent(when, lookback_days):
                    continue
                # The book export returns the full packet in one request.
                export = f"{self.BASE}/print/book/export/html/{nid}"
                try:
                    text = html_text(get(export).text)
                except Exception:
                    continue
                docs.append(Doc(
                    body=self.body,
                    kind="packet",
                    title=title,
                    meeting_date=when.isoformat() if when else "",
                    url=f"{self.BASE}/node/{nid}",
                    text=text,
                ))
        return docs

    def _rows(self, html: str):
        """
        One row per meeting node, preferring the copy that has a date.

        The page renders the same view twice: a <ul> of <li> carrying the
        title on its own, and a <table> whose <tr> carries the title, the
        date and the author. The same node id shows up in both.

        So the dedupe has to prefer the dated copy. Keeping the first
        occurrence kept the <li>, which has no date anywhere in it, and
        _recent drops anything with no date. That quietly cost every
        meeting appearing in both renderings, which is the three most
        recent ones, including both upcoming meetings. Those are exactly
        the meetings whose agendas are still worth polling for, so the
        adapter was blind to the only ones that matter and coverage
        stopped dead at the last meeting that had scrolled out of the
        upcoming block.
        """
        soup = BeautifulSoup(html, "html.parser")
        best: dict[str, tuple] = {}
        for a in soup.select('a[href^="/node/"]'):
            m = re.match(r"/node/(\d+)$", a.get("href", ""))
            if not m:
                continue
            nid = m.group(1)
            row = a.find_parent("tr")
            when = self._parse_date(row.get_text(" ") if row else a.get_text(" "))
            # Replace a dateless entry the moment a dated one turns up.
            if nid not in best or (when is not None and best[nid][2] is None):
                best[nid] = (nid, a.get_text(strip=True), when)
        return list(best.values())

    @staticmethod
    def _parse_date(s: str):
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", s)
        if not m:
            return None
        try:
            return dt.datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            return None

    @staticmethod
    def _recent(when, days):
        if when is None:
            return False
        today = dt.date.today()
        return (today - dt.timedelta(days=days)) <= when <= (today + dt.timedelta(days=45))


# ---------------------------------------------------------------- county

class County:
    """
    Transylvania County Board of Commissioners.
    Second Monday 4pm, fourth Monday 6pm.

    Everything comes off the county's own meetings index, which is a
    Drupal view listing one row per meeting with links to the agenda PDF,
    the minutes PDF and the video. Confirmed on 2026-08-05: 25 rows, all
    25 carrying a video link and a resolvable date.

    Access: www.transylvaniacounty.org serves a stock Drupal robots.txt
    that disallows /core/, /admin/, /search/, /user/ and /media/oembed.
    /meetings and /sites/default/files are not disallowed. That is a
    different host from transylvaniacounty.granicus.com, whose HTML is
    disallowed and which this project does not touch.

    This used to guess URLs instead, deriving meeting dates from the
    second and fourth Monday rule and trying a list of known filename
    suffixes for the minutes. Reading the index beats guessing on three
    counts, all of them measured against the live page:

      Suffixes. The guess list did not include "reg mtg & PH",
      "reg mtg _ PH" or "reg mtg & PH (2)", so it silently missed those
      minutes, 2026-06-22 among them.

      Dates. Not every meeting is a second or fourth Monday. The
      2026-06-02 budget workshop and the 2026-05-26 regular meeting are
      both Tuesdays and both were invisible to the rule.

      Agendas. It scraped the HTML meeting page. The index links the
      actual agenda PDF, which is the document the board works from.
    """

    ROOT = "https://www.transylvaniacounty.org"
    INDEX = ROOT + "/meetings"
    body = "county"

    # One fetch of the index per process. Both the document adapter and
    # the Vimeo video adapter read these rows, so a tick that polls and
    # transcribes was pulling the same page twice. The county's bandwidth
    # is not ours to spend twice for one answer.
    _rows_cache: list | None = None

    def collect(self, lookback_days: int = 90) -> list[Doc]:
        docs, empty, unreachable = [], [], []
        today = dt.date.today()
        cutoff = today - dt.timedelta(days=lookback_days)
        # Minutes reach further back than everything else. See
        # MINUTES_LOOKBACK_DAYS: the minutes window outlives the lookback
        # run.py passes, so without this the adapter goes blind on a
        # meeting the pipeline is still actively waiting on.
        minutes_cutoff = today - dt.timedelta(
            days=max(lookback_days, MINUTES_LOOKBACK_DAYS))

        for row in self.rows():
            when = row["date"]
            if not when or when < minutes_cutoff:
                continue
            for kind in ("agenda", "minutes"):
                if kind != "minutes" and when < cutoff:
                    continue
                url = row.get(kind)
                if not url:
                    continue
                try:
                    r = get(url)
                except Exception as e:
                    # Was a bare continue. An agenda PDF that has started
                    # 404ing looks exactly like a meeting that has not
                    # posted one yet, and only one of those is a problem.
                    unreachable.append(f"{when} {kind}: {type(e).__name__}")
                    continue
                text = (pdf_text(r.content)
                        if "pdf" in r.headers.get("content-type", "").lower()
                        else html_text(r.text))
                if not text.strip():
                    # A valid PDF holding an image of a page, as most of
                    # the elections board's minutes are. Nothing here can
                    # read it, so say so rather than dropping it.
                    empty.append(f"{when} {kind}")
                    continue
                docs.append(Doc(self.body, kind,
                                f"Commissioners {kind} {when:%m/%d/%y}",
                                when.isoformat(), url, text))

        if unreachable:
            print(f"county: {len(unreachable)} document(s) could not be "
                  f"fetched: {'; '.join(unreachable[:4])}")
        if empty:
            print(f"county: {len(empty)} document(s) had no extractable "
                  f"text, probably scanned images: {'; '.join(empty[:4])}")
        docs.extend(self._notices())
        return docs

    def rows(self) -> list[dict]:
        """
        One dict per meeting off the index: date, agenda, minutes, video.

        Shared with the video adapter, which wants the same rows for a
        different column, so parsing lives here rather than in both.
        Cached for the life of the process for the same reason.
        """
        if County._rows_cache is not None:
            return County._rows_cache
        try:
            html = get(self.INDEX).text
        except Exception as e:
            print(f"county meetings index unavailable: {e}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        out = []
        for row in soup.select("div.meeting-listing"):
            hrefs = [a.get("href", "") for a in row.select("a[href]")]
            rec = {
                "agenda":  self._abs(next((h for h in hrefs if "/agendas/" in h), "")),
                "minutes": self._abs(next((h for h in hrefs if "/minutes/" in h), "")),
                "video":   next((h for h in hrefs
                                 if "vimeo.com" in h or "granicus.com" in h), ""),
                "title":   " ".join(row.get_text(" ", strip=True).split())[:120],
            }
            rec["date"] = self._row_date(rec, row)
            if rec["date"]:
                out.append(rec)
        County._rows_cache = out
        return out

    @staticmethod
    def _row_date(rec: dict, row) -> dt.date | None:
        """
        Row text first, filename second.

        This used to be the other way round, on the reasoning that a
        YYYY-MM-DD filename is unambiguous where MM/DD/YY is not. Both
        are unambiguous. The difference is that the row text is rendered
        by the county from the meeting record, while the filename is
        typed by hand once and never looked at again.

        Checked against all 25 rows of the live index on 2026-08-23: the
        two agree everywhere except the 08/24/26 meeting, whose agenda is
        filed as "2024-08-24 BOC Agenda.pdf" and opens "MONDAY, AUGUST
        24, 2026". Trusting the filename dated that row two years early,
        put it outside every lookback, and meant the agenda for the next
        county meeting was never fetched at all.

        The filename stays as the fallback, and it is not a formality:
        the two budget workshop rows carry no date in their text and are
        dated from their filenames alone.
        """
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", row.get_text(" ", strip=True))
        if m:
            try:
                return dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        m = re.search(r"/(?:agendas|minutes)/(\d{4}-\d{2}-\d{2})",
                      rec["agenda"] + rec["minutes"])
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except ValueError:
                pass
        return None

    @classmethod
    def _abs(cls, href: str) -> str:
        if not href or href.startswith("http"):
            return href
        return cls.ROOT + href

    def _notices(self) -> list[Doc]:
        """
        The county news feed carries meeting cancellations, budget hearing
        notices and public hearing notices. Cheap to poll and it is where
        schedule changes surface first.
        """
        try:
            text = html_text(get(self.ROOT + "/news").text)
        except Exception:
            return []
        # rolling, because this URL never changes but its contents do.
        return [Doc(self.body, "notice", "County news and public notices",
                    dt.date.today().isoformat(), self.ROOT + "/news", text,
                    rolling=True)]


# ------------------------------------------------------------------ city

class CityCouncil:
    """
    City of Brevard City Council. First and third Mondays, 5:30pm.

    The public portal at brevardnc.portal.civicclerk.com is a React app.
    The data behind it is served by an open OData API. Verified working
    on another CivicClerk tenant:

        https://{tenant}.api.civicclerk.com/v1/Meetings
            /GetMeetingFileStream(fileId=NNNN,plainText=false)

    Field names confirmed against the brevardnc tenant on 2026-08-05:
    eventName, startDateTime and publishedFiles[{fileId, name, type}] are
    all present and shaped as this parser expects. The file stream
    endpoint serves a real PDF. See _events() for the two things about
    the query that are not obvious.
    """

    TENANT = "brevardnc"
    API = f"https://{TENANT}.api.civicclerk.com/v1"
    body = "city"

    # The server caps a page at 15 no matter what $top asks for.
    #
    # This said four pages, on the measurement that a 105 day span
    # returned 42 events across three. That had no room in it at all. The
    # sort is descending, so the page cap truncates the oldest events,
    # which are precisely the past meetings that minutes attach to, and it
    # truncates them without a word. Re-measured on 2026-08-23 with the
    # widened minutes window: 59 events across four pages, one event short
    # of silently losing the far end of the window.
    #
    # Twelve is a backstop rather than a working limit, and the loop below
    # says so out loud if it ever reaches it.
    MAX_PAGES = 12

    # Agendas post about a week ahead, so the window has to reach past
    # today to catch them. Matches the lookahead the other adapters use.
    LOOKAHEAD_DAYS = 45

    def discover(self) -> dict:
        """One-time helper. Print this and adjust the parser if needed."""
        return get(f"{self.API}/Events?$top=3&$orderby=startDateTime desc").json()

    def _events(self, lookback_days: int) -> list[dict]:
        """
        Every event in the window, following the server's paging.

        Two things here are load bearing, both learned from the live
        tenant rather than from the API docs.

        The filter needs an upper bound. Without one it matches every
        meeting the city has ever put on the calendar, including
        placeholders out into 2027, and a descending sort puts those
        first.

        And the paging has to be followed. $top is capped at 15 server
        side and the rest comes back behind an @odata.nextLink. Those
        two together are fatal: a single unbounded request returns the
        fifteen furthest future events, which are exactly the ones with
        no agenda posted yet, so the adapter finds nothing and raises
        nothing. Measured before this fix, against the live tenant: 15
        events returned, all in 2027, zero already held, zero files.

        The page cap is the same trap one step further on. Descending
        order means running out of pages drops the oldest events, and
        minutes only ever attach to a past meeting, so a cap that fits
        the window exactly loses minutes and nothing else. Confirmed
        against the live tenant on 2026-08-23, which is why MAX_PAGES is
        now a backstop and exhausting it prints.
        """
        today = dt.date.today()
        since = (today - dt.timedelta(days=lookback_days)).isoformat()
        until = (today + dt.timedelta(days=self.LOOKAHEAD_DAYS)).isoformat()

        out: list[dict] = []
        url = f"{self.API}/Events"
        params = {
            "$filter": (f"startDateTime ge {since}T00:00:00Z and "
                        f"startDateTime le {until}T23:59:59Z"),
            "$orderby": "startDateTime desc",
            "$top": "100",
        }
        for page in range(self.MAX_PAGES):
            # nextLink is absolute and already carries the filter and a
            # skiptoken, so it must be fetched without params of our own.
            payload = get(url, params=params if page == 0 else None).json()
            out.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")
            if not url:
                break
        else:
            if url:
                print(f"city: stopped at {self.MAX_PAGES} pages with more "
                      f"events still behind a nextLink. The oldest part of "
                      f"the {since} to {until} window was not read, which is "
                      f"the part minutes live in. Raise MAX_PAGES.")
        return out

    def collect(self, lookback_days: int = 60) -> list[Doc]:
        today = dt.date.today()
        cutoff = (today - dt.timedelta(days=lookback_days)).isoformat()
        # Same split as County.collect. The query reaches back far enough
        # to cover the whole minutes window, and everything that is not
        # minutes is then held to the ordinary lookback, because a city
        # agenda packet is a quarter of a million characters and there is
        # no reason to re-read June's.
        try:
            events = self._events(max(lookback_days, MINUTES_LOOKBACK_DAYS))
        except Exception as e:
            # Was a bare return []. An adapter returning nothing because
            # the API refused it is the exact shape of the silent failure
            # this project keeps rediscovering.
            print(f"city events query failed, returning no documents: "
                  f"{type(e).__name__}: {e}")
            return []

        docs, scanned, unreachable = [], [], []
        for ev in events:
            name = ev.get("eventName") or ev.get("name") or "Meeting"
            # Deliberately loose, and it is a decision rather than an
            # accident. Brevard prefixes every council committee with the
            # full name, "City Council Finance and Human Resources
            # Committee" and so on, so this takes the committees too.
            #
            # Measured over a 60 day window: 7 documents from the full
            # council, 12 from committees and advisory boards. The
            # committees total about 7,300 characters, roughly 1,800
            # tokens, so the cost of keeping them is around half a cent a
            # cycle. They are also upstream of council decisions, which
            # is where showing up still changes something.
            #
            # The risk is conflation, not inclusion: a committee
            # recommendation must never read as a council action. The
            # synthesis prompt already forbids that, and the full event
            # name goes into Doc.title below so a record always says
            # which body actually met.
            #
            # Note this filter excludes Board of Adjustment, Brevard
            # Planning Board and Ecusta Trail Advisory Board, none of
            # which carry "council" in the name.
            if "council" not in name.lower():
                continue
            when = (ev.get("startDateTime") or "")[:10]
            for f in ev.get("publishedFiles", []) or []:
                fid = f.get("fileId") or f.get("id")
                if not fid:
                    continue
                # type is a clean enum on this tenant, Agenda, Agenda
                # Packet, Minutes, Notice, so prefer it. name is the
                # fallback for a tenant that does not populate it, and it
                # is the looser signal: an agenda whose title mentions
                # approving last month's minutes would misfile on name.
                label = (f.get("type") or f.get("name") or "").lower()
                kind = ("minutes" if "minute" in label
                        else "notice" if "notice" in label
                        else "agenda")
                # The widened query above is for minutes only.
                if kind != "minutes" and when < cutoff:
                    continue
                url = (f"{self.API}/Meetings/GetMeetingFileStream"
                       f"(fileId={fid},plainText=false)")
                try:
                    r = get(url)
                    text = (pdf_text(r.content)
                            if "pdf" in r.headers.get("content-type", "").lower()
                            else r.text)
                except Exception as e:
                    unreachable.append(f"{when} {kind} fileId={fid}: "
                                       f"{type(e).__name__}")
                    continue
                if not text.strip():
                    # The city publishes most of its minutes as a scan of
                    # the signed copy: a valid PDF whose pages carry an
                    # image and no font, so pdfplumber has nothing to
                    # extract and nor would anything else without OCR.
                    #
                    # This has to be said out loud, and used to be the
                    # opposite. A text-free Doc was appended anyway, and
                    # run.py then discarded it as a placeholder, meaning a
                    # meeting whose minutes were scanned looked identical
                    # to one whose minutes had not been published yet. The
                    # minutes window stayed open, the digest reported "no
                    # minutes retrieved" every cycle, and nothing anywhere
                    # said why. Measured against the live tenant on
                    # 2026-08-23: of the five most recent sets of council
                    # minutes, four were scans of exactly this kind and
                    # one, 2026-07-20, carried text.
                    scanned.append(f"{when} {f.get('type') or kind}")
                    continue
                # One meeting can publish an agenda, a packet and minutes.
                # Without the file label in the title they arrive as three
                # documents with identical names, which makes a record in
                # the brief impossible to trace back to the right one.
                title = f"{name} {when}: {f.get('type') or f.get('name') or kind}"
                docs.append(Doc(self.body, kind, title, when, url, text))

        if scanned:
            print(f"city: {len(scanned)} published file(s) are scanned images "
                  f"with no extractable text, skipped: "
                  f"{'; '.join(scanned[:4])}"
                  + (f"; and {len(scanned) - 4} more" if len(scanned) > 4 else ""))
        if unreachable:
            print(f"city: {len(unreachable)} file(s) could not be fetched: "
                  f"{'; '.join(unreachable[:3])}")
        return docs


# ------------------------------------------------------------- elections

class BoardOfElections:
    """
    Transylvania County Board of Elections.

    A different shape from the other three, in two ways that matter.

    It publishes minutes and nothing else. There is no agenda anywhere on
    the site and no recording of any kind, so minutes are not the slow
    confirmation of a meeting already covered by an agenda and a video,
    they are the only account that will ever exist. That makes them worth
    more here than county minutes are, and it is why `elections` is
    restricted to the minutes window in schedule.BODY_ARTIFACTS.

    And it meets on two rhythms rather than one. Monthly regular meetings
    for most of the year, then a burst from late September through the
    canvass: absentee meetings weekly, then daily, as measured off the
    board's own schedule on 2026-08-06, where the gaps between the
    thirteen listed meetings run 28, 19, 7, 7, 7, 7, 6, 1, 3, 6, 1, 27
    days. No recurrence rule describes that, which is why the board's own
    published schedule is the calendar source rather than a fallback.

    Access: the site serves no robots.txt at all, so nothing is
    disallowed. Checked 2026-08-06.
    """

    ROOT = "https://www.transylvaniaelections.org"
    MINUTES_INDEX = ROOT + "/Minutes.shtml"
    SCHEDULE_PDF = ROOT + "/pdfs/BoardMeetingSchedule.pdf"
    body = "elections"

    MONTHS = ("JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER "
              "OCTOBER NOVEMBER DECEMBER").split()

    _schedule_cache: list | None = None

    def collect(self, lookback_days: int = 90) -> list[Doc]:
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
        docs, scanned, missing = [], [], []

        for when, title, url in self.minutes_index():
            if when < cutoff:
                continue
            try:
                r = get(url)
            except Exception as e:
                # The listing is hand maintained and does not always
                # match the files. The 2025-11-04 entry links a file
                # named 20241104M.pdf, which 404s.
                missing.append(f"{when} {title}: {type(e).__name__}")
                continue
            if "pdf" not in r.headers.get("content-type", "").lower():
                missing.append(f"{when} {title}: not served as a PDF")
                continue
            text = pdf_text(r.content)
            if not text:
                # A valid PDF holding an image of a page. pdfplumber has
                # nothing to extract and nor would anything else without
                # OCR. Never silent: this is most of the body's record.
                scanned.append(f"{when} {title}")
                continue
            docs.append(Doc(self.body, "minutes",
                            f"Board of Elections minutes {when:%m/%d/%y}, {title}",
                            when.isoformat(), url, text))

        if scanned:
            print(f"elections: {len(scanned)} set(s) of minutes are scanned "
                  f"images with no extractable text, skipped: "
                  f"{'; '.join(scanned[:4])}"
                  + (f"; and {len(scanned) - 4} more" if len(scanned) > 4 else ""))
        if missing:
            print(f"elections: {len(missing)} set(s) could not be fetched: "
                  f"{'; '.join(missing[:3])}")
        return docs

    def minutes_index(self) -> list[tuple]:
        """
        (date, description, url) for every published set of minutes.

        The date comes from the link text rather than the filename, and
        the difference is not academic: the November 4 2025 entry links
        20241104M.pdf, a year out. The listing text is maintained by hand
        and sits under a year heading, so it is the one to trust, with
        the filename as the fallback when the text will not parse.
        """
        try:
            html = get(self.MINUTES_INDEX).text
        except Exception as e:
            print(f"elections minutes index unavailable: {e}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        out, seen = [], set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "/Minutes/" not in href or not href.lower().endswith(".pdf"):
                continue
            label = a.get_text(" ", strip=True)
            when = self._parse_label_date(label) or self._parse_href_date(href)
            if not when:
                continue
            url = href if href.startswith("http") else self.ROOT + href
            if url in seen:
                continue
            seen.add(url)
            # "May 14, 2026 (Regular Meeting)" to "Regular Meeting".
            m = re.search(r"\(([^)]+)\)", label)
            out.append((when, m.group(1) if m else "Board Meeting", url))
        return out

    def scheduled_meetings(self) -> list[tuple]:
        """
        (date, time, description) from the board's own schedule PDF.

        This is the calendar source for the body, not a supplement. The
        other three bodies keep a standing rhythm that a recurrence rule
        can approximate when everything else fails. This one does not,
        so if the PDF cannot be read there is nothing sensible to fall
        back on and the right answer is no meetings rather than invented
        ones.
        """
        if BoardOfElections._schedule_cache is not None:
            return BoardOfElections._schedule_cache
        try:
            r = get(self.SCHEDULE_PDF)
            text = pdf_text(r.content)
        except Exception as e:
            print(f"elections schedule pdf unavailable: {e}")
            return []

        pat = re.compile(
            rf"^({'|'.join(self.MONTHS)})\s+(\d{{1,2}}),?\s+(\d{{4}})\s+"
            r"(\d{1,2}:\d{2}\s*[AP]M)\s+(.+)$",
            re.I | re.M,
        )
        out = []
        for m in pat.finditer(text):
            month, day, year, at, rest = m.groups()
            # The "Important Dates" column bleeds onto the same line, so
            # cut the description at the first thing shaped like a date.
            desc = re.split(r"\s+\d{1,2}/\d{1,2}/\d{2}", rest)[0].strip()
            desc = re.sub(r"\s{2,}", " ", desc)
            try:
                when = dt.datetime.strptime(f"{month} {day} {year}",
                                            "%B %d %Y").date()
            except ValueError:
                continue
            out.append((when, at.strip(), desc))

        if not out:
            print("elections schedule pdf parsed to no meetings, format may have changed")
        BoardOfElections._schedule_cache = out
        return out

    @staticmethod
    def _parse_label_date(label: str):
        m = re.search(r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", label)
        if not m:
            return None
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return dt.datetime.strptime(m.group(1), fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_href_date(href: str):
        m = re.search(r"(\d{8})", href)
        if not m:
            return None
        try:
            return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            return None


# --------------------------------------------------------------- newspaper

class Newspaper:
    """
    The Transylvania Times covers meetings before minutes are approved,
    which closes the county's four-to-six week gap. Decoded for reading
    and summarizing only.
    """

    ROOT = "https://www.transylvaniatimes.com"
    body = "press"
    KEYWORDS = ("council", "commissioner", "school board",
                "board of education", "county")

    def collect(self, lookback_days: int = 21) -> list[Doc]:
        try:
            soup = BeautifulSoup(get(self.ROOT).text, "html.parser")
        except Exception:
            return []
        docs, seen = [], set()
        for a in soup.select('a[href*="/article_"]'):
            href, title = a.get("href", ""), a.get_text(strip=True)
            if not title or href in seen:
                continue
            if not any(k in title.lower() for k in self.KEYWORDS):
                continue
            seen.add(href)
            url = href if href.startswith("http") else self.ROOT + href
            try:
                raw = get(url).text
            except Exception:
                continue
            body = "\n".join(rot47(m) for m in re.findall(r"kAm(.*?)k\^Am", raw, re.S))
            docs.append(Doc(self.body, "notice", title,
                            dt.date.today().isoformat(), url,
                            html_text(body) if body else ""))
        return docs


ADAPTERS = [SchoolBoard(), County(), CityCouncil(), BoardOfElections(), Newspaper()]
