"""
Two-pass summarization.

Pass 1 (per document, Haiku): pull structured facts out of one agenda or
set of minutes. Cheap, parallel-friendly, and the JSON is auditable, so
when the brief says something wrong you can see which document it came
from instead of guessing.

Pass 2 (once, Sonnet): synthesize the extracted records across all three
bodies into the brief. This is the only place cross-body reasoning
happens.

Splitting it this way matters. A single pass over raw packets produces
confident summaries of things that are not in the documents.
"""

from __future__ import annotations

import os
import json
import anthropic

FAST_MODEL = "claude-haiku-4-5-20251001"
SYNTH_MODEL = "claude-sonnet-5"

# Raised from 180,000 on 2026-08-06, after measuring what the old value
# was actually doing. All three city council agenda packets exceeded it:
# 229,943, 247,300 and 266,605 characters. So between 22 and 32 percent
# of every packet was being discarded without a word, and the discarded
# part is the back of the packet, where the staff reports, contracts and
# budget detail sit rather than the running order.
#
# 180,000 characters is roughly 45,000 tokens, nowhere near Haiku's
# limit, so the cap was never protecting against anything. 600,000 is
# about 150,000 tokens, which leaves comfortable room for the system
# prompt and a 16,000 token response inside a 200,000 token context.
#
# The cost of reading a packet whole rather than four fifths of it is
# about two cents. Truncation is now a warning rather than a silence.
MAX_INPUT_CHARS = 600_000

# Same reasoning applied to the synthesis payload. The digest already
# windows records to digest_days, so this is a backstop rather than a
# working limit, but a backstop that fires silently drops meetings out
# of a brief with nothing to show it happened.
MAX_SYNTH_CHARS = 400_000

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

BODY_NAMES = {
    "city": "Brevard City Council",
    "county": "Transylvania County Board of Commissioners",
    "schools": "Transylvania County Board of Education",
    "elections": "Transylvania County Board of Elections",
    "press": "local press coverage",
}

EXTRACT_SYSTEM = """You extract structured facts from local government meeting documents.

Return ONLY a JSON object, no prose and no code fences, with these keys:

  actions_taken       list of {what, vote, amount, who}
                      Only items actually voted on or approved. vote is
                      the recorded tally or "unanimous" or "" if absent.
  pending             list of {what, next_step, date}
                      Items heard but not decided, tabled, or scheduled
                      for a later vote.
  money               list of {item, amount, fund}
                      Dollar figures with enough context to be useful.
  deadlines           list of {what, date}
                      Public hearings, comment windows, votes, filings.
  public_process      list of strings
                      Anything touching public comment, notice, decorum,
                      agenda-setting, transparency, or meeting access.
  schedule_changes    list of strings
                      Cancellations, reschedules, time changes.
  quotes              list of {speaker, text}
                      At most three. Each under 15 words. Verbatim.
  watchlist_hits      list of strings, from the supplied watchlist only.

Rules:
  Use only what is in the document. Never infer, never fill gaps.
  If a key has nothing, return an empty list.
  Leave a field as "" rather than guessing.
  Do not reproduce long passages. Quotes are capped at 15 words each."""

SYNTH_SYSTEM = """You write a local government brief for the chair of a county
Democratic party. He is an informed reader who attends these meetings and
does not need civics explained to him.

Audience and purpose: he uses this to decide where to show up, what to
write about, and when public comment actually matters. Lead with what
changed and what is decidable soon.

Structure:
  1. Short "what to watch" opener. Three items maximum, each tied to a date.
  2. One section per body: what happened, then what is coming.
  3. Cross-cutting threads. Only where a genuine connection exists across
     two or more bodies. Do not manufacture themes.
  4. Calendar table.
  5. Gaps. Name every document you did not have. This section is not
     optional and it is not padding.

Hard rules:
  Every factual claim must trace to a supplied extraction record. If
  something is not in the records, it does not go in the brief. Where a
  vote or figure is unconfirmed, say so in the sentence itself, not in a
  footnote.

  Distinguish "recommended" from "adopted" and "heard" from "approved".
  These get conflated and the difference is the whole story.

  Records sourced from transcripts carry weaker evidence than records
  from agendas and minutes. Speaker labels were inferred from context,
  and speech recognition mangles names and figures. When a claim rests
  only on a transcript, say so in the sentence: "per the meeting
  recording" or "as heard on the recording". Never present a transcript
  derived vote tally as the official record. Any value marked "(verify)"
  must keep that flag or be dropped.

  Public comment usually appears only in transcripts. Report each
  speaker and what they raised. Do not compress several speakers into
  one summary line, and do not omit speakers whose position differs from
  what you expect the reader to think.

  Unanswered questions and follow-up commitments are the most actionable
  things a transcript surfaces. Give them their own subsection under the
  body they came from.

  Be even-handed about what officials said. Report positions accurately,
  including ones the reader may disagree with. He wants to know what was
  actually said, not a version of it.

  Do not editorialize or supply talking points. Note where a decision is
  still open and when, and let him draw the conclusions.

  Quotes stay under 15 words, one per source, verbatim.

Style:
  Markdown. No em dashes and no en dashes anywhere. Use commas, periods,
  or colons instead. Plain declarative sentences."""


