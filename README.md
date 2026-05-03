# Wispr Thoughts

> a time machine through everything you've voiced.

wispr thoughts is an open-source project that takes everything you've ever dictated through wispr flow, groups it by week, and makes it searchable and easy to converse with. ask yourself what you were working on, what you keep almost-saying, or what you've been quietly avoiding.

everything runs locally on your laptop. nothing syncs to any cloud, no account, no telemetry. you fully own your data.

![Wispr Thoughts weekly viewer](assets/viewer.png)

## What you can ask it

> "read all my themes from january. what was i working on then? in hindsight, were those actually problems worth solving?"

> "what do i keep almost-saying but never quite getting to? half-finished thoughts, themes that surface and disappear."

> "if you only saw my last 90 days of dictation and meetings, what would you say i'm avoiding?"

> "pretend you're an advisor who only has my voice. based on the last month, what's the one piece of advice you'd give?"

> "looking across everything i've said, what does it reveal about my real priorities, not my stated ones? where do my words and my actual time disagree?"

> "if a board member or co-founder only had my voice and meetings to evaluate me, what would they conclude about my strategic clarity, focus, and execution quality?"

The local viewer is the primary output, and everything in the pipeline runs through your existing Claude Code subscription. **No signup, no account, no Anthropic API key, and no email setup required.**

## Get started

Try it first with synthetic data. No accounts, no setup, 30 seconds:

```bash
git clone https://github.com/pvilk/wispr-thoughts.git
cd wispr-thoughts
./demo.sh
```

Your browser opens to a sample week from a fictional founder named Alice. If that works, hook up your own corpus. With [Claude Code](https://claude.ai/code) installed, run `claude` in the repo and it'll walk you through detection of your data sources, an optional Fathom API key, and the first week of theming. Or use the shell bootstrap (`./bootstrap.sh`) for the same flow through terminal prompts.

You end up with two commands installed in `~/bin`: `viewer` to open the local digest from any terminal, and `digest` to assemble next week's edition manually. Setup takes about three minutes from a fresh clone.

## What you get

A local HTML viewer at `http://127.0.0.1:8080` with calendar navigation, weekly themes that merge solo dictation with meeting transcripts, and a cross-week auditor that surfaces stuck threads, drift clusters, and avoidance patterns. The richer the data, the sharper the patterns.

| Source | What it adds | Status |
|---|---|---|
| Wispr Flow | Solo voice dictation from your local SQLite | Working today |
| Fathom | Cloud-based meeting transcripts via API | Working today |
| Granola | Local Mac app's cached meetings | Working today |
| Apple Notes | Writing from your phone, iPad, and Mac, all converged via iCloud | Exporter ships, kept as a parallel corpus |
| Mobile Wispr | Phone dictations not synced to desktop | Investigating |
| Slack (sent only) | Your work-voice, shorter and more direct than dictation | Planned |
| Gmail (sent only) | Your written long-form voice in email | Planned |
| iMessage | Your closest-relationship private speech | Planned |
| Apple Calendar | What you spent time on, titles and durations only | Planned |
| GitHub commits | What you actually built each week | Planned |

Adding a source is one Python script that produces per-day or per-meeting markdown into the `data/` directory. The theme extraction and auditor passes work over whatever's there. If you want a specific source prioritized, [open an issue](https://github.com/pvilk/wispr-thoughts/issues).

## Privacy

Everything stays on your machine by default. Wispr SQLite, Granola data, exported Apple Notes, per-day exports, themed weeks, the assembled digest, and the local viewer all live under `data/` which is gitignored. The only thing that leaves your machine is the LLM call, which goes through your existing Claude Code subscription via the local `claude -p` command. No separate API key, no account, no cloud database, no telemetry.

A pre-commit hook ships with the repo and refuses any commit containing terms from a personal denylist, paths under `data/`, or credential-shaped strings. Even if you accidentally paste a name or brand into a comment, the commit is blocked before it can leak.
