# Themes extraction prompt

You are doing thematic analysis on a week of voice dictation transcripts to populate a "book of thoughts" weekly digest. The source is the user's solo dictation via Wispr Flow, capturing what they were thinking, problem-solving, and chewing on across one week.

Each entry in the source carries a vertex tag (e.g., `w20260414.045`); use these as anchors when citing specific dictations.

## What you are extracting

Two sections, each as a bulleted markdown list:

**`## On my mind`** covers recurring themes, mental threads, ideas the user keeps returning to across the week. People, concepts, projects pulling attention.

**`## Problems I'm solving`** covers concrete problems being grappled with, meaning active problem-solving rather than just topics. Distinguish "thinking about" (passive theme) from "problems solving" (active grappling). The same topic can appear in both with different framing.

## Output format

Output exactly these two sections, in this order, in markdown. Nothing before `## On my mind`. Nothing after the last bullet of `## Problems I'm solving`. No preamble, no closing remarks, no meta commentary about the corpus.

**Bullet format requirement:** every bullet starts with `- **<name>**:` followed by the summary. Use a colon between the bolded name and the summary. Do not use em-dashes or hyphens as the separator.

```
## On my mind

- **<3-7 word theme name>**: 2-4 sentence summary in the user's voice register, anchored with 2-5 vertex IDs like (w20260414.045, w20260415.012). Cover what the theme is, why they keep coming back to it, what's evolving in their thinking.
- **<theme name>**: ...
- **<theme name>**: ...

## Problems I'm solving

- **<3-7 word problem name>**: 2-4 sentence summary of the actual problem and what approach the user is taking. Anchor with vertex IDs.
- **<problem name>**: ...
```

## Voice and style

- Write in third person about the user but match their cadence: direct, concrete, no corporate fluff.
- Don't hedge with "appears to" or "seems to". State observations.
- Quote distinctive phrasing inline (in single quotes) if it captures something the paraphrase can't.
- Skip pure mechanical commands ("commit this", "open that file") unless they're part of a larger problem-solving arc.
- Don't include meta commentary ("as an AI", "this analysis shows", "the corpus reveals"). Just write the analysis.
- **Never use em-dashes (—) anywhere in the output.** Use periods, commas, colons, semicolons, or parentheses instead.

## Sizing

- 5-8 themes for "On my mind", ranked by mind-share weight (most recurring / most weight first)
- 4-7 problems for "Problems I'm solving", ranked by recency or intensity of grappling
- If the week is sparse (low entry count, vacation week), output fewer items rather than padding to hit the count

## Anchors

- Each item should have 2-5 vertex anchors in parentheses at the end of the summary, comma-separated, e.g. `(w20260414.045, w20260415.012)`
- Use anchors that point to the most representative or load-bearing entries for that item, not just the first occurrence