TRANSCRIPT_EXTRACT_SYSTEM = """You identify speakers in a local government meeting
transcript and extract structured facts from it, in a single pass.

A transcript is not minutes. Minutes record outcomes. A transcript
records the argument, the objections, the questions staff could not
answer, and the public comment that minutes compress into a single line.
Extract what only the transcript can tell you.

The transcript has no speaker labels. Identify speakers as you read.
These meetings identify their own speakers, so use that:
  A roll call near the start establishes who is present.
  The chair names each public comment speaker before they come forward.
  Staff state their name and department before presenting.
  Members address each other by name and title.

You are given a roster of known participants. Speech recognition mangles
proper nouns, so a near miss on a roster name should be corrected to the
roster spelling. Never assign a name the transcript did not say. Where
nobody is identified, write "Unidentified speaker".

Return ONLY a JSON object, no prose and no code fences, with these keys:

  roll_call           list of names recorded as present

  actions_taken       list of {what, vote, amount, who}
                      Only votes announced aloud. Never compute a tally.
  pending             list of {what, next_step, date}
  money               list of {item, amount, fund}
  deadlines           list of {what, date}
  public_comment      list of {speaker, topic, summary, response}
                      Every speaker separately. response is what any
                      official said back, or "" if nobody responded.
  disagreements       list of {issue, positions}
                      positions is a list of {who, position}. Record
                      splits accurately, including which way each member
                      leaned. Do not soften or flatten a disagreement.
  unanswered          list of strings
                      Questions asked that got no substantive answer,
                      and commitments to follow up later. These are the
                      most useful items in any transcript.
  commitments         list of {who, what, by_when}
  public_process      list of strings
                      Anything about public comment, notice, decorum,
                      agenda-setting, closed session, or meeting access.
  schedule_changes    list of strings
  quotes              list of {speaker, text, start}
                      At most three. Each under 15 words. Verbatim.
  watchlist_hits      list of strings, from the supplied watchlist only.

Rules:
  Every speaker attribution here is inference from context, not voice
  identification, and speech recognition mangles names and numbers.
  Where a name or figure carries weight, append " (verify)" inside the
  value.
  Attribute a statement only where the transcript identifies who spoke.
  Otherwise write "Unidentified speaker".
  Use only what is in the transcript. Never infer, never fill gaps.
  If a key has nothing, return an empty list."""


ASK_SYSTEM = """You answer questions from excerpts of local government meeting
transcripts, and from nothing else.

The excerpts are unofficial, produced by automatic speech recognition or
published captions, and carry no speaker labels. Names and figures are
frequently mangled. Each paragraph opens with a timestamp.

Rules:
  Answer only from the excerpts supplied. If they do not answer the
  question, say so plainly and say what they do cover instead. Never
  fill a gap from general knowledge about how local government works.

  Cite every claim with the meeting date and the timestamp, like
  (2026-06-22, 02:32:21). A claim with no citation does not belong in
  the answer.

  Attribute a statement only where the excerpt says who was speaking.
  Otherwise write "an unidentified speaker".

  Treat every name and number as unverified, because speech recognition
  mangles both. Where one carries weight, say it should be checked
  against the recording.

  These are excerpts, not whole meetings. Say so where the answer might
  turn on something in the parts not shown.

Style:
  Markdown. No em dashes and no en dashes anywhere. Use commas, periods,
  or colons instead. Plain declarative sentences. Lead with the answer,
  then the evidence."""


