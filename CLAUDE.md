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

## Priority: unverified, needs live network access

This project was built in an environment whose network is allowlisted
to package registries only (pypi, npm, github). It could not reach
transylvaniacounty.org, granicus.com, or civicclerk.com, so the
following were built from documentation and one example tenant, never
executed against the real endpoints. If you have normal network access,
do these first, in this order, and fix whatever the real output
contradicts:

1. `python run.py probe`, find the county's working Granicus `view_id`.
   If nothing responds, the county has likely moved off legacy Granicus
   entirely with no public feed. Do not scrape the HTML UI to compensate,
   its robots.txt disallows it. Leave `video.granicus.view_id` blank and
   note the gap in `config.yml`'s `known_gaps` instead.
2. `python run.py discover`, confirm the CivicClerk Events payload shape
   for the `brevardnc` tenant matches what `CityCouncil.collect()` in
   `sources.py` expects. Field names were verified on a different
   tenant, not this one.
3. `python run.py schedule`, confirm `tcdp-shared.js` parses. The parser
   in `schedule.py: from_shared_js()` is tolerant on purpose, since the
   actual file structure was never seen.

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
python run.py probe        # county Granicus endpoints, setup only
python run.py discover     # CivicClerk payload shape, setup only
```

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
