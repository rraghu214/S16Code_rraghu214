#!/usr/bin/env python
"""Render the link catalogue from what the agent actually decided.

Why a renderer and not a model-written file
-------------------------------------------
The agent could be told to maintain a markdown file itself, and it would --
differently each run. Grouping, ordering and phrasing would drift, and a file
that looks different every time is a bad thing to put on camera and a worse
thing to trust. So the judgement stays the model's (filed or skipped, and why)
and the presentation stays deterministic: this reads the recorded decisions from
the event store and rewrites the document from them. Run it twice on the same
data and you get the same bytes.

It reads only. It cannot file, unfile or re-judge anything.

Usage
-----
    python tools/render_catalog.py                     # write the catalogue
    python tools/render_catalog.py --hours 24          # only the last day
    python tools/render_catalog.py --stdout            # print, don't write
    python tools/render_catalog.py --watch 30          # rewrite every 30s
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

# Host -> section. Deterministic, so the same link always lands in the same
# place; the agent's own reason is shown alongside as the annotation.
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Design", ("figma.com", "sketch.com", "dribbble.com", "canva.com", "excalidraw.com")),
    ("Documents", ("docs.google.com", "notion.so", "notion.site", "sharepoint.com",
                   "dropbox.com", "drive.google.com", "onedrive.live.com")),
    ("Code & issues", ("github.com", "gitlab.com", "bitbucket.org", "stackoverflow.com")),
    ("Reading", ("arxiv.org", "medium.com", "substack.com", "oreilly.com",
                 "news.ycombinator.com", "youtube.com", "youtu.be")),
]
FALLBACK_SECTION = "Everything else"


def section_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for name, hosts in SECTIONS:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return FALLBACK_SECTION


def load_token() -> str:
    token = os.getenv("S16_CONTROL_TOKEN", "").strip()
    if token:
        return token
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("S16_CONTROL_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def fetch_events(base: str) -> list[dict]:
    request = urllib.request.Request(f"{base}/v1/agent/events?after=0")
    token = load_token()
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("events", [])
    except urllib.error.URLError as error:
        raise SystemExit(f"cannot reach S16 at {base}: {error}") from error


def collect(events: list[dict], since: datetime | None):
    """Split recorded link decisions into filed, skipped and blocked."""
    filed: dict[str, list[dict]] = defaultdict(list)
    skipped: list[dict] = []
    blocked: list[dict] = []
    seen_urls: set[str] = set()
    duplicates = 0

    for record in events:
        event = record.get("event", {})
        if event.get("type") != "link.shared":
            continue
        data = event.get("data", {})
        url = data.get("url")
        if not url:
            continue
        occurred = event.get("occurred_at", "")
        if since is not None and occurred:
            try:
                if datetime.fromisoformat(occurred) < since:
                    continue
            except ValueError:
                pass

        entry = {
            "url": url,
            "source": event.get("source", "?"),
            "sender": data.get("sender", "?"),
            "when": occurred[:10],
            # split() on no argument collapses every whitespace run, including the
            # CRLFs an email body carries. Replacing only "\n" leaves the "\r"
            # behind, which renders as a hard break and shatters the bullet.
            "note": " ".join((data.get("text") or "").split())[:110],
        }

        # A URL filed once is not filed again. The dedupe is the whole point of
        # the exercise, so it is counted and reported rather than hidden.
        if url in seen_urls:
            duplicates += 1
            continue

        decisions = record.get("decisions", [])
        if not decisions:
            continue
        for decision in decisions:
            if decision.get("refused_by"):
                blocked.append({**entry, "why": decision.get("refusal_reason") or decision["refused_by"]})
            elif decision.get("relevant"):
                seen_urls.add(url)
                filed[section_for(url)].append({**entry, "why": (decision.get("reason") or "")[:130]})
            else:
                skipped.append({**entry, "why": (decision.get("reason") or "")[:130]})
            break
    return filed, skipped, blocked, duplicates


def render(filed, skipped, blocked, duplicates, window: str) -> str:
    total = sum(len(v) for v in filed.values())
    lines = [
        "# Link catalogue",
        "",
        f"*Rendered {datetime.now(UTC).strftime('%d %b %Y %H:%M UTC')} · {window}*",
        "",
        f"**{total} filed · {len(skipped)} skipped · {len(blocked)} blocked by a ceiling "
        f"· {duplicates} duplicates dropped**",
        "",
    ]

    order = [name for name, _ in SECTIONS] + [FALLBACK_SECTION]
    for name in order:
        items = filed.get(name)
        if not items:
            continue
        lines.append(f"## {name} ({len(items)})")
        lines.append("")
        for item in sorted(items, key=lambda i: i["when"], reverse=True):
            lines.append(f"- <{item['url']}>")
            lines.append(f"  `{item['source']}` · {item['sender']} · {item['when']}")
            if item["note"]:
                lines.append(f"  > {item['note']}")
            if item["why"]:
                lines.append(f"  *filed because:* {item['why']}")
            lines.append("")

    # The skipped section is the point of the whole build, not an appendix: it
    # is the evidence the agent is judging rather than hoarding.
    lines += [f"## Skipped ({len(skipped)})", ""]
    if not skipped:
        lines.append("_nothing was skipped in this window_")
    for item in sorted(skipped, key=lambda i: i["when"], reverse=True):
        lines.append(f"- <{item['url']}> — {item['why'] or 'no reason recorded'}")
    lines.append("")

    if blocked:
        lines += [f"## Blocked by a ceiling ({len(blocked)})", "",
                  "_A control stopped these before they were judged. This leaves no other trace._", ""]
        for item in blocked:
            lines.append(f"- <{item['url']}> — {item['why']}")
        lines.append("")
    return "\n".join(lines)


def build(base: str, hours: int | None, out: pathlib.Path | None) -> str:
    since = datetime.now(UTC) - timedelta(hours=hours) if hours else None
    window = f"last {hours}h" if hours else "all time"
    document = render(*collect(fetch_events(base), since), window=window)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8")
    return document


def main() -> None:
    default_out = os.getenv("S16_SANDBOX_ROOT") or str(
        pathlib.Path(__file__).resolve().parents[1] / "sandbox")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("S16_BASE", "http://127.0.0.1:8113"))
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--out", default=str(pathlib.Path(default_out) / "link-catalogue.md"))
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS")
    args = parser.parse_args()

    out = None if args.stdout else pathlib.Path(args.out)
    while True:
        document = build(args.base, args.hours, out)
        if args.stdout:
            print(document)
        else:
            print(f"[catalogue] {out}  ({len(document.splitlines())} lines)")
        if not args.watch:
            return
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
