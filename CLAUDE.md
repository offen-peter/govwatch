# GovWatch

Automated monitoring of three local government bodies (Brevard City
Council, Transylvania County Board of Commissioners, Transylvania
County Board of Education): scheduled polling, video transcription,
Claude-generated briefs. Python, GitHub Actions, no server, no database.

@README.md
@SETUP.md
@config.yml

Read those three first. This file is only what a session needs that
isn't already written down there: priorities and standing rules.

## Verified against the real endpoints, 2026-08-05 and 06

This was built without network access to any of its sources, so the
first three priorities were to check it against reality. All three are
done. Recorded here because each one contradicted the design, and
knowing what was already wrong is worth more than the instruction to
check it again.

1. **probe.** No usable county Granicus feed exists. Legacy RSS views
   carry only 2022 training clips, view 2 returns 403, the legacy API
   paths are gone. `video.granicus.enabled` is now false. County video
   comes from Vimeo instead, see below.
2. **discover.** The CivicClerk field names were right, and the query
   was wrong in a way that returned nothing while raising nothing: no
   upper bound on the filter, and `$top` capped at 15 server side with
   the rest behind `@odata.nextLink`. The city adapter was silently
   dead. Now bounded and paged.
3. **schedule.** `tcdp-shared.js` parses, but every entry was being
   discarded: the live array has no `body` field, only a title, and
   `from_shared_js` consulted `body` and `entity` only. The
   authoritative calendar was being ignored in silence and the pipeline
   was falling back to recurrence rules.

Two more things found the same way. The county's own `/meetings` index
lists agendas, minutes and a Vimeo recording per meeting, which replaced
URL guessing and closed the county video gap. And YouTube refuses
subtitle requests from datacenter addresses, so city and school board
video works locally and needs a proof of origin token in Actions.

The lesson worth keeping: every one of these failed silently. Prefer a
loud failure to a tidy empty result.

## Next: decide whether to OCR the elections minutes

The Board of Elections is wired in as a fourth body and works. It is
also, right now, capturing almost nothing, and the reason is the source.

**93 percent of its minutes are scanned images.** Measured 2026-08-06
over the sixteen most recent sets: one readable, fourteen scanned with
no extractable text, one linking a file that 404s. The board publishes
no agendas and no recordings, so minutes are the only account any of its
meetings will ever get, and this pipeline can read one meeting in
sixteen.

Everything else about the body is done. Its own six month schedule PDF
is the calendar source, parsed by `from_boe_schedule`, which matters
because no recurrence rule describes it: the gaps between its next
thirteen meetings run 28, 19, 7, 7, 7, 7, 6, 1, 3, 6, 1, 27 days,
monthly most of the year and near daily through a canvass.
`BODY_ARTIFACTS` restricts it to the minutes window, so no video window
ever opens for a body with no video, which also keeps the media stack
out of runs that would have no use for it.

So the one open question is OCR. Arguments for: the documents are typed
and then scanned, which is the case OCR handles best, they are short, a
few pages each, and without it this body is decoration. Against: it
means tesseract as a system dependency, a slower document poll, and text
of a quality nothing else in the pipeline has to caveat.

If OCR is added, keep it failing soft. A missing tesseract should leave
the gap recorded, exactly as now, not break the document poll for the
other three bodies.

Worth trying first, since it costs nothing: ask the board whether they
can publish minutes as text. They are produced in a word processor and
then scanned, so the digital originals exist.

## Standing rules

- No em dashes or en dashes, anywhere, including in code comments and
  commit messages. Commas, periods, or colons instead. This applies to
  the whole repo, not just user-facing text.
- Never scrape a source whose robots.txt disallows it. Find the open
  feed or API instead, or leave the gap and say so in `known_gaps`.
- Every claim a transcript or extracted record makes should be
  traceable to the source document. Don't let a summarization pass
  quietly upgrade "heard" into "approved."

## Design decisions, don't re-litigate without a real reason

