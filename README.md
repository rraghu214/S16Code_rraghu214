# S16Code — a general live-graph agent

S16Code takes S15's durable graph, memory, A2A, UI, budget controller and
telemetry as its foundation, then replaces the task-shaped planner with a
general capability-driven agent loop. `glc_v5` connects that loop to every
enabled gateway channel through one shared envelope.

The planner does **not** build the whole DAG up front. It proposes only the next
runnable frontier, the runtime launches independent nodes together, and every
outcome causes another planning round. The graph therefore grows from evidence:

```text
goal → plan next frontier → run independent work concurrently
     → observe real outcomes → critique evidence → expand or answer
```

There is no prompt classifier, benchmark router, `_work_intent`, or deterministic
task fallback. A model may propose work, but Python owns the boundary: only
registered capabilities with valid arguments and valid existing dependencies
can enter the graph.

## What makes it general

- `s16code/capabilities.py` is the complete manifest the planner sees. It
  describes what the agent can do and strictly validates every argument.
- `s16code/planner.py` asks for only the next useful frontier. A new task may
  depend only on evidence that already exists—not on an imagined future task.
- Independent tasks in one frontier run concurrently. Synthesis is held until
  active siblings finish, so the agent does not answer while useful evidence is
  still arriving.
- Before a terminal answer, a separate evidence-readiness pass checks the
  original request against accumulated outcomes. Missing facts cause more work,
  not cosmetic rewriting.
- Equivalent active work is deduplicated even when the planner invents a new
  node ID. Run and frontier limits keep an unproductive loop finite.
- Invalid planner output is repaired through the model and recorded. If repair
  fails, the run fails visibly; it never switches to a hidden, hardcoded agent.

## Capabilities

The shipped registry includes scoped memory recall and explicit remembering,
semantic document indexing, web search and URL reading, bounded research,
retrieval/distillation/validation, sandboxed file access, calendar artifact
creation, A2A delegation, UI composition and evidence-grounded answers.

Web research uses a multi-backend search client and then reads the returned
pages. Search snippets and pages are untrusted evidence. Crucially, if search
finds no usable URL—or no page can be read—the researcher returns
`insufficient: true` and does **not** ask a model to synthesize facts.

## Inherited production boundaries

- `s16code/core/live_graph/`: event-sourced executor, patches and replay
- `s16code/core/memory/`: typed, scoped memory and semantic chunking
- `s16code/core/a2a/`: Agent Cards, JSON-RPC and optional gRPC
- `s16code/ui/`: catalog validation, A2UI surfaces, AG-UI and HITL
- `s16code/economics/`: model tiers, hard budget admission and ledger
- `s16code/telemetry/`: journal-to-OpenTelemetry span export
- `s16code/evals/`: generic resolution judging

The graph journal remains the source for replay, UI events and telemetry. All
gateway model calls—including planning and evidence review—pass through S15's
metered call seam. `glc_v5` remains a separate service and owns provider keys;
S16Code contains none.

## Run locally

Start `glc_v5` on port `8111`, then:

```bash
uv sync
cp .env.example .env
uv run pytest -q
uv run ruff check .
uv run s16code serve
```

S16Code defaults to `http://127.0.0.1:8113`. Useful environment variables are
documented in `.env.example`; most importantly:

```text
GLC_BASE_URL=http://127.0.0.1:8111
S16_GATEWAY_PROVIDER=gemini
S16_SANDBOX_ROOT=/absolute/path/the-agent-may-read
S16_CHANNEL_BRIDGE_TOKEN=the-same-private-value-used-by-glc-v5
```

Do not put provider keys in S16Code. `glc_v5` can rotate among its configured
Gemini keys behind the one logical `gemini` provider.

## Channel operation and proof

GLC converts provider-specific payloads; S16 sees only the canonical envelope.
An inbound message creates a real live-graph run, and its terminal result is
returned on the originating channel and thread. A same-thread reply can satisfy
a waiting human-approval node. An external job callback can resume a sleeping
run and proactively send its completed answer through GLC.

The channel list is discovered from GLC at runtime:

```bash
curl -s http://127.0.0.1:8111/v1/channels | jq
```

The 20-prompt stress catalogue spans every shipped channel and checks observable
capability families, parallel frontiers, and wait/resume events—not prescribed
node IDs or a prompt-specific graph:

```bash
# Start this proof S16 with only local fixture mutations authorised:
S16_CHANNEL_ALLOWED_SIDE_EFFECTS=remember_explicit_fact,write_file,index_file,create_calendar_events,request_approval \
  uv run s16code serve

# In another shell, use the installation token printed from glc_v5:
GLC_INSTALL_TOKEN=<glc-v5-install-token> \
  uv run python proofs/channel_stress.py \
  --glc http://127.0.0.1:8111 --s16 http://127.0.0.1:8113
```

Run the proof with channel authority limited to the local fixture capabilities
shown in `proofs/channel_stress.py`. It injects canonical envelopes locally and
fails any scenario that invokes `send_channel_message` or `launch_job`, so it
cannot silently count an external delivery as proof.
Native provider payload conversion remains the responsibility of each GLC
adapter's tests. Its JSON report contains each original prompt, actual graph
capabilities, reply, event count, parallel/wait/resume evidence, and result.

GLC recomputes sender trust from pairing state before the message reaches S16.
Only a gateway-verified installation owner receives the side-effect authority
listed in `S16_CHANNEL_ALLOWED_SIDE_EFFECTS`; other allowed senders remain
read-only.

Example:

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id":"demo",
    "project_id":"general-agent",
    "user_id":"student",
    "prompt":"Research Rust and Go independently, then compare their concurrency models. Explain one situation where each is the safer choice."
  }' | jq '{status, answer, graph: .graph.nodes, planner: .trace.planner}'
```

## Replaceable live proof

`proofs/tasks/general_agent.jsonl` is data, not routing code. Replace its prompts
with unseen tasks and run the same HTTP harness against a live S16 process:

```bash
S16_PORT=8116 uv run s16code serve
uv run python proofs/general_agent_live.py \
  --base-url http://127.0.0.1:8116 \
  --tasks proofs/tasks/general_agent.jsonl
```

The output at `proofs/out/general_agent_live.json` retains each prompt, final
answer, every node and edge, every accepted graph patch, planner decisions,
evidence review and timing. This is the inspectable proof of behavior—not a
claim that a prompt "worked."

The inherited S15 economics proofs remain available in `proofs/`. They test the
same runtime's budget ceiling, denial-of-wallet protection, trace export,
semantic-cache savings and cross-model tier ladder.

## Honest limits

A general agent is bounded by its registered capabilities, source availability
and models. The evidence critic is an additional model judgment, not a theorem.
The hard guarantees are narrower and enforced in code: authority validation,
existing-evidence dependencies, bounded graph/frontier size, deduplication,
metered provider calls, budget admission, durable outcomes, and no research
synthesis without readable sources.

Provider adapters vary in how much live external delivery they implement. The
connection proof establishes that every registered adapter reaches the S16 seam;
it is not a claim that unconfigured Gmail, Twilio, or Slack accounts can send.
