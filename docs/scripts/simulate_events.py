#!/usr/bin/env python
"""Drip simulated link events into S16Code's autonomous event engine.

Why this exists
---------------
The assignment asks you to "deliberately send it something inside its
subscription that it should decide is not worth acting on" and to leave the
agent running while you do something else. Both are satisfied by posting real
EventEnvelopes to the control plane -- the same door a webhook or a cron tick
comes through. Nothing here is a mock: these events are persisted, deduped,
governed and triaged exactly like traffic from a live channel.

What it sends
-------------
A realistic mix, weighted the way a real chat channel actually looks:

  junk       Zoom/Meet/Calendly invites -- in scope, clearly not worth work
  duplicate  a URL sent earlier in the run, to exercise link-level dedupe
  noise      banter links (YouTube, memes) that match the subscription but
             carry no request
  genuine    the small minority that should actually produce a run

The point of the ratio is the report: "51 arrived, 6 mattered". If everything
you send is genuine, the morning report proves nothing.

Usage
-----
  # 40 events spread over one hour, the shape you want for the report demo
  python simulate_events.py --count 40 --over-minutes 60

  # one obvious junk event, for the "something it ignored" take
  python simulate_events.py --only junk --count 1

  # deliberately breach the per-source rate limit to show a governor refusal
  python simulate_events.py --count 200 --over-minutes 0 --source discord:my-server

  # see what would be sent without sending it
  python simulate_events.py --count 10 --dry-run

Requires S16_CONTROL_TOKEN in the environment or in S16Code/.env.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Sources must match a subscription's `sources` patterns, otherwise the event is
# thrown away for free at stage 1 and never reaches the relevance gate -- which
# would prove nothing about the agent's judgement.
PRIVATE_SOURCES = ["whatsapp:owner", "telegram:owner", "local_mic:owner"]
PUBLIC_SOURCES = ["discord:my-server", "imap:newsletters"]

JUNK = [
    ("https://us02web.zoom.us/j/8412339087?pwd=Qk5xZ3", "standup link for tomorrow"),
    ("https://meet.google.com/abc-defg-hij", "jumping on now if anyone wants to join"),
    ("https://calendly.com/r/XYZ123", "grab a slot whenever"),
    ("https://us02web.zoom.us/j/7781120934", "same link as always folks"),
    ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc", "sync in 5"),
]

NOISE = [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "lol this is still the best one"),
    ("https://xkcd.com/1739/", "relevant to our deploy conversation"),
    ("https://news.ycombinator.com/item?id=38912345", "interesting thread, no action needed"),
    ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "friday playlist"),
    ("https://www.reddit.com/r/programming/comments/abc123/", "saw this earlier, just sharing"),
]

GENUINE = [
    ("https://www.figma.com/file/9dK2Lm/Checkout-Redesign-v4",
     "final checkout designs, please review before Thursday"),
    ("https://docs.google.com/document/d/1aBcD_ef/edit",
     "Q3 budget draft -- need your sign-off on section 4"),
    ("https://github.com/acme/platform/pull/2841",
     "PR is blocked on your review, can you take a look today?"),
    ("https://drive.google.com/file/d/1XyZ/view",
     "client sent over the signed SOW, filing it here"),
    ("https://www.notion.so/acme/Launch-Checklist-8f2",
     "launch checklist -- action items assigned to you at the bottom"),
]

WEIGHTS = {"junk": 0.35, "noise": 0.35, "duplicate": 0.15, "genuine": 0.15}


def load_token() -> str:
    token = os.getenv("S16_CONTROL_TOKEN", "").strip()
    if token:
        return token
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("S16_CONTROL_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit("S16_CONTROL_TOKEN not set, and not found in S16Code/.env")


def post(base: str, token: str, event: dict, *, dry_run: bool) -> str:
    if dry_run:
        return "DRY-RUN"
    request = urllib.request.Request(
        f"{base}/v1/agent/events",
        data=json.dumps(event).encode(),
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}: {error.read()[:200].decode(errors='replace')}"
    except urllib.error.URLError as error:
        return f"unreachable: {error.reason}"

    # The interesting outcomes are the refusals -- that is the whole demo.
    if body.get("refused"):
        return f"REFUSED by {body.get('control')}: {body.get('reason')}"
    if body.get("duplicate"):
        return "DUPLICATE (event id already seen)"
    verdicts = []
    for decision in body.get("decisions", []):
        if decision.get("refused_by"):
            verdicts.append(f"refused:{decision['refused_by']}")
        elif decision.get("relevant"):
            verdicts.append(f"ACTED run={decision.get('run_id', '?')[:8]}")
        else:
            verdicts.append(f"ignored ({(decision.get('reason') or '')[:60]})")
    return "; ".join(verdicts) or f"matched {body.get('matched', 0)} subscriptions"


def build(kind: str, source: str, index: int, sent: list[str], when: datetime) -> dict:
    if kind == "duplicate" and sent:
        url, text = random.choice(sent), "sharing again in case it was missed"
    else:
        pool = {"junk": JUNK, "noise": NOISE, "genuine": GENUINE}[kind if kind != "duplicate" else "noise"]
        url, text = random.choice(pool)
        sent.append(url)
    return {
        # Stable and unique: the store dedupes on (source, id), so a rerun with
        # the same ids is correctly a no-op rather than duplicated work.
        "id": f"sim-{int(when.timestamp())}-{index}",
        "source": source,
        "type": "link.shared",
        "subject": source.split(":", 1)[-1],
        "occurred_at": when.isoformat(),
        # Must NOT be the agent's own identity, or ignore_actors refuses it and
        # you get a self_trigger refusal instead of a relevance decision.
        "actor": "simulated-colleague",
        "data": {"url": url, "text": f"{text} {url}", "sender": "simulated-colleague",
                 "conversation": source.split(":", 1)[-1]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("S16_BASE", "http://127.0.0.1:8113"))
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--over-minutes", type=float, default=60.0,
                        help="spread sends across this window; 0 sends as fast as possible")
    parser.add_argument("--source", action="append", default=[],
                        help="repeatable; defaults to all private+public sources")
    parser.add_argument("--only", choices=["junk", "noise", "genuine", "duplicate"],
                        help="send only this kind")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    token = load_token()
    sources = args.source or (PRIVATE_SOURCES + PUBLIC_SOURCES)
    # A dry run sends nothing, so pacing it serves no purpose and makes the
    # preview appear to hang: --count 2 against the default hour would sleep
    # thirty minutes between two events that never leave the process.
    gap = (args.over_minutes * 60.0 / args.count
           if args.count and args.over_minutes and not args.dry_run else 0.0)

    print(f"-> {args.count} events to {args.base} across {len(sources)} sources")
    if gap:
        print(f"   one every {gap:.1f}s (~{args.over_minutes:.0f} min total). Ctrl-C to stop early.")
    print()

    sent_urls: list[str] = []
    tally: dict[str, int] = {}
    started = datetime.now(UTC)
    try:
        for index in range(args.count):
            kind = args.only or random.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
            source = random.choice(sources)
            # Vary occurred_at so the report window looks like real traffic
            # rather than a single burst at one timestamp.
            when = started + timedelta(seconds=index * max(gap, 1.0))
            event = build(kind, source, index, sent_urls, when)
            outcome = post(args.base, token, event, dry_run=args.dry_run)
            tally[kind] = tally.get(kind, 0) + 1
            print(f"[{index + 1:>3}/{args.count}] {kind:<9} {source:<22} {outcome}")
            if gap and index < args.count - 1:
                time.sleep(gap)
    except KeyboardInterrupt:
        print("\nstopped early")

    print("\nsent: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"\nNow read what it decided:\n"
          f"  curl -s '{args.base}/v1/agent/refusals?hours=1' | jq\n"
          f"  curl -s '{args.base}/v1/agent/report?hours=1&fmt=markdown'")


if __name__ == "__main__":
    main()