- **Extraction and speaker attribution are one Haiku call, not two.**
  They used to be separate passes, both reading the full transcript.
  Merging cut token spend from $1.10/mo to $0.74/mo, see `brief.py`
  docstring on `extract()`.
- **The meeting calendar's authority is `tcdp-shared.js`, not Mobilize.**
  Confirmed while the events widget was built: government meetings
  aren't in Mobilize. Don't add a Mobilize-first resolver back.
- **Ticks are anchored to the meeting calendar, not a blanket cron.**
  `schedule.py` cut runs from 360/mo to 60/mo with zero missed
  artifacts in simulation. Anchoring uses a window (could this artifact
  exist) and a separate cadence (how often to check within that window),
  don't collapse them back into one number.
- **Transcripts are committed markdown in `transcripts/`, not JSON.**
  Costs the same tokens either way, the API bills what you send. The
  point is a browsable, diffable, greppable archive.
- **Hosted runners, not self hosted, even though self hosted would fix
  YouTube.** YouTube gates subtitle requests behind a proof of origin
  token and challenges datacenter addresses far harder than residential
  ones, so city and school board captions fetch fine from a laptop and
  fail in Actions. A self hosted runner on a home connection solves that
  outright. It was built and backed out on 2026-08-06 anyway, because it
  makes every run depend on one machine being awake at 12:17 and 19:17
  UTC, and a missed slot there is missed rather than queued. Trading a
  visible failure for a silent one is a bad trade. It also avoids the
  question GitHub raises about self hosted runners on public repos.
  The cloud answer is deno, curl-cffi and the bgutil token provider, see
  the workflow. If that proves insufficient, the fallback is a split:
  cloud does documents and county video, which never touches yt-dlp, and
  city video gets swept up by running `transcribe` locally inside
  `video.lookback_days`.
- **City council committees are in scope, not noise.** Brevard prefixes
  every committee with "City Council", so `CityCouncil.collect()`'s
  substring match takes them all. Confirmed keep, 2026-08-06: 7 full
  council documents against 12 committee ones over 60 days, but the
  committees are only ~1,800 tokens combined, about half a cent a cycle.
  They are upstream of council decisions, which is where turning up
  still changes the outcome. The thing to guard is conflation, never
  letting a committee recommendation read as a council action, and the
  synthesis prompt already covers that.
- **Speaker attribution is inference from context, not voice
  diarization.** These meetings self-narrate: roll call, the chair
  naming public comment speakers, staff introducing themselves. Cheaper
  and more reliable here than pyannote would be. It's still inference,
  every transcript says so, and the prompt is forbidden from inventing
  a name the transcript didn't say.

## Commands

```
python run.py tick         # scheduled entrypoint, window and cadence aware
python run.py schedule     # print the calendar and today's open windows
python run.py poll         # documents, ignores windows
python run.py transcribe   # video, ignores windows
python run.py digest       # build and send the brief
python run.py ask "..."    # question the transcript archive, grep first
python run.py reextract    # rebuild records from transcripts already on disk
python run.py reextract county   # ... or just the ones matching a filter
python run.py probe        # county Granicus endpoints, setup only
python run.py discover     # CivicClerk payload shape, setup only
```

`--dry-run` (also `-n`) works on tick, poll, transcribe, digest and ask.
It writes nothing, never stamps the cadence clock, and never calls the
API. On `ask` it prints the matching paragraphs and what sending them
would cost, which separates a badly worded question from an archive
that has nothing on the subject.

## Cost baseline, don't regress silently

~$0.74/mo API spend, ~$0/mo Actions minutes (232 min against a 2,000 or
unlimited free tier). If a change meaningfully increases either, mention
it before committing rather than after.

## Where things live

- `transcripts/` the durable artifact, one markdown file per meeting
- `briefs/` synthesized weekly briefs, regenerable from transcripts
- `state/` bookkeeping only: seen documents, acquired artifacts, check
  cadence, video pointer files. Nothing here is worth reading directly.
- `config.yml` all tunables: watchlist, roster, schedule source, video
  settings. Start here for behavior changes before touching code.
