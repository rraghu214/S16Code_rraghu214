# Runbook — Link Cataloguing EA

Start-up, reset, and verification for the Session 16 Part 1 build.
Five channels: **WhatsApp, Telegram, Discord, IMAP, local microphone.**

Two repos, both needed:

```
glc_v5/    the gateway   :8111   owns every credential and channel adapter
S16Code/   the agent     :8113   owns the decisions; holds no credentials
```

---

## 1. Cold start

Order matters. **Every bridge connects to the gateway first**, so a bridge started
before glc dies immediately with `ConnectionRefusedError [WinError 1225]`.

### Terminal 1 — Ollama

```powershell
ollama serve
ollama list          # qwen2.5:7b must be present
```

### Terminal 2 — the gateway

```powershell
cd C:\Raghu\MyLearnings\EAG_V3\S16-08082026\assignment\glc_v5
uv run glc serve
```
Verify: `curl.exe -s http://127.0.0.1:8111/healthz`

### Terminal 3 — the agent

```powershell
cd C:\Raghu\MyLearnings\EAG_V3\S16-08082026\assignment\S16Code
uv run s16code serve
```
Verify: `curl.exe -s http://127.0.0.1:8113/healthz`

> `/v1/agent/liveness` answering **503** with *"no heartbeat has ever been recorded"*
> is correct on a fresh store. It turns 200 after the first event.

### Terminal 4 — ngrok (WhatsApp only)

```powershell
ngrok http 8111
```
⚠️ The free subdomain **changes on every restart**. After restarting ngrok, paste
the new URL into the Twilio console (*Messaging → Try it out → WhatsApp sandbox
settings → When a message comes in*) as:

```
https://<new-sub>.ngrok-free.app/v1/channels/whatsapp/webhook
```

and update `TWILIO_WEBHOOK_URL` in `glc_v5/.env` to the **exact same string** —
Twilio signs over the full URL, so any mismatch fails validation silently.

### Terminals 5–7 — the channel bridges

All run from the **glc_v5** directory.

```powershell
cd C:\Raghu\MyLearnings\EAG_V3\S16-08082026\assignment\glc_v5

# Telegram
uv run python glc\channels\catalogue\telegram\dev\live_poll.py

# Discord
uv run python glc\channels\catalogue\discord\tests\run_discord_bridge.py

# IMAP
uv run python glc\channels\catalogue\imap\dev\live_poll.py
```

### Terminal 8 — microphone (start when you want to use it)

```powershell
uv run python glc\channels\catalogue\local_mic\dev\mic_client.py
```

WhatsApp needs no bridge process — it arrives by webhook through ngrok.

---

## 2. Reset to a clean store

Do this before the final rehearsal so old test noise is not on camera.

```powershell
# 1. Stop the agent            Ctrl-C in terminal 3
# 2. Delete the event history
Remove-Item $env:USERPROFILE\.s16code\events\history.json
# 3. Start the agent
uv run s16code serve
# 4. Reinstall the subscriptions (deleting history clears them too)
uv run python tools\put_subscriptions.py
```

Restarting the agent does **not** require restarting glc or the bridges — but if
you restart *glc*, every bridge must be restarted, because each holds a
WebSocket to it.

⚠️ **Restart the bridges after any pairing change.** `glc/routes/channels.py:92`
reads the owner set **once at WebSocket connect**. A first-run auto-pair lands
too late for that session, so messages are dropped with
`not in allowed_senders` until the bridge reconnects.

---

## 3. Verify each channel

The agent catalogues **links**. Send a message containing a URL — a plain
greeting takes a different path and shows none of the judging behaviour.

| Channel | How to send | Expect |
|---|---|---|
| **Telegram** | DM the bot: `look at this https://www.figma.com/file/abc/Design-v2` | Reply naming the link, *filed* or *skipped* |
| **Discord** | Post the same in your server | Bridge prints `received Discord message` |
| **WhatsApp** | From your phone to the Twilio sandbox number | Reply on WhatsApp |
| **IMAP** | Email the watched mailbox (`IMAP_USER`) from another account, link in the body | Poller prints `[imap] uid N from …` |
| **Microphone** | ENTER, then speak | Transcript printed, spoken reply |

Then confirm the agent judged rather than merely handled it:

```powershell
curl.exe -s "http://127.0.0.1:8113/v1/agent/events?after=0"
curl.exe -s "http://127.0.0.1:8113/v1/agent/refusals?hours=1"
uv run python tools\render_catalog.py
```

### Warm the local model first

The first Ollama call after an idle spell reloads the model and takes ~12s.
Send one throwaway Telegram message before recording.

---

## 4. Drive the demo

### The catalogue — the thing you actually show

```powershell
uv run python tools\render_catalog.py                # write sandbox\link-catalogue.md
uv run python tools\render_catalog.py --watch 30     # keep it live during the demo
uv run python tools\render_catalog.py --hours 24     # window it by event time
```
`--watch` re-renders the whole file each pass, so it always reflects current
state rather than appending. Leave it running in a terminal and open the .md
beside it.

