# Candidate D — glc_v5's dollar budget controller doesn't hold under concurrency

*Repo: `glc_v5` only. `S16Code` is not involved — it's just one caller among many of `POST /v1/chat` and has no visibility into this bug or its fix. Written 2026-08-11 after a long design discussion; this is the source of truth for that discussion, not a summary of it.*

## 1. The bug, precisely

`BudgetController.admit()` (`glc/economics/budget.py:429`) approves or refuses a call by reading how much a principal has *already spent* — a `SELECT SUM(usd) FROM calls WHERE ...` against the ledger — and comparing it to the configured limit. It writes nothing. The actual spend is only written later, by `Meter.record()` (`glc/economics/meter.py:164`, via `db.log_call`), which is called from `glc/routes/chat.py` **after** the LLM provider call returns — often several seconds later, longer for streaming responses.

```
chat.py:669   admission = ctl.admit(principal, projected_cost)     # reads spend, writes nothing
              ... await provider.stream(...) / await provider.chat(...) ...   # the slow part
chat.py:733   meter.record(...)                                    # only NOW is spend written
```

Two concurrent requests from the *same principal* (same tenant/project/user/session — whatever dimension the policy governs) can both call `admit()` while the first one is still in that gap. Both read the same, not-yet-updated spend total. Both get approved. Both complete. Combined spend sails past a limit that every individual check said was fine. This is a classic TOCTOU (time-of-check / time-of-use) bug.

**Why it matters:** the project's own README calls this "the hard controller... admission on a projected worst-case cost before the provider is called; breach → HTTP 402." Session 16's own material (§10, "the attacker who schedules your agent") names exactly this shape as one of the four ways to escape a budget: *"escape the budget by starting more runs... bound the window, not the request."* This is that failure, in the one place that's supposed to prevent it.

