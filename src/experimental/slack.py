#!/usr/bin/env python3
"""Parse a Slack workspace export (zip) into one Markdown file per channel/DM.

Stub adapter for a future Slack source. Not wired into the digest pipeline
yet. Run standalone if you've already got a Slack workspace export and want
to bring your own messages into the corpus.

A Slack workspace export is a zip of:
    users.json
    channels.json (or groups.json, mpims.json, dms.json depending on what was
                   exported)
    <channel-name>/YYYY-MM-DD.json   (one JSON per day per channel)

Input:  ./export.zip   (or ./export/ if already extracted)
Output: ./conversations/*.md   (one per channel or DM)
        ./INDEX.md
        ./me_only.md (every message you sent, in chronological order)

Usage:
    python3 parse_export.py
    python3 parse_export.py --in ./export.zip --out ./conversations --me U_YOUR_ID

Find your Slack user ID in your Slack profile -> ... -> Copy member ID.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Default Slack user ID. Override with --me when running. Format is `U` or
# `W` followed by uppercase alphanumerics.
ME_DEFAULT = "U_YOUR_SLACK_USER_ID"


# --- Slack mrkdwn → markdown -------------------------------------------------

USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
CHANNEL_MENTION = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]+))?>")
LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
SUBTEAM = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]+))?>")
SPECIAL = re.compile(r"<!(here|channel|everyone)>")


def render_text(text: str, users: dict[str, str], channels: dict[str, str]) -> str:
    text = USER_MENTION.sub(lambda m: f"@{users.get(m.group(1), m.group(2) or m.group(1))}", text)
    text = CHANNEL_MENTION.sub(lambda m: f"#{channels.get(m.group(1), m.group(2) or m.group(1))}", text)
    text = LINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = SUBTEAM.sub(lambda m: f"@{m.group(1) or 'group'}", text)
    text = SPECIAL.sub(lambda m: f"@{m.group(1)}", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


# --- Loaders -----------------------------------------------------------------

def load_export(in_path: Path, me_id: str) -> dict:
    """Return {users:{id:name}, channels:{id:name}, messages_by_channel:{cid:[msg]}, channel_meta:{cid:{...}}}."""
    if in_path.is_file() and in_path.suffix == ".zip":
        zf = zipfile.ZipFile(in_path)
        names = zf.namelist()
        def read_json(name):
            with zf.open(name) as f:
                return json.load(f)
        def list_files(prefix):
            return [n for n in names if n.startswith(prefix) and n.endswith(".json")]
    elif in_path.is_dir():
        def read_json(name):
            return json.loads((in_path / name).read_text(encoding="utf-8"))
        def list_files(prefix):
            base = in_path / prefix.rstrip("/")
            if not base.is_dir():
                return []
            return [str(p.relative_to(in_path)) for p in base.glob("*.json")]
    else:
        raise SystemExit(f"input not found or not a zip/dir: {in_path}")

    # Users
    users = {}
    try:
        for u in read_json("users.json"):
            users[u["id"]] = u.get("real_name") or u.get("name") or u["id"]
    except Exception as e:
        print(f"  no users.json ({e})")

    # Channels (public, private, mpim, dm)
    channels = {}  # id -> display name
    channel_meta = {}  # id -> {kind, members, dir}
    for fname, kind in [("channels.json", "channel"), ("groups.json", "private"),
                        ("mpims.json", "mpim"), ("dms.json", "dm")]:
        try:
            for c in read_json(fname):
                cid = c["id"]
                if kind == "dm":
                    other = next((m for m in c.get("members", []) if m != me_id), c.get("user", ""))
                    name = f"dm-{users.get(other, other)}"
                elif kind == "mpim":
                    members = [users.get(m, m) for m in c.get("members", []) if m != me_id]
                    name = "mpim-" + "-".join(members[:3])
                else:
                    name = c.get("name", cid)
                channels[cid] = name
                channel_meta[cid] = {"kind": kind, "name": name}
        except Exception:
            continue

    # Messages: each channel has its own subdir with daily JSON files
    messages_by_channel: dict[str, list[dict]] = defaultdict(list)
    for cid, meta in channel_meta.items():
        # Per-channel directory in export named after channel name (or just id for DMs)
        # Export structure varies; try a few prefixes.
        candidates = [meta["name"] + "/", cid + "/"]
        for prefix in candidates:
            files = list_files(prefix)
            if not files:
                continue
            for fname in sorted(files):
                try:
                    day_msgs = read_json(fname)
                    if isinstance(day_msgs, list):
                        messages_by_channel[cid].extend(day_msgs)
                except Exception:
                    continue
            if messages_by_channel[cid]:
                break

    return {"users": users, "channels": channels, "channel_meta": channel_meta, "messages_by_channel": messages_by_channel}


# --- Rendering ---------------------------------------------------------------

def ts_to_dt(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def render_channel(cid: str, name: str, kind: str, msgs: list[dict], users: dict, channels: dict, me: str) -> tuple[str, int]:
    msgs.sort(key=lambda m: float(m.get("ts", "0")))
    lines = [
        "---",
        f'channel: "{name}"',
        f'channel_id: {cid}',
        f'kind: {kind}',
        f'message_count: {len(msgs)}',
        "---",
        "",
        f"# {name}",
        "",
    ]
    my_count = 0
    for i, m in enumerate(msgs):
        if m.get("subtype") in ("channel_join", "channel_leave", "channel_topic", "channel_purpose", "bot_message"):
            continue
        uid = m.get("user", "")
        if not uid:
            continue
        is_me = uid == me
        if is_me:
            my_count += 1
        text = m.get("text", "")
        if not text and m.get("attachments"):
            text = "\n".join(a.get("text", "") for a in m["attachments"] if a.get("text"))
        if not text and m.get("blocks"):
            text = blocks_to_text(m["blocks"])
        text = render_text(text or "", users, channels)
        if not text.strip():
            continue
        spk = "me" if is_me else "other"
        speaker = "Me" if is_me else users.get(uid, uid)
        v = f"s{cid}.m{i:04d}.{spk}"
        ts_str = ts_to_dt(m["ts"]).strftime("%Y-%m-%d %H:%M") if m.get("ts") else "?"
        thread_marker = " [thread]" if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else ""
        lines.append(f"**{speaker}** · `{v}` · {ts_str}{thread_marker}")
        for line in text.splitlines():
            lines.append(f"> {line}" if line.strip() else ">")
        lines.append("")
    return "\n".join(lines), my_count


def blocks_to_text(blocks) -> str:
    out = []
    for b in blocks or []:
        if b.get("type") == "rich_text":
            for el in b.get("elements", []):
                for sub in el.get("elements", []):
                    if sub.get("type") == "text":
                        out.append(sub.get("text", ""))
                    elif sub.get("type") == "link":
                        out.append(sub.get("text") or sub.get("url", ""))
                    elif sub.get("type") == "user":
                        out.append(f"<@{sub.get('user_id')}>")
    return "".join(out)


def render_my_messages(my_msgs: list[dict], users, channels) -> str:
    my_msgs.sort(key=lambda m: float(m["ts"]))
    lines = ["# Your Slack messages (chronological)", "",
             f"_{len(my_msgs):,} messages you sent across all channels and DMs._", ""]
    cur_day = None
    for m in my_msgs:
        dt = ts_to_dt(m["ts"])
        day = dt.strftime("%Y-%m-%d")
        if day != cur_day:
            lines.append(f"\n## {day}\n")
            cur_day = day
        text = render_text(m.get("text", "") or "", users, channels)
        if not text.strip():
            continue
        time_str = dt.strftime("%H:%M")
        cv = m.get("vertex", "")
        lines.append(f"- `{cv}` · {time_str} · _{m.get('channel_name', '')}_ · {text[:300]}")
    return "\n".join(lines)


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="export.zip")
    ap.add_argument("--out", default="conversations")
    ap.add_argument("--me", default=ME_DEFAULT, help=f"Your Slack user ID (default: {ME_DEFAULT})")
    args = ap.parse_args()

    inpath = Path(args.infile)
    if not inpath.exists():
        raise SystemExit(
            f"export not found: {inpath}\n\n"
            "Slack workspace export: https://<your-workspace>.slack.com/services/export\n"
            "(requires admin). Drop the resulting .zip here as 'export.zip'."
        )

    outpath = Path(args.out)
    outpath.mkdir(parents=True, exist_ok=True)

    me = args.me
    print(f"loading {inpath}...")
    data = load_export(inpath, me)
    print(f"  {len(data['users'])} users · {len(data['channels'])} channels/DMs · "
          f"{sum(len(v) for v in data['messages_by_channel'].values()):,} messages")

    manifest = []
    my_messages = []
    for cid, msgs in data["messages_by_channel"].items():
        meta = data["channel_meta"][cid]
        if not msgs:
            continue
        # Safe filename
        safe = re.sub(r"[^a-z0-9-]", "-", meta["name"].lower()).strip("-") or cid
        fname = f"{meta['kind']}_{safe}_{cid}.md"
        rendered, my_count = render_channel(cid, meta["name"], meta["kind"], msgs, data["users"], data["channels"], me)
        (outpath / fname).write_text(rendered, encoding="utf-8")
        manifest.append({
            "channel": meta["name"], "kind": meta["kind"],
            "file": fname, "msgs": len(msgs), "my_msgs": my_count,
        })
        # Collect your messages for combined corpus
        for i, m in enumerate(msgs):
            if m.get("user") == me and m.get("text"):
                my_messages.append({**m, "vertex": f"s{cid}.m{i:04d}.me", "channel_name": meta["name"]})

    # Index
    manifest.sort(key=lambda x: -x["my_msgs"])
    total_mine = sum(m["my_msgs"] for m in manifest)
    lines = ["# Slack Archive", "",
             f"Total channels/DMs: {len(manifest)} · Your messages: {total_mine:,}", "",
             "| Channel | Kind | Total msgs | Your msgs | File |", "|---|---|---:|---:|---|"]
    for m in manifest:
        lines.append(f"| {m['channel']} | {m['kind']} | {m['msgs']} | {m['my_msgs']} | [{m['file']}](conversations/{m['file']}) |")
    (outpath.parent / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")

    # Your messages, chronological
    (outpath.parent / "me_only.md").write_text(render_my_messages(my_messages, data["users"], data["channels"]), encoding="utf-8")

    print(f"\ndone: {len(manifest)} conversations written")
    print(f"  Your chronological feed: {outpath.parent / 'me_only.md'} ({len(my_messages):,} msgs)")


if __name__ == "__main__":
    main()
