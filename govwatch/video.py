"""
Meeting video to transcript.

This is the piece that closes the county gap. Approved county minutes
land four to six weeks after a meeting, and they summarize public comment
thinly or not at all. The video exists the same day. So does everything
anyone actually said.

Pipeline per video:

    discover  ->  captions?  ->  yes: use them, done, free
                      |
                      no
                      |
                  download audio  ->  faster-whisper  ->  timestamped text
                                                              |
                                                       attribute speakers
                                                              |
                                                     Doc(kind="transcript")

Captions first is not a shortcut, it is the correct order. YouTube's
automatic captions are already machine generated from better audio than
we can get by re-encoding a stream, they cost nothing, and they arrive
within an hour of upload. Whisper is the fallback for sources that
publish no captions at all, which for us means the county.

Speaker attribution is done with Claude reading the transcript against
the roster and the agenda, not with acoustic diarization. These meetings
announce themselves: there is a roll call, the chair names each public
comment speaker before they approach, and staff introduce themselves
before presenting. Context beats voiceprints here, and pyannote would
add a model download, a Hugging Face token, and a licence acceptance
step for worse results. The tradeoff is that attribution is inference
and is labelled as such everywhere it appears.
"""

from __future__ import annotations

import os
import re
import json
import shutil
import hashlib
import subprocess
import datetime as dt
from pathlib import Path
from dataclasses import dataclass, field

import requests
import yaml

from govwatch.sources import Doc, get, UA

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())
VIDEO_CFG = CONFIG.get("video", {})

# The transcript is the durable artifact this project produces, so it
# lives in the repo as readable markdown rather than buried in a JSON
# cache. Two separate things with two separate homes:
#
#   transcripts/   the artifact. Committed, browsable on GitHub, greppable,
#                  diffs line by line, citable by URL.
#   state/video/   the bookkeeping. Which video ids we have already done,
#                  so nothing is transcribed twice.
#
# This changes no tokens. The model receives the same characters either
# way, and the API bills on what you send, not on how it was stored. What
# it buys is an archive a human can actually read, and git history that
# shows a paragraph changing instead of one 30,000 character line.
TRANSCRIPTS = Path(__file__).parent.parent / "transcripts"
TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
CACHE = Path(__file__).parent.parent / "state" / "video"
CACHE.mkdir(parents=True, exist_ok=True)
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "govwatch-media"
WORK.mkdir(parents=True, exist_ok=True)


@dataclass
class VideoRef:
    body: str
    title: str
    date: str            # ISO, best effort
    page_url: str
    media_url: str = ""  # direct audio or video, or a yt-dlp resolvable page
    vid: str = field(default="")

    def __post_init__(self):
        if not self.vid:
            self.vid = hashlib.sha1(
                (self.media_url or self.page_url).encode()
            ).hexdigest()[:16]


# ============================================================ discovery

class YouTubeSource:
    """
    School board and city council both publish full meetings to YouTube.

    School board: youtube.com/user/tcsnc, confirmed from the board's own
    agendas, which print the livestream link on every notice.
    City council: uploads titled "Brevard City Council Meeting | <date>",
    with the CivicClerk agenda packet link in the description.
    """

    def __init__(self, body: str, channel: str, title_filter: str,
                 rss_url: str = ""):
        self.body = body
        self.channel = channel
        self.rss_url = rss_url
        self.title_re = re.compile(title_filter, re.I)

    def discover(self, lookback_days: int) -> list[VideoRef]:
        """RSS first. It is near instant and costs one small request."""
        refs = self._from_rss(lookback_days)
        return refs if refs else self._from_ytdlp(lookback_days)

    def _from_rss(self, lookback_days: int) -> list[VideoRef]:
        """
        YouTube publishes a channel feed that updates within minutes of an
        upload:

            https://www.youtube.com/feeds/videos.xml?channel_id=UC...
            https://www.youtube.com/feeds/videos.xml?user=tcsnc

        This is what makes "as soon as it is available" real for the two
        bodies that use YouTube. yt-dlp is the fallback, because it is
        slower, heavier, and more likely to be rate limited.
        """
        feed = self.rss_url
        if not feed:
            return []
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
        try:
            xml = get(feed).text
        except Exception:
            return []

        out = []
        for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
            title = _tag(entry, "title")
            if not self.title_re.search(title):
                continue
            vid = _tag(entry, "yt:videoId")
            published = _tag(entry, "published")[:10]
            when = _date_from_title(title)
            if not when and published:
                try:
                    when = dt.date.fromisoformat(published)
                except ValueError:
                    when = None
            if when and when < cutoff:
                continue
            url = f"https://www.youtube.com/watch?v={vid}"
            out.append(VideoRef(self.body, title,
                                when.isoformat() if when else "", url, url))
        return out

    def _from_ytdlp(self, lookback_days: int) -> list[VideoRef]:
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
        raw = _ytdlp_json(
            f"{self.channel}/videos",
            ["--flat-playlist", "--playlist-end", "40"],
        )
        out = []
        for entry in raw:
            title = entry.get("title", "")
            if not self.title_re.search(title):
                continue
            when = _date_from_title(title) or _epoch_date(entry.get("timestamp"))
            if when and when < cutoff:
                continue
            url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
            out.append(VideoRef(self.body, title,
                                when.isoformat() if when else "", url, url))
        return out