**Not yet claimed.** Checked against every PR open on `glc_v5` as of 2026-08-11 (#1–#9) — none touch `glc/economics/budget.py`, `glc/routes/chat.py`'s admission path, or anything resembling a concurrency fix. Two *S16Code* PRs (#7, #10) have similarly-worded titles ("so daily_triage_budget can actually bind," "so daily_budget can bind") but are unrelated: those fix `s16code/`'s own governor recording $0.00 for every run (a data-plumbing bug — the field was always zero). This bug is different in kind: the recorded numbers are correct, they're just read at the wrong moment by a second concurrent request. Re-verify this with `gh pr list --repo theschoolofai/glc_v5 --state all` and `gh pr list --repo theschoolofai/S16Code --state all` before starting — more PRs may have landed since this was written.

## 2. The real-world pattern this borrows from

This is exactly what a card authorization does. Swiping a card doesn't immediately charge the final amount — it places a **hold** for an estimated worst-case amount, which immediately reduces the *available* balance so a second concurrent swipe can't also spend the same money. The real transaction settles later for the *actual* amount, and — critically — the hold is never overwritten. It's explicitly **released**, and a separate, new, immutable entry records the real charge. Both facts exist forever.

Applying that here fixes two things at once: the concurrency hole, and a subtler problem an early "just overwrite the reservation row" version of this fix has — if request 2 gets refused because request 1's *worst-case* hold looked too big, and request 1's row later gets silently rewritten down to its smaller *actual* cost, nobody can later explain why request 2 was refused. The evidence for a real, correct decision gets destroyed by later information. Session 16's own principle applies one layer down here: **a control that prevents work must leave a trace, and that trace must not be erasable by what happens afterward.**

## 3. The fix design

### 3.1 Three states, not two

Add a third ledger status alongside the existing `'ok'` / error statuses: **`'pending'`** (a hold, not yet resolved) and **`'released'`** (a hold that has been resolved — its dollar figure stays untouched forever, it's just excluded from the running total from then on).

### 3.2 Step one — reserve, atomically with the check

The moment `admit()` approves a call, in the **same transaction** as the check (SQLite serializes writers database-wide, so a single `BEGIN IMMEDIATE ... COMMIT` block gets this for free — no `SELECT FOR UPDATE`-style row lock needed or available in SQLite):

- Insert a new row: `status='pending'`, `usd=<the worst-case projected cost already computed by ctl.project()>`, tagged with the principal.
- Return that row's id as part of the `Admission` result, so the caller can reconcile it later.

The running-total query becomes: `SELECT SUM(usd) FROM calls WHERE principal=? AND status IN ('ok','pending')`. A second concurrent request now sees the first request's hold as part of "already committed," and gets refused if the combined total would breach the limit — the race is closed.

### 3.3 Step two — reconcile, atomically with the release

When the real call finishes (`meter.record()`'s call site in `chat.py`), in one transaction:

- **Insert a new row** for the real, final cost: `status='ok'` (or the error status), `usd=<actual>`. This is the genuine, permanent ledger entry — identical in shape to every other completed call today.
- **Update** the *original* hold row, but only its `status` field, `'pending' → 'released'`. Its `usd` value is never touched — it stays exactly what it was at admission time, forever, as the historical record of what was believed committed at that moment.

This second step must be as atomic as the first — flip the hold to `released` and insert the settlement together, in one transaction. A gap between them would recreate a smaller version of the same race (a window where neither row counts, briefly under-committing).

### 3.4 What differentiates two rows belonging to the same call

Nothing needs to *link* them for the sum to be correct — the `status` field alone does it. `released` is excluded from both `'ok'` and `'pending'`, so a resolved hold contributes $0 once its settlement row exists. (Add a `reservation_id` column on the settlement row pointing back to the hold's row id anyway — purely so a human debugging later can see which hold a given settlement resolved. Not required for the arithmetic.)

### 3.5 The limit itself never moves into this table

The budget ceiling (`limit_usd`) already lives entirely separately — in `budgets.yaml` (file policy) or the `budget_limits` SQLite table (`glc/economics/budget.py:_overrides()`, written by `POST /v1/budget`). It is **policy**, set by a human, rarely changes, and has nothing to do with any individual request. Do not conflate it with the ledger. `admit()` already keeps this separation (`statuses()` reads policy; `spend_usd()` reads the ledger); the fix must preserve it, not merge them.

### 3.6 Worked example (already validated in discussion — use these numbers for the regression test)

Policy: `principal X, limit_usd=2.00, period=day`.

| id | principal | status | usd |
|---|---|---|---|
| 101 | X | released | 0.70 | ← T1's hold, resolved, dollar figure untouched |
| 102 | X | ok | 0.60 | ← T1's real settlement |
| 103 | X | pending | 1.00 | ← T2's still-open hold |

Committed = 0.60 + 1.00 = **1.60**. Remaining = 2.00 − 1.60 = **0.40**. A third request projecting $0.50 must be refused; one projecting $0.30 must be admitted. That's the assertion shape for the regression test.

## 4. Concrete touch points

- `glc/db.py` — `spend_usd()`'s `WHERE` clause needs `status IN ('ok','pending')` instead of implicitly only ever summing completed rows; `log_call()` needs to support inserting a `'pending'` row and later updating just its `status`.
- `glc/economics/budget.py` — `BudgetController.admit()` needs to perform the reserve (3.2) inside the same transaction as its check, and return the reservation's row id on the `Admission` object.
- `glc/economics/meter.py` — `Meter.record()` needs an optional `reservation_id=` parameter: when given, insert the settlement row and release the hold (3.3) instead of just inserting.
- `glc/routes/chat.py` — every call site of `meter.record()` (there are several — streaming success, streaming failure, non-streaming, cache-hit paths) needs to pass the reservation id it got back from `admit()` at line 669. Check the non-`/v1/chat` budget-admission paths too (embeddings, etc., if any share `BudgetController`).

## 5. Test strategy

Mirror `S16Code`'s own precedent for this exact bug shape (`tests/test_autonomy_governor.py::test_the_run_ceiling_holds_when_events_arrive_together`, from `S16Code` PR #3): set a tight budget, dispatch several requests **concurrently** via `asyncio.gather` against a stubbed provider with artificial latency (so the "slow gap" is real inside the test), and assert the *total recorded spend* never exceeds the ceiling. It must fail against current `main` (sequential-dispatch tests, which is all that exists today, cannot expose this — same lesson as Session 16 §12's own honest-failures section: a fixture that never actually overlaps two calls proves nothing about concurrency).

## 6. Explicitly out of scope for this PR

- Don't also fix Candidate C (Windows file-permission enforcement) in the same PR — different bug, different file, keep the diff reviewable.
- Don't attempt to make this correct across multiple `glc_v5` worker processes/machines — the whole codebase currently assumes one process (SQLite itself doesn't support multi-process writers safely either); state that assumption explicitly in the PR rather than silently expanding scope to solve it.
- Don't add automatic cleanup for a hold that's abandoned by a crash before reconciliation. Conservative default, matching `S16Code` PR #3's own stated principle for its run-slot reservation: *"a reserved slot is not released if the run then fails... the conservative direction for a ceiling is to count it."* Leave a stuck `pending` row counted; note it as a known follow-up, don't build it now.
