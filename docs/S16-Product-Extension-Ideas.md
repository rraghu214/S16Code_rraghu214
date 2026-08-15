# Product extension — from bot sandbox to a real link dashboard

*Written 2026-08-14, after the Part 1 build. Nothing here is needed for the assignment; it is the "what this becomes if I keep going" note.*

---

## 1. The observation that started this

> *"Isn't this absurd that I'm unable to read my other chats in WhatsApp, Telegram etc. outside my Twilio chat or the bot chat? This would have been a great product if I could intercept all my messages and build a dashboard of all educational links from all sources — with the origin, who sent it, through which medium."*

That frustration is well-founded, and it is worth being precise about **what is an architectural choice and what is a genuine platform wall**, because they are different problems with different answers.

## 2. Why the current build feels like a sandbox

**`glc_v5` is bot-shaped.** Every one of its fifteen adapters models the same interaction: *a bot receives a message addressed to it, and replies*. `ChannelAdapter` (`glc/channels/base.py`) is literally two methods, `on_message` and `send`.

That shape has three consequences the assignment build inherits:

1. You only ever see messages sent **to the bot**, never your real conversations.
2. There is no history — the contract has no `fetch_history` and no cursor anywhere.
3. Identity is the *sender talking to the bot*, so grouping by "which of my chats did this come from" is not expressible.

None of that is a limitation of the *agent*. The subscriptions, the governor, the relevance gate and the catalogue only ever see `link.shared` events with a `source` string. **They do not care where those events came from.** That is the seam the whole extension hangs on.

## 3. Platform reality, checked

| Platform | Can it read your real chats? | Route | Verdict |
|---|---|---|---|
| **Telegram** | **Yes, fully** | MTProto **user** API — Telethon or Pyrogram. You authenticate as yourself, not as a bot: every private chat, group and channel, with full history | Official, supported, and the single best starting point |
| **Email** | **Yes, already** | IMAP is whole-account access. This build is scoped to one folder *by choice* (`mailbox` in `ImapConfig`), not by limitation | Already solved |
| **Slack** | **Yes** | A user token (`xoxp-`) plus `conversations.history` reads every channel you belong to, with pagination | Achievable; needs a real client, since glc's Slack adapter has none |
| **Discord** | **Servers yes, DMs no** | A bot reads every channel it has access to in servers it has joined. Personal DMs would need a selfbot | Servers fine; DMs off the table |
| **WhatsApp** | **No** | Meta's Cloud API only delivers messages sent *to a business number*. Personal chats are end-to-end encrypted with no official read API | The genuine wall |

### The WhatsApp wall, explicitly

Unofficial libraries that drive WhatsApp Web can read everything. They violate the Terms of Service and get numbers banned, and a product whose foundation can be removed by the platform at any moment is not a foundation. **Treat WhatsApp as delivery-only**: the assistant can message *you* there — digests, approvals, alerts — it just cannot read your conversations. That is a reasonable product boundary, not a defeat.

## 4. What the real product looks like

The pipeline built for the assignment survives unchanged. Only the ingest layer is replaced.

```
  today                                    extension
  ─────                                    ─────────
  bot message                              Telethon user session   (all chats, all history)
      │                                    Slack user token        (all channels, paginated)
      ▼                                    Discord bot             (all server channels)
  channel adapter                          IMAP, no folder scope   (whole mailbox)
      │                                            │
      └──────────────► link.shared events ◄────────┘
                              │
                   subscriptions + governor        ← unchanged
                    relevance gate, refusals       ← unchanged
                    catalogue renderer             ← unchanged
```

**What stays exactly as built:** the subscriptions and their ceilings, the disclosure ceiling (`Subscription.provider`), the two-stage filter, the refusal ledger, the morning report, the catalogue renderer. All of it keys off `source` and `data.url`.

**What changes:** one importer per platform, emitting the same envelope. The source string becomes meaningful — `telegram:eag-v3-cohort` rather than `telegram:<numeric-user-id>` — which is exactly what makes "who sent this, in which room" expressible for the first time. The subscription glob (`sources: ["telegram:*"]`) already handles it.

**What gets unlocked, that the bot shape cannot do at all:**

- **History.** Telethon can walk a group back months. That was the original ambition in this project and it was abandoned only because the adapters have no history contract — not because it is hard.
- **Real grouping.** Origin, room, sender, medium — the dashboard columns being asked for.
- **Deduplication that means something.** The same link shared in three groups by two people is one entry with three provenances, not three entries.

## 5. Build order, if picked up again

1. **Telegram via Telethon.** Highest value, fully legitimate, and covers both live watching and months of backfill in one client. Prove the whole idea here before touching anything else.
2. **Drop the IMAP folder scope** once the judging is trusted — the mailbox is already fully readable.
3. **Slack user token** for work channels.
4. **Discord bot** across your servers.
5. **A real dashboard.** The markdown catalogue was the right call under a deadline; grouping by origin and sender wants a table with filters. The renderer already separates judgement from presentation, so this is a new view over the same data, not a rewrite.

## 6. The thing worth keeping from the assignment build

Two-tier disclosure is more valuable at product scale than it is in the demo. Once an importer is reading *everything*, "which model is allowed to see this content" stops being a nice demo line and becomes the actual product promise: **your private groups are judged by a model on your own machine, and you can prove it from the call log.** That is a genuine differentiator for a tool whose whole premise is reading your messages.

## 7. Boundaries to keep

- No ToS-violating routes as product foundations — specifically no WhatsApp Web automation and no Discord selfbots.
- Private-tier content stays on a local model, and the private tier never fetches a URL: fetching discloses it to the destination host and to DNS, which defeats local inference.
- Read-only by default. The assistant catalogues and reports; it does not reply on your behalf without approval.