class GranicusSource:
    """
    Transylvania County. The tenant is transylvaniacounty.granicus.com.

    Granicus has shipped two generations of public archive. The legacy one
    exposes an RSS feed per view, which is the cleanest entry point we
    have anywhere in this project, since RSS is published expressly for
    machine consumption:

        https://{tenant}.granicus.com/ViewPublisherRSS.php?view_id=N

    Each item links a player page carrying a clip_id, and the media
    itself is a plain file:

        https://archive-media.granicus.com/OnDemand/{tenant}/{tenant}_{clip}.mp4

    The county appears to be on the newer boards interface, where those
    endpoints may not exist. I could not verify which generation is live,
    so probe() checks both and tells you what responds. Run it once
    before trusting this adapter, and set video.granicus.view_id in
    config.yml to whatever it finds.

    Note on access: the county's Granicus HTML is disallowed by
    robots.txt. RSS feeds and media files generally are not, but check
    the current robots.txt and the county's terms before running this at
    any volume, and leave the rate limit alone. One request per meeting
    per week is a rounding error on their bandwidth. Do not make it more
    than that.
    """

    TENANT = "transylvaniacounty"
    HOST = f"https://{TENANT}.granicus.com"
    MEDIA = f"https://archive-media.granicus.com/OnDemand/{TENANT}"

    body = "county"

    def probe(self) -> dict:
        """One-time setup helper. Reports which endpoints answer."""
        results = {}
        for vid in range(1, 9):
            url = f"{self.HOST}/ViewPublisherRSS.php?view_id={vid}"
            try:
                r = requests.get(url, headers=UA, timeout=20)
                ok = r.status_code == 200 and "<item" in r.text
                results[f"rss view_id={vid}"] = {
                    "status": r.status_code,
                    "items": r.text.count("<item"),
                    "sample": re.findall(r"<title>(.*?)</title>", r.text)[:3],
                } if ok else {"status": r.status_code, "items": 0}
            except Exception as e:
                results[f"rss view_id={vid}"] = {"error": str(e)[:120]}

        for path in ("/services/NetworkAPI", "/api/v1/events", "/boards"):
            try:
                r = requests.get(self.HOST + path, headers=UA, timeout=20)
                results[path] = {"status": r.status_code,
                                 "type": r.headers.get("content-type", "")}
            except Exception as e:
                results[path] = {"error": str(e)[:120]}
        return results

    def discover(self, lookback_days: int) -> list[VideoRef]:
        view_id = VIDEO_CFG.get("granicus", {}).get("view_id")
        if not view_id:
            return []
        cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
        try:
            xml = get(f"{self.HOST}/ViewPublisherRSS.php?view_id={view_id}").text
        except Exception:
            return []

        out = []
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            title = _tag(item, "title")
            link = _tag(item, "link")
            when = _date_from_title(title) or _rfc822(_tag(item, "pubDate"))
            if when and when < cutoff:
                continue
            if not re.search(r"commission", title, re.I):
                continue
            clip = re.search(r"clip_id=(\d+)", link + item)
            media = f"{self.MEDIA}/{self.TENANT}_{clip.group(1)}.mp4" if clip else ""
            out.append(VideoRef(self.body, title,
                                when.isoformat() if when else "", link, media or link))
        return out