### Volume, for the report and the ceiling

Real messages prove the five channels. These two beats need volume that is not
pasteable:

```powershell
# A realistic hour for the morning report               [the 10-pt item]
uv run python docs\scripts\simulate_events.py --count 40 --over-minutes 60

# Breach the per-source rate limit -> recorded governor refusals
uv run python docs\scripts\simulate_events.py --count 200 --over-minutes 0

# One obviously junk link -> a single recorded refusal
uv run python docs\scripts\simulate_events.py --only junk --count 1
```

### The report

```powershell
curl.exe -s "http://127.0.0.1:8113/v1/agent/report?hours=1&fmt=markdown"
```

### The liveness alarm — how it actually works

`store.beat()` is called in exactly one place, `engine.py:70`, on each event.
There is **no startup beat and no timer**. Two consequences:

- **Killing the process does not produce a 503.** Nothing is left listening, so
  you get connection-refused. Do not plan the shot that way.
- **503 appears when no event has arrived for 900 seconds**, with the process
  still running and still answering.

That is the demo, and it is the stronger one — it shows that silence and death
are distinguishable:

```powershell
# 1. While links are flowing:
curl.exe -s -i "http://127.0.0.1:8113/v1/agent/liveness"
#    200  {"alive":true,"reason":"beating","silent_for_seconds":12.4}

# 2. Stop sending anything. Wait 15 minutes (STALE_AFTER_SECONDS = 900).

# 3. Same call, process untouched:
curl.exe -s -i "http://127.0.0.1:8113/v1/agent/liveness"
#    503  {"alive":false,"reason":"no heartbeat for 9xx s; the watcher may have died"}
```

Optionally then Ctrl-C the agent and show the endpoint going unreachable — a
cruder signal an uptime monitor would page on.

### Where cost is recorded

- **Gateway ledger** — `~/.glc/gateway.sqlite`, read via
  `GET /v1/cost/by_principal`. The authoritative money record, priced at
  published list rates even on a free tier.
- **S16 event store** — `triage_cost_usd` per decision inside
  `~/.s16code/events/history.json`, aggregated into the report's
  `cost_of_watching_usd`.

### The privacy proof shot

```powershell
curl.exe -s "http://127.0.0.1:8111/v1/calls?limit=20"
# provider=ollama on links-private work, gemini_1 on links-public
```

---

## 5. Known behaviour — do not mistake these for faults

