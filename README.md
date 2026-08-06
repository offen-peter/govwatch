# TCDP GovWatch

Automated monitoring of the three local bodies: Brevard City Council, the
Transylvania County Board of Commissioners, and the Transylvania County
Board of Education. Polls for new agendas and minutes on weekdays, alerts
on watchlist hits between digests, and emails a full brief on Fridays.

Same shape as the social pipeline: Python, GitHub Actions, Claude API,
Gmail SMTP, state committed back to the repo. No server, no database.

## Setup

1. Create the repo and push these files.
2. Add three repository secrets: `ANTHROPIC_API_KEY`, `GMAIL_USER`,
   `GMAIL_APP_PASSWORD`.
3. Set your address in `config.yml` under `email.to`.
4. Run the workflow manually once with mode `probe`. This checks the
   county's Granicus endpoints and reports which respond. Put the
   working view number in `config.yml` under `video.granicus.view_id`.
   Until you do, the county video adapter is a deliberate no-op rather
   than a guess.
5. Run the workflow manually once with mode `discover`. This prints the
   CivicClerk Events payload for the Brevard tenant. Check the field
   names against `CityCouncil.collect()` and adjust if they differ. This
   is the one piece I could not verify directly, because the portal is a
   JavaScript app. The API pattern itself is confirmed working.
6. Run once with mode `poll`. Expect a large first batch, since the
   initial run has no state to compare against.
7. Point `schedule.shared_js_url` at your hosted file and run mode
   `schedule`. It prints the calendar it assembled, which source supplied
   each meeting, and which windows are open. Check this before anything
   else, because every other decision follows from it.
8. Run once with mode `transcribe` and read the first transcript against
   the video before you trust any of it.

## Scheduling

### Where the meeting calendar comes from

One correction worth stating plainly: the Mobilize feeds do not carry the
government meeting schedule. You established that while building the
events widget, which is why government meetings are hardcoded there. So
the authority for this pipeline is the same hardcoded schedule you
already maintain in `tcdp-shared.js`, not Mobilize.

That keeps your single source of truth single. When the county cancels a
meeting, you add one line to the file you already edit, and both the
website widget and this pipeline stop expecting materials that will never
appear.

Resolution order, first hit wins per meeting:

| Source | Role |
|---|---|
| `tcdp-shared.js` | Authoritative. Cancellations here override everything. |
| Official body sites | Catches a change made this morning that has not reached shared.js |
| Mobilize | Supplement only, for the rare staged meeting. Uses org 1550. |
| Recurrence rules | Last resort so the pipeline degrades instead of going blind |

If a future meeting exists only as a recurrence rule and is absent from
`tcdp-shared.js`, it is treated as cancelled. Silence in the
authoritative source means do not go looking.

The expected shape in `tcdp-shared.js`, which the parser will find under
`govMeetings`, `governmentMeetings`, or `GOV_MEETINGS`:

```js
const govMeetings = [
  {"body": "county",  "date": "2026-08-24", "title": "Board of Commissioners, 6pm"},
  {"body": "county",  "date": "2026-07-27", "cancelled": true},
  {"body": "schools", "date": "2026-08-17", "title": "Board of Education"},
  {"body": "city",    "date": "2026-08-03", "title": "City Council"}
];
```

If your current structure differs, the tolerant parser may still handle
it. Run `python run.py schedule` and check what it found before relying
on it.

### Windows and cadence

Nothing upstream can push to us. GitHub Actions cannot be triggered by a
clerk uploading a PDF. But we know the meeting calendar, so there is no
reason to poll around the clock. The workflow runs twice a day, 08:00
and 15:00 Eastern, and the schedule module decides whether anything
could plausibly have appeared. The morning run does the real work; the
afternoon run is the retry.

Each meeting opens three windows, each with its own polling cadence:

| Artifact | Window | Checked | Why |
|---|---|---|---|
| agenda | T-7 to T | daily | Packets post a few days ahead, in business hours |
| video | T+1 to T+4 | both daily runs | Uploaded overnight, and it is the only same-week county record |
| minutes | T+14 to T+75 | weekly | Approved at a later meeting, so the lag is structural |
| press | T+1 to T+10 | every 3 days | Publishes on its own rhythm |

Video starts at T+1 rather than T+0 on purpose. All three bodies convene
in the evening and sit for two to three hours, so a same-day check is
either mid-meeting or after the last scheduled run, and a three hour
video needs processing time after upload besides. The next morning is
the realistic first sighting. The window runs to T+4 so a late upload
still gets caught rather than missed outright.