class VimeoSource:
    """
    Transylvania County. The county streams each meeting to its own Vimeo
    account and links the recording from its meetings index, one Vimeo
    event per meeting.

    Three hops, none of which need credentials:

        vimeo.com/event/N            linked from the county index
        vimeo.com/event/N/embed      carries the player iframe
        player.vimeo.com/video/M/config

    That last one returns the caption track and the HLS streams.

    Hand rolled rather than shelled out to yt-dlp on purpose. Vimeo has
    withdrawn anonymous extraction and yt-dlp's extractor now refuses
    without a login, tested 2026-08-06 on the nightly build. The player
    config endpoint is a different path and still answers anonymously,
    which is the whole reason county video is reachable at all.

    Verified against two meetings on 2026-08-06. Both carried an
    auto-generated English track: 130,338 and 125,878 characters, each
    opening with a roll call naming every commissioner present, which is
    exactly what extraction needs to attribute speakers.
    """

    body = "county"
    EMBED = "https://vimeo.com/event/{}/embed"

    def discover(self, lookback_days: int) -> list[VideoRef]:
        from govwatch.sources import County

        today = dt.date.today()
        cutoff = today - dt.timedelta(days=lookback_days)
        out = []
        for row in County().rows():
            url, when = row.get("video"), row.get("date")
            if not url or "vimeo.com" not in url or not when or when < cutoff:
                continue
            # A meeting that has not happened yet has no recording, and
            # the county publishes the event link ahead of time, so the
            # row exists well before the video does.
            #
            # This was latent until the row dates got fixed. The
            # 2026-08-24 agenda is filed under a 2024 filename, so that
            # row used to be dated two years early and fell out of the
            # lookback at the other end. Reading the date from the row
            # text instead put it back in range, and the first county
            # transcribe after that tried to fetch tomorrow's meeting and
            # failed with "no caption track and no HLS stream", which
            # reads exactly like a missing recording rather than one that
            # does not exist yet.
            if when >= today:
                continue
            title = f"Board of Commissioners {when:%B} {when.day}, {when.year}"
            out.append(VideoRef(self.body, title, when.isoformat(), url, url))
        return out


def _vimeo_config(event_url: str) -> dict | None:
    """Event URL to the player config, the JSON the player itself reads."""
    m = re.search(r"/event/(\d+)", event_url)
    if not m:
        return None
    try:
        html = get(VimeoSource.EMBED.format(m.group(1))).text
    except Exception as e:
        print(f"vimeo embed page unavailable: {e}")
        return None
    vid = (re.search(r"player\.vimeo\.com/video/(\d+)", html)
           or re.search(r"/video/(\d+)/config", html))
    if not vid:
        print(f"no player id on the vimeo embed page for {event_url}")
        return None
    try:
        return get(f"https://player.vimeo.com/video/{vid.group(1)}/config").json()
    except Exception as e:
        print(f"vimeo player config unavailable: {e}")
        return None


def _vimeo_captions(ref: VideoRef) -> str | None:
    cfg = _vimeo_config(ref.media_url)
    if not cfg:
        return None
    tracks = (cfg.get("request", {}) or {}).get("text_tracks") or []
    if not tracks:
        print(f"no caption track on {ref.title}, falling back to whisper")
        return None
    url = tracks[0].get("url", "")
    if url.startswith("/"):
        url = "https://player.vimeo.com" + url
    try:
        return _vtt_to_text(get(url).text)
    except Exception as e:
        print(f"vimeo caption fetch failed: {e}")
        return None


def _vimeo_hls(event_url: str) -> str:
    """
    Master playlist url, for the Whisper fallback.

    Vimeo serves no progressive mp4 for these, only HLS and DASH, so
    there is no single file to stream. ffmpeg reads HLS directly, which
    is why the fallback goes through it rather than through a download.
    """
    cfg = _vimeo_config(event_url)
    if not cfg:
        return ""
    cdns = ((((cfg.get("request", {}) or {}).get("files", {}) or {})
             .get("hls", {}) or {}).get("cdns", {}) or {})
    for cdn in cdns.values():
        if cdn.get("url"):
            return cdn["url"]
    return ""