- **`cost_of_doing_usd` is always `$0.00`.** Run spend is not metered upstream
  (claimed by S16Code PR #10). `cost_of_watching_usd` **is** real — say so plainly
  rather than glossing it.
- **Liveness 503 on a fresh store** is correct until the first event.
- **`whatsapp: connected=false`** in `/v1/channels` is normal — it is webhook-based,
  not a WebSocket adapter.
- **Discord and Slack carry no attachments**; WhatsApp media arrives with
  `text=None`. Send text-with-link.
- **Gmail/IMAP drop the subject and HTML body** — use a plain-text sender.
- **Twilio sandbox pairing expires** after 72h of inactivity; re-send
  `join <code>` before recording.

---

## 6. Tests

```powershell
uv run pytest -q
```

Expected: **1 failed, 357 passed.** The single failure,
`test_calendar_skill_is_general_and_uses_planner_supplied_iso_dates`, is the
pre-existing Windows `file://` bug that PR #4 fixes on `part2-bugfix-file-uri`.

If `test_official_subscription_resumes_waiting_graph_and_maps_cancel` also
fails, the machine was busy: it polls a 150 ms window at
`test_hardening.py:157-166` and loses that race under load. Re-run on an idle
machine.

---

## 7. Branches

```
main                       untouched
part1-executive-assistant  this build. Local only, never pushed
part2-bugfix-file-uri      PR #4. Do not add Part 1 work to it
```

---

## 8. Pre-recording checklist

### Clean slate — in this order

```powershell
# 1. Stop the agent (Ctrl-C in its terminal)
# 2. Wipe the event history
Remove-Item $env:USERPROFILE\.s16code\events\history.json
# 3. Wipe the catalogue
Remove-Item .\sandbox\link-catalogue.md
# 4. Start the agent
uv run s16code serve
# 5. Reinstall the subscriptions (step 2 removed them too)
uv run python tools\put_subscriptions.py
```

WARNING: **Do not delete `~/.glc/pairings.sqlite`.** It holds every channel owner
pairing. Deleting it re-triggers the "first message from each sender is silently
dropped" behaviour on all five channels at once, mid-demo.

Clearing chat history in Discord, Telegram and WhatsApp is cosmetic — it does not
touch the event store, so do both.

### Who sends the demo email

WARNING: **Not the watched mailbox itself.** The address in `S16_SELF_EMAIL` is
in `SELF_ACTORS`, because it is the address the agent replies *from*: without
that guard a reply to the watched inbox arrives as new unseen mail and the agent
answers its own answer forever. With the guard, mail from that address is
correctly refused as self-caused — so self-sending demonstrates a refusal, not a
filing.

Send from a third address you are willing to show on camera — not the watched
mailbox, and not your primary one. Its first mail is dropped once while the
pairing registers; restart the poller and it works from then on.

### Warm the model

The first Ollama call after an idle spell reloads ~4 GB and takes ~12s. Send one
throwaway Telegram message before you hit record.

### Never on camera

- `.env` in either repo — every credential lives there
- `http://127.0.0.1:8111/channels` — the settings page shows saved secrets
- `~/.glc/channel_secrets.json`, `~/.glc/install_token`
- The ngrok terminal shows your public URL; the Twilio console shows SID and auth token
- Any real personal message

**Safe to show:** `http://127.0.0.1:8113/console` (read-only by design), the
catalogue, the report, `tools/put_subscriptions.py`, the bridge terminals.

### Timing traps

- **Liveness 503 comes from silence, not from killing the process.** Beats only
  happen on events (`engine.py:70`). Stop sending, wait 15 minutes, and the
  *running* process reports 503. A killed process gives connection-refused
  instead. Cut away rather than sitting through the wait.
- **The report's minimum window is 1 hour** (`hours` ge=1).
- **Exactly one approval pending** during wait/resume — the resume path requires
  `len(waits) == 1` and silently does nothing with two.
- **Twilio sandbox pairing expires** after 72h idle; re-send `join <code>` first.
- **ngrok's subdomain changes on restart** — update both the Twilio console and
  `TWILIO_WEBHOOK_URL`, or WhatsApp goes silent with no error anywhere.

### Say these out loud

- `cost_of_doing_usd` is `$0.00`; `cost_of_watching_usd` is real. Say which is
  which rather than glossing it — fabricating a control is what the rubric punishes.
- The dollar ceilings on `links-private` are absent *on purpose*: a local model
  projects $0.00, so `max_runs_per_day` and the rate limit are what actually bind.
- The disclosure ceiling is the distinctive part. Show `GET /v1/calls?limit=20`:
  `provider=ollama` on private-tier work, `gemini_1` on public.

---

## 9. Recording run order

Two sources of traffic, and they are not interchangeable:

- **Your own demo run-sheet** — real messages you paste into real channels. This
  is the only thing that can satisfy *"five real conversations, not five
  configured cards"*. It also carries the ignored links and the ambiguous one
  that triggers the approval. Kept out of the repo: a run-sheet names real
  mailboxes and chat ids.
- **`docs/scripts/simulate_events.py`** — volume you cannot paste: the
  report window and the rate-limit refusal.

### T-60 min — before you press record

```powershell
# Clean slate (section 8), then start the unattended window and walk away.
uv run python docs\scripts\simulate_events.py --count 40 --over-minutes 45
```
Each event posts synchronously and waits for triage, so this takes real time —
that is the point. Do your channel checks while it runs.

Warm the local model with one throwaway Telegram message near the end.

### On camera

| # | Beat | What you do | Source | Pts |
|---|---|---|---|---|
| 1 | Opening | One sentence: what it does, which five channels | — | — |
| 2 | Five channels | Paste blocks **1–4** of `S16-Demo-Content.md`, one channel at a time, showing each reply | Demo content | 25 |
| 3 | The subscription | Read `tools/put_subscriptions.py` aloud. Defend each ceiling. Land the disclosure ceiling | — | 15 |
| 4 | Something ignored | Point at the Zoom and Calendly refusals already on screen from beat 2, then `GET /v1/agent/refusals?hours=1` for the recorded reasoning | Demo content | 15 |
| 5 | The catalogue | `render_catalog.py`, open `sandbox\link-catalogue.md` — filed grouped by topic, Skipped section populated | — | — |
| 6 | Wait and resume | Block **5** on Telegram → note `run_id` → **close the /console tab**, Ctrl-C `s16code serve` (twice if it lags) → restart → reply in the same chat → same `run_id` completes | Demo content | 15 |
| 7 | The mic | Block **6** — ask it to read the catalogue, get a spoken summary | Demo content | 5th channel |
| 8 | Morning report | `GET /v1/agent/report?hours=1&fmt=markdown` — the 40 events from T-60 are in here. Read the ignored section, it should be the bulk | Simulation | 10 |
| 9 | Liveness | Show `200 / beating`. Stop sending anything. Cut. Return after the threshold and show **503**, process still running | — | 10 |
| 10 | The PR | PR #4, the failing test, the fix | — | 20 |

### The two things that go wrong

- **Beat 6:** exactly one approval pending. The resume path requires
  `len(waits) == 1` and silently does nothing with two.
- **Beat 9:** do not kill anything. 503 comes from silence, not from Ctrl-C. A
  killed process gives connection-refused instead.