Window and cadence are different questions, and conflating them is what
makes a frequent schedule expensive. A window says an artifact could
exist. Cadence says how urgently to look. Video gets chased hard because
it is the thing you actually want quickly. Minutes sit in a wide window
but nobody gains from asking twice a day for something weeks out.

Windows close on acquisition and stay closed. Cancelled meetings never
open one at all.

### What that costs, measured

Simulating 45 days against a realistic calendar, six meetings including
one cancelled, with artifacts arriving on plausible lags:

- **60 runs a month**, down from 360
- **All six artifacts captured**, none missed
- **Mean capture latency 9 hours, worst case 17** from posting to the
  pipeline holding it. Both are well inside the useful window: an agenda
  found the morning after it posts still leaves five days before the
  meeting.
- **63% of runs exit immediately** having found no open window
- ffmpeg and the Whisper stack install only when a video window is
  actually open, so the rest skip the expensive setup entirely

For the two bodies on YouTube the detection path is the channel RSS feed,
which updates within minutes of an upload. That is as close to a push
notification as any of these sources offers.

## How it works

```
schedule.py  meeting calendar -> which artifacts could exist right now
   |
sources.py   four document adapters -> Doc objects
video.py     meeting video -> captions or Whisper -> transcripts/*.md -> Doc
   |
run.py tick  runs only what the open windows justify
   |         de-duplicate against state/seen.json
   |
brief.py     pass 1: Haiku extracts structured JSON per document
   |         -> state/records.json
   |
run.py digest
   |
brief.py     pass 2: Sonnet synthesizes records into the brief
   |
   -> briefs/YYYY-MM-DD-brief.md, emailed, committed
```

### Why two passes

A single pass over raw meeting packets produces fluent summaries of
things that are not in the documents. Splitting extraction from synthesis
means every claim in the brief traces back to a JSON record you can open
and check, and the model doing the writing never sees the raw text it
might be tempted to embellish.

The extraction prompt refuses to infer. The synthesis prompt is told to
distinguish recommended from adopted and heard from approved, because
those get conflated and the difference is usually the whole story.

### Why a watchlist

Alerts between digests are only useful if they are rare. The list in
`config.yml` is set to the threads you already track. Prune it rather
than extend it. Thirty-four terms is close to the ceiling before the
alerts stop being worth opening.

## Meeting transcription

This is the part that closes the county gap, so it is worth
understanding what it does and does not give you.

### Captions first, Whisper second

All three bodies publish captions, so in practice captions are not the
first choice, they are the only one. Whisper has never run.

| Body | Source | Captions |
|---|---|---|
| County | Vimeo, linked from the county's own meetings index | auto-generated, fetched from the player config endpoint |
| City | YouTube, the council's own meetings playlist | auto-generated |
| School board | YouTube | auto-generated |

The county was supposed to be the body that needed Whisper. It turned
out to stream every meeting to its own Vimeo account, and Vimeo
generates an English track for each one. Three hops get it, none of them
needing credentials: the event URL from the meetings index, the event
embed page for the player id, then `player.vimeo.com/video/{id}/config`
for the caption track.

That matters more than it sounds, because yt-dlp cannot reach Vimeo at
all any more. Vimeo withdrew anonymous extraction and its extractor now
demands a login. The player config endpoint is a different path and
still answers, which is the only reason county video is reachable.

A typical week is therefore three caption fetches taking seconds
between them, and no CPU time at all.

### YouTube blocks datacenter addresses

The city and school board paths run through yt-dlp, and yt-dlp works
from a laptop and fails on a GitHub runner. YouTube requires a proof of
origin token for subtitle requests and challenges datacenter addresses
far harder than residential ones. On 2026-08-06 all three city videos
failed in Actions and the same three succeeded locally minutes later.

The workflow answers that with three things the runner was missing:
deno for the JavaScript runtime, curl-cffi for TLS impersonation, and
the bgutil proof of origin token provider. The county is unaffected,
since Vimeo never touches yt-dlp.

### Whisper settings, and why

Meeting audio breaks Whisper in specific ways, so the defaults are not
the library defaults:

- **Voice activity detection is on.** Meetings contain long dead air:
  recesses, people walking to the podium, closed session. Whisper
  hallucinates into silence, usually by repeating the previous sentence
  or emitting a stray "thank you". VAD strips the silence before it can.