def sources() -> list:
    cfg = VIDEO_CFG
    out = []
    if cfg.get("vimeo", {}).get("enabled", True):
        out.append(VimeoSource())
    if cfg.get("granicus", {}).get("enabled", False):
        out.append(GranicusSource())
    for yt in cfg.get("youtube", []):
        out.append(YouTubeSource(yt["body"], yt["channel"], yt["title_filter"],
                                 yt.get("rss_url", "")))
    return out


# ============================================================== captions

# Which yt-dlp client asks for the video decides whether a caption track
# is reachable at all, and the defaults are wrong for one of our two
# YouTube bodies.
#
# Transylvania County Schools marks its uploads "made for kids". The
# lightweight clients yt-dlp reaches for first, visionos and android_vr,
# refuse those outright with "This video is not available". That, not the
# proof of origin token, is why the school board has never produced a
# transcript while the city has produced three.
#
# Verified against the live videos on 2026-08-23, from a residential
# connection: the 2026-08-17 and 2026-06-15 board meetings both fail on
# the default clients and both return a full English caption track on
# tv_simply or web_embedded. The city is unaffected by the addition,
# since default stays first in the list and still wins for its videos.
YT_CLIENT_ARGS = ["--extractor-args",
                  "youtube:player_client=default,tv_simply,web_embedded"]

# Without this, yt-dlp aborts with "Requested format is not available"
# before it writes the subtitle file, whenever no downloadable A/V format
# resolves. We are passing --skip-download and only ever want the
# captions, so a missing media format is not a reason to give up on them.
YT_SUBS_ONLY_ARGS = ["--ignore-no-formats-error"]

# yt-dlp's wording when YouTube refuses the request outright rather than
# simply having no captions. Both phrasings mean the same thing in
# practice, that this address is being challenged, and neither is
# recoverable by trying harder from the same place.
BLOCKED_RE = re.compile(
    r"confirm you.{0,3}re not a bot|Sign in to confirm|"
    r"not a bot|blocked it in your country|age.restricted",
    re.I)

# The last caption failure per video id, so the reason survives long
# enough to reach failures.json and then the brief. Printing it to the
# job log was not enough: the 2026-08-23 runner runs printed the real
# cause and recorded a misleading one.
LAST_CAPTION_ERROR: dict[str, str] = {}


def fetch_captions(ref: VideoRef) -> str | None:
    """
    Try published captions before spending CPU on Whisper.

    Order matters: a human-corrected caption track beats automatic
    captions, which beat anything we generate locally.
    """
    if "vimeo.com" in ref.media_url:
        return _vimeo_captions(ref)

    if "youtube.com" not in ref.media_url and "youtu.be" not in ref.media_url:
        return _granicus_captions(ref)

    dest = WORK / ref.vid
    dest.mkdir(exist_ok=True)
    for args in (["--write-subs", "--sub-langs", "en.*"],
                 ["--write-auto-subs", "--sub-langs", "en.*"]):
        proc = subprocess.run(
            ["yt-dlp", "--skip-download", "--sub-format", "vtt",
             *YT_CLIENT_ARGS, *YT_SUBS_ONLY_ARGS,
             *args, "-o", str(dest / "cap"), ref.media_url],
            capture_output=True, text=True, timeout=300,
        )
        vtts = list(dest.glob("*.vtt"))
        if vtts:
            return _vtt_to_text(vtts[0].read_text(errors="ignore"))
        if proc.returncode != 0:
            # yt-dlp's own message is the only thing that separates "this
            # video has no caption track", which is normal and means fall
            # through to Whisper, from "YouTube refused the request",
            # which is not normal and Whisper cannot rescue because the
            # audio download uses the same blocked client. Throwing the
            # stderr away made those two look identical.
            tail = " ".join((proc.stderr or "").strip().splitlines()[-2:])
            print(f"yt-dlp captions failed for {ref.title}: {tail[:400]}")
            LAST_CAPTION_ERROR[ref.vid] = tail[:400]
            # A refusal is not a missing caption track, and only one of
            # the two is worth falling through to Whisper for. Whisper
            # downloads audio through the same client that was just
            # refused, so on a blocked request it re-fails several
            # minutes and several hundred megabytes later, and the error
            # that reaches failures.json is "audio download failed",
            # which points at the wrong thing entirely. That is exactly
            # what the 2026-08-23 runs recorded for both YouTube bodies.
            if BLOCKED_RE.search(tail):
                raise RuntimeError(
                    f"YouTube refused the caption request: {tail[:200]}. "
                    f"Whisper cannot rescue this, it downloads through the "
                    f"same client, so nothing was attempted.")
    return None


