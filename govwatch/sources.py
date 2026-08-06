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
        docs = []
        today = dt.date.today()
        cutoff = today - dt.timedelta(days=lookback_days)

        for row in self.rows():
            when = row["date"]
            if not when or when < cutoff:
                continue
            for kind in ("agenda", "minutes"):
                url = row.get(kind)
                if not url:
                    continue
                try:
                    r = get(url)
                except Exception:
                    continue
                text = (pdf_text(r.content)
                        if "pdf" in r.headers.get("content-type", "").lower()
                        else html_text(r.text))
                if not text:
                    continue
                docs.append(Doc(self.body, kind,
                                f"Commissioners {kind} {when:%m/%d/%y}",
                                when.isoformat(), url, text))
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
        Filename first, row text second.

        The PDF filenames carry an unambiguous YYYY-MM-DD. The row text
        carries MM/DD/YY, which is the only date a row with a video but
        no documents yet will have.
        """
        m = re.search(r"/(?:agendas|minutes)/(\d{4}-\d{2}-\d{2})",
                      rec["agenda"] + rec["minutes"])
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except ValueError:
                pass
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", row.get_text(" ", strip=True))
        if m:
            try:
                return dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
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

    # The server caps a page at 15 no matter what $top asks for. Four
    # pages covers the window with room to spare: the live tenant returns
    # 42 events across three for a 105 day span.
    MAX_PAGES = 4

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
        return out

    def collect(self, lookback_days: int = 60) -> list[Doc]:
        try:
            events = self._events(lookback_days)
        except Exception:
            return []

        docs = []
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
                url = (f"{self.API}/Meetings/GetMeetingFileStream"
                       f"(fileId={fid},plainText=false)")
                try:
                    r = get(url)
                    text = (pdf_text(r.content)
                            if "pdf" in r.headers.get("content-type", "").lower()
                            else r.text)
                except Exception:
                    continue
                # One meeting can publish an agenda, a packet and minutes.
                # Without the file label in the title they arrive as three
                # documents with identical names, which makes a record in
                # the brief impossible to trace back to the right one.
                title = f"{name} {when}: {f.get('type') or f.get('name') or kind}"
                docs.append(Doc(self.body, kind, title, when, url, text))
        return docs


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


ADAPTERS = [SchoolBoard(), County(), CityCouncil(), Newspaper()]
