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
    uid: str = field(default="")

    def __post_init__(self):
        if not self.uid:
            self.uid = hashlib.sha1(
                f"{self.body}|{self.kind}|{self.url}".encode()
            ).hexdigest()[:16]

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
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        for a in soup.select('a[href^="/node/"]'):
            m = re.match(r"/node/(\d+)$", a.get("href", ""))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            row = a.find_parent("tr")
            date_txt = row.get_text(" ") if row else a.get_text(" ")
            yield m.group(1), a.get_text(strip=True), self._parse_date(date_txt)

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

    Meeting-day URL slugs use MMDDYY. Minutes PDFs use YYYY-MM-DD with a
    suffix that varies by meeting type, so we try the known variants.
    """

    ROOT = "https://www.transylvaniacounty.org"
    MIN_DIR = ROOT + "/sites/default/files/departments/administration/minutes"
    body = "county"

    MINUTE_SUFFIXES = [
        "reg mtg", "Regular Meeting", "Budget Workshop",
        "Special Meeting", "work session",
    ]

    def collect(self, lookback_days: int = 90) -> list[Doc]:
        docs = []
        for d in self._meeting_dates(lookback_days):
            docs.extend(self._agenda(d))
            docs.extend(self._minutes(d))
        docs.extend(self._notices())
        return docs

    def _meeting_dates(self, lookback_days: int) -> list[dt.date]:
        """Second and fourth Mondays inside the window, plus a lookahead."""
        today = dt.date.today()
        start = today - dt.timedelta(days=lookback_days)
        end = today + dt.timedelta(days=45)
        out, d = [], start
        while d <= end:
            if d.weekday() == 0:
                nth = (d.day - 1) // 7 + 1
                if nth in (2, 4):
                    out.append(d)
            d += dt.timedelta(days=1)
        return out

    def _agenda(self, d: dt.date) -> list[Doc]:
        slug = d.strftime("%m%d%y")
        url = f"{self.ROOT}/meetings/commissioners-meeting-{slug}"
        try:
            text = html_text(get(url).text)
        except Exception:
            return []
        if "Call to Order" not in text and "CALL TO ORDER" not in text:
            return []
        return [Doc(self.body, "agenda", f"Commissioners agenda {d:%m/%d/%y}",
                    d.isoformat(), url, text)]

    def _minutes(self, d: dt.date) -> list[Doc]:
        for suffix in self.MINUTE_SUFFIXES:
            url = f"{self.MIN_DIR}/{d:%Y-%m-%d} {suffix}.pdf".replace(" ", "%20")
            try:
                r = get(url)
            except Exception:
                continue
            if "pdf" not in r.headers.get("content-type", "").lower():
                continue
            return [Doc(self.body, "minutes", f"Commissioners minutes {d:%m/%d/%y}",
                        d.isoformat(), url, pdf_text(r.content))]
        return []

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
        return [Doc(self.body, "notice", "County news and public notices",
                    dt.date.today().isoformat(), self.ROOT + "/news", text)]


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