def _granicus_captions(ref: VideoRef) -> str | None:
    """Granicus sometimes publishes a caption sidecar next to the media."""
    if not ref.media_url.endswith(".mp4"):
        return None
    for ext in (".vtt", ".srt", ".txt"):
        try:
            r = requests.get(ref.media_url[:-4] + ext, headers=UA, timeout=30)
            if r.status_code == 200 and len(r.text) > 500:
                return _vtt_to_text(r.text)
        except Exception:
            continue
    return None


def _vtt_to_text(vtt: str) -> str:
    """Collapse WebVTT or SRT into timestamped paragraphs."""
    lines, current, stamp = [], [], None
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line in ("WEBVTT",) or line.isdigit():
            continue
        # YouTube's auto-caption files open with "Kind: captions" and
        # "Language: en". They are file metadata, not speech, and they
        # were landing in the first paragraph of every YouTube
        # transcript in the archive.
        if re.match(r"(Kind|Language|NOTE|STYLE|REGION):", line):
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2})[.,]\d+\s*-->", line)
        if m:
            if stamp is None:
                stamp = m.group(1)
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if current and line == current[-1]:
            continue          # auto-captions repeat each line as it builds
        current.append(line)
        if len(" ".join(current)) > 700:
            lines.append(f"[{stamp}] " + " ".join(current))
            current, stamp = [], None
    if current:
        lines.append(f"[{stamp or '00:00:00'}] " + " ".join(current))
    return "\n\n".join(lines)


# =========================================================== whisper

def transcribe_audio(ref: VideoRef) -> str:
    """
    Fallback path. Download audio only, then run faster-whisper on CPU.

    Settings chosen for meeting audio specifically:

      vad_filter                 Meetings have long dead air: recesses,
                                 people walking to the podium, closed
                                 session gaps. Whisper hallucinates
                                 confidently into silence, usually by
                                 repeating the last sentence or emitting
                                 a stray "thank you". VAD removes the
                                 silence before it can.
      condition_on_previous_text Off. When it does derail, this is what
                                 makes it stay derailed for minutes.
      beam_size 5, int8          Reasonable accuracy at CPU speed.
      model base.en              About four to eight times realtime on a
                                 GitHub runner, so a three hour meeting
                                 lands in twenty to forty five minutes.
                                 small.en is noticeably better on gavel
                                 bangs, crosstalk and room echo, and
                                 roughly halves the speed. Switch if the
                                 output disappoints you.
    """
    from faster_whisper import WhisperModel

    audio = _download_audio(ref)
    model = WhisperModel(
        VIDEO_CFG.get("whisper_model", "base.en"),
        device="cpu",
        compute_type="int8",
        download_root=str(CACHE / "models"),
    )
    segments, _ = model.transcribe(
        str(audio),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1000},
        condition_on_previous_text=False,
        language="en",
    )

    out, buf, start = [], [], None
    for seg in segments:
        if start is None:
            start = seg.start
        buf.append(seg.text.strip())
        if len(" ".join(buf)) > 700:
            out.append(f"[{_hms(start)}] " + " ".join(buf))
            buf, start = [], None
    if buf:
        out.append(f"[{_hms(start or 0)}] " + " ".join(buf))

    audio.unlink(missing_ok=True)
    return "\n\n".join(out)