def answer(question: str, excerpts: str) -> str:
    """
    Answer one question from transcript excerpts.

    Sonnet rather than Haiku. This reasons across several meetings at
    once and is asked ad hoc rather than on a schedule, so quality
    matters more than the fraction of a cent the difference costs.
    """
    msg = client.messages.create(
        model=SYNTH_MODEL,
        max_tokens=4000,
        system=ASK_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Question: {question}\n\n---\n\n{excerpts}"}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def extract(doc: dict, watchlist: list[str], roster: list[str] | None = None) -> dict:
    """
    One document in, one structured record out.

    For transcripts this does speaker identification and fact extraction
    in the same call. An earlier version ran them as two passes, which
    meant reading every transcript twice and accounted for two thirds of
    the project's entire token spend. The separation bought nothing:
    extraction already has to know who said what in order to report
    public comment and attribute a split, so it was doing the
    identification work regardless and then paying for it twice.

    The system prompt is marked for caching. It is the same on every
    call, so after the first request in a five minute span the rest read
    it at a tenth of the price.
    """
    raw = doc.get("text", "")
    text = raw[:MAX_INPUT_CHARS]
    if len(raw) > MAX_INPUT_CHARS:
        # Loud, because the old limit was silent and was throwing away
        # real content. See MAX_INPUT_CHARS.
        print(f"WARNING: {doc.get('title', '?')} is {len(raw):,} chars and was "
              f"cut to {MAX_INPUT_CHARS:,}. {len(raw) - MAX_INPUT_CHARS:,} "
              f"characters were not read.")
    if len(text) < 200:
        return {}

    is_transcript = doc.get("kind") == "transcript"
    system = TRANSCRIPT_EXTRACT_SYSTEM if is_transcript else EXTRACT_SYSTEM

    header = (
        f"Body: {BODY_NAMES.get(doc['body'], doc['body'])}\n"
        f"Document type: {doc['kind']}\n"
        f"Meeting date: {doc.get('meeting_date') or 'unknown'}\n"
        f"Watchlist: {', '.join(watchlist)}\n"
    )
    if is_transcript and roster:
        header += "Roster of known participants:\n" + "\n".join(roster) + "\n"

    # Raised from 8000 on 2026-08-06. The 2026-06-22 county meeting, seven
    # public comment speakers plus eight unanswered items, ran past 8000
    # and returned truncated JSON, which parsed as nothing and was then
    # treated as permanently unextractable. Output is billed on tokens
    # actually generated, so a higher ceiling costs nothing on the
    # documents that never approach it.
    max_tokens = 16000 if is_transcript else 4000

    msg = client.messages.create(
        model=FAST_MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"{header}\n---\n{text}"}],
    )

    # Raise rather than return {}. The caller treats an empty return as a
    # permanent verdict and stops retrying, which is right for content
    # that is genuinely unusable and wrong for a cap that can be lifted.
    # A truncated response is the second kind and has to stay retryable.
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(
            f"extraction hit the {max_tokens} token output cap on "
            f"{doc.get('title', 'this document')!r} and the JSON is "
            f"truncated. Raise max_tokens rather than accepting the loss."
        )

    raw = "".join(b.text for b in msg.content if b.type == "text")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        # Was silent, which made a parse failure indistinguishable from a
        # document with nothing in it.
        print(f"extraction returned unparseable JSON for "
              f"{doc.get('title', '?')}: {e}. First 200 chars: {raw[:200]!r}")
        return {}
    rec["_source"] = {k: doc[k] for k in ("body", "kind", "title", "meeting_date", "url")}
    return rec


def synthesize(records: list[dict], missing: list[str], period: str) -> str:
    """Pass 2. All records in, one brief out."""
    full = json.dumps(records, indent=2)
    payload = full[:MAX_SYNTH_CHARS]
    if len(full) > MAX_SYNTH_CHARS:
        print(f"WARNING: {len(records)} records serialize to {len(full):,} chars "
              f"and were cut to {MAX_SYNTH_CHARS:,}. The brief is being written "
              f"from an incomplete set. Narrow digest_days or raise the cap.")
    gaps = "\n".join(f"- {m}" for m in missing) or "- none recorded"

    msg = client.messages.create(
        model=SYNTH_MODEL,
        max_tokens=8000,
        system=SYNTH_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Reporting period: {period}\n\n"
                f"Documents that could not be retrieved:\n{gaps}\n\n"
                f"Extraction records:\n{payload}"
            ),
        }],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()