- **`condition_on_previous_text` is off.** This is what turns a single
  bad guess into three minutes of drift.
- **Audio is downmixed to mono 16kHz** before transcription, which is
  all the model uses and a fraction of the bytes to move and store.
- **`base.en` by default.** Roughly four to eight times realtime on a
  GitHub runner. `small.en` handles gavel bangs, crosstalk and room echo
  noticeably better at about half the speed. Switch in `config.yml` if
  the county audio disappoints you, and it may.

None of the above has been exercised. Every one of these settings is
reasoning about meeting audio, not a measurement of it, because every
body turned out to publish captions. Whisper is the fallback for a
meeting whose caption track is missing, which has not happened yet but
nearly did: the school board's 2026-06-16 video was removed from YouTube
before it could be fetched. Treat this section as untested until a
transcript actually comes back marked `whisper base.en`.

### Speaker attribution is inference

There is no voice identification here. Claude reads the transcript
against the roster and the agenda and works out who is speaking from
what the room says out loud: the roll call, the chair naming each public
comment speaker before they approach, staff introducing themselves.

This works because these meetings narrate themselves. It is still
inference, and it will be wrong sometimes. Every transcript carries a
header saying so, the attributor is instructed never to invent a name
and to write "Unidentified speaker" instead, uncertain figures get
flagged `(verify)`, and the synthesis prompt is told never to present a
transcript-derived vote tally as the official record.

Acoustic diarization with pyannote would add a model download, a Hugging
Face token, and a licence acceptance step, and would still not tell you
which voice belongs to which commissioner without a labelled sample of
each. Context is the better tool for this specific problem.

**Do not quote anyone from a transcript without checking the video.**
The timestamps in the transcript are there so that check takes seconds.

### Where transcripts live

Each meeting becomes one markdown file in `transcripts/`, committed to
the repo:

```
transcripts/2026-08-24-county-board-of-commissioners-august-24-2026.md
```

with YAML frontmatter recording the body, meeting date, source URL, and
whether it came from captions or Whisper. Frontmatter is stripped before
anything is sent to the model, so it costs nothing to keep.

To be clear about what this does and does not buy: **it saves no tokens.**
The model receives the same characters whether they came from a markdown
file or a JSON blob, and the API bills on what you send, not on how it
was stored. What it buys is:

- **A readable archive.** Browsable on GitHub, linkable, citable. If you
  want to show someone what was actually said at a meeting, you send
  them a URL.
- **Diffs that mean something.** A transcript inside JSON is one 30,000
  character line and every commit is an unreadable wall. As markdown it
  diffs paragraph by paragraph.
- **grep.** Five years of meetings become searchable with no tooling at
  all.

Bookkeeping stays in `state/video/` as a small pointer file per video, so
nothing is ever transcribed twice.

One future payoff worth noting: because the files are structured and
timestamped, a later ad hoc question, say what anyone said about the
landfill this year, could grep the archive and send only matching
sections to the model rather than every transcript in full. **That** would
save tokens, unlike the storage format itself.

### What transcripts add that minutes never will

The extraction prompt for transcripts asks for different things than the
one for agendas, because a transcript is not minutes. Minutes record
outcomes. Transcripts record:

- **Public comment, speaker by speaker.** County minutes compress an
  entire comment period into a line or two. This is the single biggest
  gain.
- **Unanswered questions,** and commitments to follow up later. Usually
  the most actionable thing in the file.
- **Disagreements with attribution,** including which way each member
  leaned on a split.
- **What staff could not answer** when pressed.

### Cost

**About $1.00 a month now, nearer $1.30 from September, $0 in
infrastructure.**

Higher than the $0.74 this section used to claim, for three reasons,
none of them a regression:

- Sonnet's introductory rate ends 31 August. Output goes from $10 to
  $15 per million, and brief synthesis is output-heavy.
- Agenda packets are now read whole. The old 180,000 character cap was
  discarding between 22% and 32% of every city packet in silence, so the
  old figure was partly the price of not reading things.
- The county now produces transcripts. It was the body the old model
  assumed would have none.

### Where the money goes

Measured from one full cycle, five meetings a month across the three
bodies, rather than estimated.

