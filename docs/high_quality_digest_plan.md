# High Quality Daily Digest Plan

The current report structure is useful, but the summarization pipeline is too
shallow for long podcasts. The next iteration should keep the existing reader
facing parts and improve how evidence is extracted, ranked, and synthesized.

## Current Report Parts To Preserve

Top-level:

- `3 分钟速览`
- `全部更新`
- category sections

Per item:

- metadata line: original title, source, platform, publish time, duration,
  category, recommendation
- `嘉宾与机构`
- `30 秒结论`
- `为什么值得看`
- `完整摘要 · 深读`
- `核心要点`
- `关键事实`
- `分歧与限制`
- `你可以怎么用`

The improvement should happen behind these parts, not by replacing the format
with a new one.

## Main Problem

The current code already summarizes each transcript separately, but the
intermediate extraction is optimized for format safety:

- It prevents hallucination.
- It asks for evidence references.
- It enforces short field lengths.

It does not yet force the model to understand:

- the episode's central question
- the speaker's actual thesis
- what is new or non-obvious
- the strongest evidence
- the argument chain across a long conversation
- where the guest disagrees with common assumptions
- what a reader can reuse after reading

For long podcasts, simple chunk extraction loses the main line of reasoning.

## Proposed Pipeline

### 1. Transcript Intake And Quality Gate

Before summarization, build a transcript profile:

- duration
- transcript source: official caption, auto caption, ASR
- coverage ratio
- word/text length
- chapters from YouTube description if available
- timestamp density
- speaker clues, if available

Reject or downgrade reports when:

- `duration >= 300` and coverage is below `0.95`
- transcript text is empty
- transcript is mostly boilerplate
- only description is available

### 2. Chapter Or Topic Segmentation

For every item, split the transcript into topic segments before extraction.

Preferred order:

1. Use explicit YouTube chapters from description.
2. Use timestamp gaps and cue boundaries.
3. Use semantic segmentation every 6-10 minutes for long shows.

Each segment should carry:

```json
{
  "segment_id": "S001",
  "start": 0,
  "end": 420,
  "topic": "Why nuclear development stalled",
  "text": "..."
}
```

This prevents a 90-minute podcast from being treated as one flat text stream.

### 3. Per-Segment Evidence Extraction

Run a strict extraction prompt for each segment. The model should not summarize
yet. It should extract structured evidence:

```json
{
  "central_question": "...",
  "claims": [
    {
      "text": "...",
      "speaker": "...",
      "support": "...",
      "evidence_ref": "S003:E02"
    }
  ],
  "examples": [],
  "numbers": [],
  "mechanisms": [],
  "tensions": [],
  "quotes": [],
  "terms": []
}
```

Extraction rules:

- Separate claim, evidence, example, and opinion.
- Preserve numbers with unit and context.
- Prefer specific mechanisms over generic commentary.
- Mark uncertainty instead of smoothing it away.
- Do not turn every sentence into a bullet.

### 4. Episode-Level Value Ranking

After all segment evidence is extracted, run a second pass that ranks what is
actually valuable.

Score each candidate insight by:

- `novelty`: is this non-obvious?
- `specificity`: does it include concrete mechanism, data, or example?
- `decision_value`: can a reader use it?
- `source_authority`: is it from a guest with direct experience?
- `argument_importance`: is it central to the episode?
- `timeliness`: does it explain a current change?

This stage should output only the best 5-9 insights for long episodes.

### 5. Existing Report Part Synthesis

Use the ranked evidence to fill the current report fields.

`嘉宾与机构`

- Identify actual guest, host, organization, and role.
- Avoid generic placeholders.
- If no guest is clear, say so briefly.

`30 秒结论`

- One sentence answering: what did this episode actually teach us?
- Must include the core topic and the most valuable implication.
- Should not merely restate the title.

`为什么值得看`

- Explain why this item is worth the reader's time.
- Prefer a concrete hook: uncommon operator experience, new data, strong
  argument, market signal, policy implication, or usable framework.

`完整摘要 · 深读`

- For short items: 2-3 compact paragraphs.
- For 30-60 minute episodes: 4-5 paragraphs.
- For 60+ minute episodes: 5-7 paragraphs.
- Follow the argument arc, not timestamp order mechanically.
- Include setup, key thesis, evidence, examples, consequences, and caveats.

`核心要点`

- 4-7 bullets.
- Each bullet should be a claim with support, not a topic label.
- For long episodes, each bullet should represent one major insight.

`关键事实`

- Use only facts with strong source evidence.
- Include numbers, named organizations, products, policies, dates, or concrete
  cases.
- Drop weak or context-free facts.

`分歧与限制`

- Capture explicit disagreements, open questions, constraints, incentives,
  failure modes, and counterarguments.
- This is important for avoiding promotional summaries.

`你可以怎么用`

- Convert the episode into reader actions or reusable lenses.
- Avoid vague "continue researching" language.
- Examples:
  - "Use this as a checklist for evaluating AI infrastructure capex claims."
  - "When reading China consumption data, separate industrial strength from
    household confidence."

`3 分钟速览`

- Select top items using value scores, not recency or view counts.
- Explain the reason in one sentence per item.

## Prompt Requirements

The new prompts should force the model to produce fewer, better claims.

Key instructions:

- "Find what a smart reader would not know before listening."
- "Prefer mechanisms, examples, numbers, and tradeoffs."
- "Do not summarize chronologically unless the episode's argument is
  chronological."
- "Do not praise the episode; extract its usable value."
- "Every important claim must map to evidence."
- "For long podcasts, identify the argument spine before writing."

## Suggested Implementation Steps

1. Add a `scripts/digest_evidence_pipeline.py` module.
2. Reuse `generate_daily_report.py` output contract, but replace the internal
   `summarize_item_contract` implementation.
3. Add intermediate JSON artifacts under `reports/evidence/`:
   - `item_segments_{video_id}.json`
   - `item_evidence_{video_id}.json`
   - `item_ranked_insights_{video_id}.json`
4. Keep current final report schema so Feishu rendering does not need to change.
5. Add a local evaluation set with 3 examples:
   - one short 10-20 minute video
   - one 45 minute podcast
   - one 2 hour Bloomberg-style show
6. Compare old vs new output manually before changing the scheduled workflow.

## Acceptance Criteria

For every long-form item:

- `30 秒结论` says something more specific than the title.
- `为什么值得看` gives a real reason to spend time.
- `完整摘要` preserves the episode's argument spine.
- `核心要点` are claims, not generic topics.
- `关键事实` includes concrete facts when present.
- `分歧与限制` is not empty when the episode includes tradeoffs.
- `你可以怎么用` contains usable reader takeaways.

For the whole daily report:

- Top recommendations are selected by insight value.
- Short clips below 5 minutes are excluded.
- Long podcasts do not collapse into a generic paragraph.
- No item is published from title/description alone when transcripts are
  required.
