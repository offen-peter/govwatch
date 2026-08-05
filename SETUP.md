# GovWatch Setup Guide

Follow these in order. Each step depends on the one before it, and a
couple of them (probe, discover) exist specifically to stop you from
relying on a guess about a system nobody could inspect directly.

Budget about 45 minutes, most of it waiting on the first Whisper run.

---

## 0. Prerequisites

- A GitHub account with permission to create a repository and add
  repository secrets.
- An Anthropic API key with billing enabled. platform.claude.com.
- A Gmail account to send from, with an
  [app password](https://myaccount.google.com/apppasswords) generated
  (not your normal Gmail password: this requires 2-Step Verification to
  be on first).
- Python 3.12 locally, only for the verification steps below. The
  workflow itself needs nothing installed on your machine.

---

## 1. Create the repository and push the files

```bash
cd govwatch
git init
git add .
git commit -m "initial govwatch scaffold"
```

Create an empty repository on GitHub (no README, no .gitignore, you
already have both), then:

```bash
git remote add origin https://github.com/YOUR-ORG/govwatch.git
git branch -M main
git push -u origin main
```

**Public or private is a real decision here, not a formality.** A public
repo gets unlimited free Actions minutes. A private one gets 2,000 free
minutes a month, which this project stays well under, but the free tier
is shared with anything else running in the same account, including the
social media pipeline. There is also a substantive case for public: the
transcripts this produces are civic data, and making the archive
browsable is a public good in its own right, not just a cost saver.
Decide, then move on.

---

## 2. Add repository secrets

GitHub repo → **Settings → Secrets and variables → Actions → New
repository secret**. Add three:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from platform.claude.com |
| `GMAIL_USER` | the sending address |
| `GMAIL_APP_PASSWORD` | the 16-character app password, not your login password |

---

## 3. Point the pipeline at your meeting calendar

Open `config.yml` and set:

```yaml
schedule:
  shared_js_url: https://YOUR-SITE.netlify.app/tcdp-shared.js
```

This should be the same hosted file the events widget already reads.
Government meetings are not in Mobilize, you confirmed that while
building the widget, so `tcdp-shared.js` is the authority here as well.

If that file does not yet expose a plain array of meetings, publish a
small sibling next to it in this shape, and update `array_names` in
`config.yml` if you name it something else:

```js
const govMeetings = [
  {"body": "county",  "date": "2026-08-24", "title": "Board of Commissioners, 6pm"},
  {"body": "county",  "date": "2026-07-27", "cancelled": true},
  {"body": "schools", "date": "2026-08-17", "title": "Board of Education"},
  {"body": "city",    "date": "2026-08-03", "title": "City Council"}
];
```

Cancelling a meeting is then one line, and both the website and this
pipeline pick it up.

**Verify before moving on:**

```bash
pip install -r requirements.txt
python run.py schedule
```

This prints every meeting the pipeline currently knows about, which
source supplied each one, and which artifact windows are open today. If
`shared_js_url` is unreachable or the parser cannot find the array, this
command tells you so instead of failing silently later. Do not proceed
until this looks right, since every other step depends on it.

---

## 4. Set your email address

In `config.yml`:

```yaml
email:
  to:
    - you@example.org
```

---

## 5. Resolve the county's Granicus endpoint

The county's document portal blocks automated access, but Granicus
usually exposes an open RSS feed for the same archive. I could not
verify which endpoint is live for this tenant, so the adapter is a
deliberate no-op until you confirm it.

```bash
python run.py probe
```

This checks several endpoint patterns and reports what responds. Look
for an entry like:

```
"rss view_id=2": {"status": 200, "items": 24, "sample": [...meeting titles...]}
```

Take the `view_id` that returns real meeting titles and set it in
`config.yml`:

```yaml
video:
  granicus:
    view_id: 2
```

If nothing responds, the county has likely moved fully to the newer
Granicus boards interface with no public feed. In that case leave
`view_id` blank. The county video adapter will do nothing rather than
silently guess, and county coverage falls back to agendas plus press
until you find another way in. Note this in the digest's Gaps section by
adding a line to `known_gaps` in `config.yml`.

---

## 6. Resolve the YouTube channel IDs

The school board's feed URL is already filled in. Confirm it and fill in
the city's:

```bash
pip install yt-dlp
yt-dlp --print channel_id https://www.youtube.com/user/tcsnc
yt-dlp --print channel_id https://www.youtube.com/@CityofBrevardNC
```

Each prints a `UC...` string. Set the feed URLs in `config.yml`:

```yaml
youtube:
  - body: schools
    rss_url: https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxxxxxx
  - body: city
    rss_url: https://www.youtube.com/feeds/videos.xml?channel_id=UCyyyyyyyy
```

This feed is what makes same-day video detection work. It updates within
minutes of an upload.

---

## 7. Check the roster

`config.yml` under `video.roster` lists the people this pipeline knows
to expect in a transcript. It exists only to correct names speech
recognition mangles, spelling `McCall` correctly when Whisper hears
something close. It can never cause a name to be invented that the
transcript did not actually say. Update it when someone changes seats,
add anyone else you expect to hear from regularly.

---

## 8. First document poll

```bash
python run.py poll
```

Expect a large first batch, there is no prior state to compare against.
Check `state/failures.json` afterward for any adapter that could not
reach its source, and `state/records.json` to see what got extracted.

---

## 9. First transcription, and the step you should not skip

```bash
python run.py transcribe
```

This finds whatever meeting videos fall inside a currently open window,
fetches captions where they exist, and falls back to Whisper where they
do not. Expect twenty to forty five minutes if a county meeting is due
for Whisper; seconds if only caption-based sources are due.

**Open the resulting file in `transcripts/` and check it against the
source video before you trust any of it.** Confirm the transcript reads
sensibly, that speaker attribution in the extracted record looks right
for at least the roll call and one or two public comments, and that
timestamps line up. This is the one manual check in the whole setup and
it is worth doing properly once, since it validates every piece: audio
extraction, Whisper accuracy on this specific meeting room's acoustics,
and the extraction prompt's speaker identification all at once.

---

## 10. First digest

```bash
python run.py digest
```

Builds and emails a brief from whatever records exist so far. Read it
against the same standard as any other output here: does it distinguish
recommended from adopted, does the Gaps section actually list what is
missing, does anything read as more confident than the underlying record
supports.

---

## 11. Turn on the schedule

```bash
git add config.yml state transcripts
git commit -m "configure govwatch: calendar, granicus, youtube, roster"
git push
```

The workflow in `.github/workflows/govwatch.yml` runs automatically from
here, twice a day at 08:00 and 15:00 Eastern. Nothing further to start.

Confirm it is wired correctly with one manual run: GitHub repo → **Actions
→ govwatch → Run workflow**, mode `tick`. Watch it in the Actions tab.
It should either report open windows and do the corresponding work, or
report none and exit in about a minute.

---

## Where transcripts are stored

**`transcripts/` in the repository, one markdown file per meeting,
committed to git.**

```
transcripts/2026-08-24-county-board-of-commissioners-august-24-2026.md
```

Filename pattern: `{meeting date}-{body}-{slugified title}.md`. Each file
carries YAML frontmatter, then the transcript body:

```markdown
---
body: county
meeting_date: 2026-08-24
title: Board of Commissioners | August 24, 2026
source: https://transylvaniacounty.granicus.com/...
method: whisper base.en
transcribed: 2026-08-25
speaker_labels: none. speakers are identified at extraction time,
  from what was said aloud, not by voice matching.
---

# Board of Commissioners | August 24, 2026

> This is an unofficial transcript produced by automated speech
> recognition or published captions. It is not the official record.
> Verify against the source video before quoting anyone. Every
> paragraph carries a timestamp so that check is quick.

[00:00:12] Chair McCall: We'll call the meeting to order...

[00:00:47] ...
```

This is the durable artifact. Committed rather than kept as a build
byproduct, because it is the thing this project exists to produce, not
just an intermediate step toward the brief. Browsable on GitHub,
linkable by URL, searchable with plain `grep`, and diffs paragraph by
paragraph if anything is ever corrected.

A small pointer file per video also lives in `state/video/{id}.json`.
That is bookkeeping only, which file this video's transcript is at, and
which method produced it, so nothing is ever transcribed twice. It has
no content of its own worth reading.

The synthesized weekly briefs are separate: `briefs/YYYY-MM-DD-brief.md`,
also committed. A brief is built from many meetings' extracted records
and is disposable in the sense that it can be regenerated from the
transcripts at any time. The transcripts cannot be regenerated; if a
video is ever taken down, the transcript in this repo is what remains.

---

## After setup: what to check periodically

- **The Gaps section of each digest.** It is not padding. It names every
  document and video the pipeline could not retrieve that cycle.
- **`state/failures.json`** after any poll, for a source that has started
  silently failing.
- **A transcript against its video**, occasionally, not just the first
  one. Whisper accuracy can vary meeting to meeting with room acoustics
  and crosstalk, and speaker attribution is inference, not certainty.
- **`config.yml`'s `known_gaps`** and `video.granicus.view_id`, if the
  county ever changes its Granicus setup. Re-run `probe` if county video
  stops appearing.