def _download_audio(ref: VideoRef) -> Path:
    dest = WORK / f"{ref.vid}.m4a"
    if dest.exists():
        return dest

    if "vimeo.com" in ref.media_url:
        # Only reached when a meeting has no caption track. ffmpeg pulls
        # the HLS stream and strips it to the mono 16k Whisper wants, so
        # nothing lands on disk except the audio.
        hls = _vimeo_hls(ref.media_url)
        if not hls:
            raise RuntimeError(f"no caption track and no HLS stream for {ref.title}")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", hls,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", str(dest)],
            check=True, timeout=3600,
        )
        return dest

    if ref.media_url.endswith(".mp4"):
        # Direct file. Stream it and strip to mono 16k audio, which is
        # all Whisper wants and a fraction of the bytes to hold on disk.
        tmp = WORK / f"{ref.vid}.src.mp4"
        with requests.get(ref.media_url, headers=UA, stream=True, timeout=900) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(tmp),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", str(dest)],
            check=True, timeout=3600,
        )
        tmp.unlink(missing_ok=True)
    else:
        proc = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "m4a",
             *YT_CLIENT_ARGS,
             "--postprocessor-args", "-ac 1 -ar 16000",
             "-o", str(WORK / f"{ref.vid}.%(ext)s"), ref.media_url],
            capture_output=True, text=True, timeout=3600,
        )
        if proc.returncode != 0:
            # Raise with what yt-dlp actually said. check=True alone
            # reports only the exit status, which tells you nothing about
            # whether the video is private, the extractor is broken, or
            # YouTube is refusing this IP.
            tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
            raise RuntimeError(f"yt-dlp audio download failed: {tail[:500]}")
    return dest


# NOTE: a standalone attribute() pass used to live here. It was removed
# once speaker identification moved into brief.extract, which already
# reads the full transcript. Keeping both meant every transcript was read
# twice for no gain in accuracy. The prompt logic survives in
# brief.TRANSCRIPT_EXTRACT_SYSTEM.


# ============================================================== driver

def process(ref: VideoRef, roster: list[str], agenda: str = "") -> Doc | None:
    """
    Captions or Whisper, then a Doc for the pipeline.

    Speaker identification used to happen here, as its own pass over the
    transcript. It now happens inside extraction instead, because both
    steps needed to read the whole transcript and running them
    separately meant paying for those tokens twice. See brief.extract.
    """
    pointer = CACHE / f"{ref.vid}.json"
    if pointer.exists():
        meta = json.loads(pointer.read_text())
        md = Path(meta["path"])
        if md.exists():
            return _doc_from_markdown(ref, md)

    text = fetch_captions(ref)
    method = ("vimeo auto-generated captions" if "vimeo.com" in ref.media_url
              else "published captions")
    if not text or len(text) < 2000:
        text = transcribe_audio(ref)
        method = f"whisper {VIDEO_CFG.get('whisper_model', 'base.en')}"
    if not text or len(text) < 2000:
        return None

    md = _write_markdown(ref, text, method)
    pointer.write_text(json.dumps({
        "path": str(md),
        "method": method,
        "source": ref.page_url,
        "transcribed": dt.date.today().isoformat(),
    }, indent=2))
    return _doc_from_markdown(ref, md)


