#!/usr/bin/env python
"""Install the two link-cataloguing subscriptions.

A subscription is the only thing in this system that grants the agent authority.
There are two here rather than one because the two halves of my life have
different rules, and authority is scoped per subscription:

  links-private   the microphone.                      provider: gemini (see note)
                  This is the tier intended to run on a local model, and the
                  `provider` field is what pins it. On this laptop qwen2.5:7b
                  judges well -- its verdicts quote the instruction correctly --
                  but it cannot drive the planner: it emits patches with invented
                  fields, so every run fails validation while the judgement
                  itself is sound. Rather than demo a broken run, both tiers are
                  on the hosted model here. The control is real and enforced on
                  the gate *and* the run; only the value differs.

  links-public    discord, imap, whatsapp, telegram.   provider: gemini
                  Non-personal sources, so a hosted model is acceptable. WhatsApp
                  sits here deliberately: the Twilio sandbox only ever carries
                  messages sent *to* the bot, so there is no private content to
                  protect -- and its 15s webhook timeout cannot absorb a local
                  model taking minutes. Real spend ceilings apply and can be
                  quoted from GET /v1/cost/by_principal.

The `provider` field is a ceiling on *disclosure* rather than on spend: it says
which model this subscription's content is allowed to reach at all. It is
enforced for the relevance gate as well as the run, because a gate that reads
the event has already disclosed it.

Usage:
    python tools/put_subscriptions.py            # install both
    python tools/put_subscriptions.py --show     # print what is installed
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

PRIVATE_INSTRUCTION = (
    "You are cataloguing links shared in my private conversations. For each link, decide "
    "whether it is worth keeping.\n"
    "KEEP: design assets; documents, pull requests or issues that name me or ask something "
    "of me; articles I said I wanted to read; anything someone explicitly asked me to look at.\n"
    "DO NOT KEEP: meeting invites (zoom, meet, teams, calendly); links shared as banter or "
    "small talk; expired or single-use links; tracking and unsubscribe URLs; anything already "
    "catalogued.\n"
    "Never fetch the URL itself. Judge only from the surrounding message text -- fetching would "
    "disclose the link to its host and to DNS, which defeats the point of running this "
    "subscription on a local model.\n"
    "THE GOAL YOU WRITE DECIDES WHAT HAPPENS NEXT, so choose between exactly two shapes.\n"
    "(a) NORMAL CASE -- the link is clearly worth keeping and nobody asked for my opinion. "
    "Goal: 'Use write_file to record <url> in the catalogue with a one-line reason.' Never open, "
    "fetch or summarise the page, and never ask for its contents: the message has all you need.\n"
    "(b) ASK-ME CASE -- the sender explicitly asks me to decide, says it is my call, or says "
    "they are unsure. This overrides (a): you must NOT file it yourself. "
    "Goal: 'Use request_approval to ask the owner whether to file <url>, with choices yes and no.' "
    "The only action this goal permits is request_approval. Do not write any file.\n"
    "Otherwise, if you are unsure, do not keep it: record it as skipped and give one short "
    "sentence of why."
)

PUBLIC_INSTRUCTION = (
    "You are cataloguing links shared in my public and semi-public channels. For each link, "
    "decide whether it is worth keeping.\n"
    "KEEP: design assets; shared documents; pull requests and issues; substantive articles.\n"
    "DO NOT KEEP: meeting invites (zoom, meet, teams, calendly); banter links; newsletter "
    "tracking, pixel and unsubscribe URLs; expired links; anything already catalogued.\n"
    "THE GOAL YOU WRITE DECIDES WHAT HAPPENS NEXT, so choose between exactly two shapes.\n"
    "(a) NORMAL CASE -- the link is clearly worth keeping and nobody asked for my opinion. "
    "Goal: 'Use write_file to record <url> in the catalogue with a one-line reason.' Never open, "
    "fetch or summarise the page, and never ask for its contents: the message has all you need.\n"
    "(b) ASK-ME CASE -- the sender explicitly asks me to decide, says it is my call, or says "
    "they are unsure. This overrides (a): you must NOT file it yourself. "
    "Goal: 'Use request_approval to ask the owner whether to file <url>, with choices yes and no.' "
    "The only action this goal permits is request_approval. Do not write any file.\n"
    "Otherwise, if you are unsure, do not keep it: record it as skipped and give one short "
    "sentence of why."
)

#: Identities the agent itself speaks as. An event whose actor is one of these
#: is refused as self-caused. This must include every address or handle the
#: agent can *write* to a watched channel from -- notably the IMAP bot address:
#: if the watched mailbox is also the sending mailbox, each reply lands back in
#: the inbox as new unseen mail and the agent answers its own answer forever,
#: bounded only by max_runs_per_day. The runtime's own name is not enough,
#: because the actor recorded for a channel event is the sender's address.
#: The mail address is read from the environment rather than written here, so
#: this file carries no personal identifier. Set S16_SELF_EMAIL (or IMAP_BOT_FROM)
#: to whatever address the agent replies from.
SELF_ACTORS = [
    actor for actor in (
        "s16code",
        os.getenv("S16_SELF_EMAIL", "").strip() or os.getenv("IMAP_BOT_FROM", "").strip(),
    ) if actor
]

SUBSCRIPTIONS = [
    {
        "id": "links-private",
        "instruction": PRIVATE_INSTRUCTION,
        "event_types": ["link.shared"],
        # Source strings are minted by the channel route as "<channel>:<conversation>",
        # which is the only conversation-level scoping available -- the gateway
        # filters by sender and has no room-level allowlist at all.
        "sources": ["local_mic:*"],
        "tenant_id": "personal-ea",
        "provider": "gemini",
        # Ask-first, and nothing else. remember_explicit_fact is deliberately
        # absent: it writes to scoped memory, which embeds the text against the
        # same local Ollama instance already serving completions. Queued behind a
        # chat call it exceeds the embedder's hard 30s socket timeout
        # (core/memory/embeddings.py:41) and the whole decision fails with
        # "TimeoutError: timed out". The catalogue is rendered from the recorded
        # decisions, not from memory, so this costs nothing.
        "allowed_side_effects": ["request_approval", "write_file"],
        # No `budget` or `daily_budget`: a local model projects $0.00, so a dollar
        # ceiling would be decoration. These two are the ceilings that actually bind.
        "max_runs_per_day": 40,
        "daily_triage_budget": 0.05,
        "ignore_actors": SELF_ACTORS,
    },
    {
        "id": "links-public",
        "instruction": PUBLIC_INSTRUCTION,
        "event_types": ["link.shared"],
        "sources": ["discord:*", "imap:*", "whatsapp:*", "telegram:*"],
        "tenant_id": "personal-ea",
        "provider": "gemini",
        "allowed_side_effects": ["request_approval", "write_file"],
        # Roughly 4x the measured cost of one triage+judge on flash, so a single
        # unusually long thread cannot silently blow through it.
        "budget": 0.02,
        # ~25 real judgements a day at observed cost, which is well above my
        # actual link volume and well below anything I would not notice.
        "daily_budget": 0.50,
        "max_runs_per_day": 60,
        # The cost of *watching*, bounded separately from the cost of doing --
        # a flood must not be able to bill me through the gate itself.
        "daily_triage_budget": 0.10,
        "ignore_actors": SELF_ACTORS,
    },
]


def token() -> str:
    value = os.getenv("S16_CONTROL_TOKEN", "").strip()
    if value:
        return value
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("S16_CONTROL_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit("S16_CONTROL_TOKEN not set and not found in S16Code/.env")


def call(base: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"authorization": f"Bearer {token()}", "content-type": "application/json"},
        method="PUT" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        sys.exit(f"{path} -> HTTP {error.code}: {error.read()[:400].decode(errors='replace')}")
    except urllib.error.URLError as error:
        sys.exit(f"cannot reach S16 at {base}: {error.reason}")


def describe(sub: dict) -> str:
    ceilings = " ".join(
        f"{k}={sub[k]}" for k in
        ("budget", "daily_budget", "max_runs_per_day", "daily_triage_budget") if sub.get(k))
    provider = sub.get("provider") or "(default)"
    return (f"  {sub['id']:<15} provider={provider:<10}"
            f" sources={','.join(sub['sources'])}\n"
            f"  {'':<15} may={','.join(sub['allowed_side_effects'])}\n"
            f"  {'':<15} {ceilings}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("S16_BASE", "http://127.0.0.1:8113"))
    parser.add_argument("--show", action="store_true", help="print installed subscriptions only")
    args = parser.parse_args()

    if not args.show:
        for sub in SUBSCRIPTIONS:
            result = call(args.base, f"/v1/agent/subscriptions/{sub['id']}", sub)
            print(f"installed {sub['id']}: accepted={result.get('accepted')}")
        print()

    for sub in call(args.base, "/v1/agent/subscriptions").get("subscriptions", []):
        print(describe(sub))
        print()


if __name__ == "__main__":
    main()