| Stage | Model | Input | Output | Cost |
|---|---|---|---|---|
| Transcript extraction, 5 meetings | Haiku | 156,000 | 18,000 | $0.25 |
| City agenda packets, 2 | Haiku | 125,000 | 3,000 | $0.14 |
| County minutes, agendas, notices | Haiku | 45,000 | 14,000 | $0.11 |
| Brief synthesis, ~4 digests | Sonnet | 86,000 | 34,000 | $0.77 |
| | | **412,000** | **69,000** | **~$1.27** |

Synthesis is now the largest line, which is a change worth noticing. It
is a fifth of the input and three fifths of the cost, because output
tokens are five times the price of input and a brief is mostly output.
The lever there, if one is ever wanted, is `digest_days` and how many
records a single brief has to read, not the extraction side.

### Why extraction is one pass

Before optimizing, two thirds of every token went to reading the same
transcripts twice: once for speaker attribution, once for fact
extraction. The separation bought nothing. Extraction already has to
know who said what in order to report public comment and attribute a
split, so it was doing the identification work anyway and being billed
for it twice.

Accuracy is unaffected or slightly better. The merged prompt sees the
roster, the agenda and the full transcript at once, where the old
extraction pass only ever saw a pre-labelled summary of who spoke.

Rates verified 24 July 2026: Haiku 4.5 at $1.00/$5.00 per million
tokens, Sonnet 5 at $2.00/$10.00 introductory through 31 August, then
$3.00/$15.00. The figures above use the September rates.

### The cheap way to interrogate the archive

`run.py ask` exists because the alternative is expensive. Six meetings
is 768,000 characters, so putting one question to the whole archive
would cost more than a month of everything else here. Grepping first and
sending only matching paragraphs put a real cross-meeting question at
9,048 tokens, about a twentieth of that, and the ratio improves with
every meeting added.

### What was left on the table, deliberately

**The Batch API halves everything, taking this to $0.37.** It is not
implemented. Batch adds a submit, poll and retrieve cycle and up to 24
hours of latency, in exchange for saving thirty seven cents a month.
That is a bad trade, and it would undercut the point of catching video
quickly. Worth revisiting only if this is ever pointed at a dozen bodies
instead of three.

**Compressing transcripts before extraction** would save more. It is
also the one optimization that would directly damage what this exists to
produce. The transcript is the asset. Do not trim it to save pennies.

### GitHub Actions

Runs dropped from 360 a month to 60 once the schedule became anchored to
actual meetings.

| Run type | Count | Minutes each | Total |
|---|---|---|---|
| Exited immediately, no window open | 38 | 1 | 38 |
| Working run, documents only | 12 | 2 | 24 |
| Video window open, media stack installed | 10 | 5 | 50 |
| County meeting, Whisper transcription | 0 | n/a | 0 |
| | | | **~112 min** |

Down from the 232 this table used to show, and for an unexpected reason:
150 of those minutes were budgeted for county Whisper runs that will
never happen, because the county publishes captions. What replaced them
is smaller and different. A video-window run now installs faster-whisper,
ffmpeg, deno and pulls the token provider image, which is about three
minutes of setup for a fallback that has not yet been needed.

That is insurance, not waste, but it is worth knowing it is insurance.
The remaining lever is scheduling again, not transcription time.

This repository is public, so Actions minutes are unlimited and free.
Private repositories get 2,000 a month, which this would still sit well
inside.

### The real cost

Neither number above is the one that matters. What this costs is the
attention to check the Gaps section, verify a figure before publishing
it, and notice when a scraper has been failing quietly for three weeks.
Budget for that rather than for the dollar.

## Commands

```
python run.py tick         # the scheduled entrypoint, window aware
python run.py schedule     # print the calendar and today's open windows
python run.py poll         # find and extract new documents, ignores windows
python run.py transcribe   # find, transcribe, and extract new video
python run.py digest       # build and send the brief
python run.py ask "..."    # question the transcript archive, grep first
python run.py discover     # print the CivicClerk payload, setup only
python run.py probe        # probe the county's Granicus endpoints, setup
```

`ask` is the payoff this section predicted. It greps `transcripts/` for
the query terms and sends only the paragraphs that match, so a question
across the whole archive costs a few thousand tokens rather than every
transcript in full. Measured on six meetings: "what was said about the
landfill and solid waste" matched 49 paragraphs across 5 meetings,
9,048 tokens, against 192,000 for the archive entire. Every paragraph
arrives with its timestamp, so the answer cites a meeting and a time you
can check against the recording.

Transcripts are cached in `state/video/` by video id and committed, so
nothing is ever transcribed twice and the archive accumulates.