def _write_markdown(ref: VideoRef, text: str, method: str) -> Path:
    """
    One meeting, one markdown file, with frontmatter so it stays machine
    readable and a body a person can skim.

    Timestamps are kept on every paragraph. They are what makes the
    "verify before you quote anyone" instruction actionable: with a
    timestamp, checking a line against the video takes seconds.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", ref.title.lower()).strip("-")[:60]
    date = ref.date or dt.date.today().isoformat()
    path = TRANSCRIPTS / f"{date}-{ref.body}-{slug}.md"

    path.write_text(
        "---\n"
        f"body: {ref.body}\n"
        f"meeting_date: {date}\n"
        f"title: {ref.title}\n"
        f"source: {ref.page_url}\n"
        f"method: {method}\n"
        f"transcribed: {dt.date.today().isoformat()}\n"
        "speaker_labels: none. speakers are identified at extraction time,\n"
        "  from what was said aloud, not by voice matching.\n"
        "---\n\n"
        f"# {ref.title}\n\n"
        "> This is an unofficial transcript produced by automated speech\n"
        "> recognition or published captions. It is not the official record.\n"
        "> Verify against the source video before quoting anyone. Every\n"
        "> paragraph carries a timestamp so that check is quick.\n\n"
        f"{text}\n",
        encoding="utf-8",
    )
    return path


def _doc_from_markdown(ref: VideoRef, path: Path) -> Doc:
    """
    Feed the pipeline the same text either way.

    Frontmatter is stripped before the model sees it. It is bookkeeping
    for humans and tooling, and paying to send it to Haiku on every
    extraction would be a small ongoing waste for no benefit.
    """
    raw = path.read_text(encoding="utf-8")
    body = raw.split("---", 2)[-1].lstrip() if raw.startswith("---") else raw
    return Doc(
        body=ref.body,
        kind="transcript",
        title=f"{ref.title} (transcript)",
        meeting_date=ref.date,
        url=ref.page_url,
        text=body,
    )


# What the last collect() attempted and what went wrong, so a caller can
# persist it. Printing to the job log was the only record a video failure
# left, which is why the city path could fail on four consecutive runs
# and produce nothing but a brief saying "no video was retrieved". Prefer
# a loud failure to a tidy empty result.
LAST_ATTEMPTED: set[str] = set()
LAST_FAILURES: dict[str, str] = {}


def collect(lookback_days: int, roster: list[str],
            agendas: dict[str, str] | None = None,
            only_bodies: set[str] | None = None) -> list[Doc]:
    """
    only_bodies restricts work to bodies with an open video window. A
    meeting that happened yesterday is worth checking every two hours. A
    body that has not met in three weeks is not worth checking at all.
    """
    agendas = agendas or {}
    docs = []
    LAST_ATTEMPTED.clear()
    LAST_FAILURES.clear()
    for src in sources():
        body = getattr(src, "body", str(src))
        if only_bodies and body not in only_bodies:
            continue
        LAST_ATTEMPTED.add(body)
        try:
            refs = src.discover(lookback_days)
        except Exception as e:
            msg = f"discovery failed: {type(e).__name__}: {e}"
            print(f"video discovery failed for {body}: {e}")
            LAST_FAILURES[body] = msg[:300]
            continue
        for ref in refs[: VIDEO_CFG.get("max_per_run", 4)]:
            try:
                doc = process(ref, roster, agendas.get(ref.date, ""))
            except Exception as e:
                msg = f"{ref.title}: {type(e).__name__}: {e}"
                # When Whisper is what failed, the caption attempt that
                # preceded it is usually the more informative half, and
                # it is the half that used to exist only in the job log.
                why = LAST_CAPTION_ERROR.get(ref.vid)
                if why and why not in msg:
                    msg = f"{msg} (captions first failed with: {why})"
                print(f"transcription failed for {ref.title}: {e}")
                LAST_FAILURES[body] = msg[:300]
                continue
            if doc:
                docs.append(doc)
                print(f"transcribed {ref.title}")
            else:
                msg = (f"{ref.title}: no captions and no usable Whisper "
                       f"output, nothing written")
                print(msg)
                LAST_FAILURES[body] = msg[:300]
    return docs


# ============================================================== helpers

def _ytdlp_json(url: str, extra: list[str]) -> list[dict]:
    if not shutil.which("yt-dlp"):
        return []
    proc = subprocess.run(
        ["yt-dlp", "-J", *extra, url],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("entries", []) or []


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _date_from_title(title: str):
    for pat, fmt in (
        (r"([A-Z][a-z]+ \d{1,2},? \d{4})", "%B %d %Y"),
        # The school board titles every meeting "TCS Board Meeting
        # MM-DD-YYYY". Neither pattern below it matched that, so every
        # school board video fell back to its YouTube publish date, which
        # is the morning after the meeting. The transcript would have
        # been filed and dated one day late.
        (r"(\d{4}-\d{2}-\d{2})", "%Y-%m-%d"),
        (r"(\d{1,2}-\d{1,2}-\d{4})", "%m-%d-%Y"),
        (r"(\d{1,2}/\d{1,2}/\d{2,4})", "%m/%d/%Y"),
    ):
        m = re.search(pat, title)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        if fmt == "%m/%d/%Y" and len(raw.split("/")[-1]) == 2:
            raw = raw[:-2] + "20" + raw[-2:]
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _rfc822(s: str):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _epoch_date(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts)).date()
    except (TypeError, ValueError):
        return None


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
