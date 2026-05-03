# Unresolved threads detection

You are reviewing the last several weeks of the user's "Problems I'm solving" lists, extracted from their solo voice dictation. Identify problems that are stuck (carrying for 3+ consecutive weeks with no shift in framing) and problems that appeared and then vanished (likely resolved or abandoned).

## Input format

You will receive multiple week sections, each formatted as:

```
### 2026-W17 (Apr 19 to Apr 25)

- **Problem name**: summary text with vertex anchors (w20260419.001, ...)
- **Another problem**: ...
```

## Output format

Return exactly these three sections, in this order, in markdown. Output ONLY these sections; no preamble, no closing remarks, no meta commentary.

**Never use em-dashes (—) anywhere in the output.** Use periods, commas, colons, semicolons, or parentheses instead.

```
## Stuck threads (3+ weeks, no shift in framing)

- **<problem name>**: appeared in W14, W15, W16, W17. Framing has not shifted: <one-sentence observation of what hasn't moved>. Anchors: w20260414.045, w20260423.012.
- ...

## Resolved or abandoned

- **<problem name>**: last surfaced in W14, absent W15-W17. Likely resolved (the framing in W14 sounded close to a fix) / Likely abandoned (no resolution language). Anchors: w20260414.030.
- ...

## Persistent but evolving

- **<problem name>**: appears in W14, W16, W17 with shifting framing: started as <X>, now framed as <Y>. Worth noting because the user is making progress but hasn't closed it. Anchors: ...
- ...
```

If a section has no items, output the header followed by `_None this period._` on its own line.

## Detection guidance

- **Stuck thread:** A problem name (or close paraphrase) appears in 3+ consecutive weeks AND the summary text shows similar framing each time (same approach, same constraints, same blockers). The honest read is: the user has been chewing on this without changing the angle.
- **Resolved or abandoned:** A problem appears in earlier weeks and is absent from the most recent 2-3 weeks. Distinguish:
  - "Likely resolved": final week's summary contained resolution language ("landed at", "decided", "shipped", concrete outcome).
  - "Likely abandoned": no resolution language, just disappeared. Could be deprioritized or forgotten.
- **Persistent but evolving:** Problem recurs across 3+ weeks BUT framing visibly shifts. This is healthy iteration, not stuckness, but worth flagging because it's still open.

Use the vertex anchors from the source to ground your observations. Do not invent anchors; only use ones you saw in the input.

If fewer than 4 weeks of data are provided, prefix the output with one line: `_Note: <N> weeks of context available; carryover detection has limited signal._`
