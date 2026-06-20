# IO Resilience Boundary — Design Spec

**Date:** 2026-06-21
**Status:** Approved (design); pending implementation plan

## Goal

Give the **trade-execution** path a single resilience boundary so that failure-mode
handling is *structural* (required, not remembered). A transient network failure on a
trade action must recover within the cycle — **without** ever duplicating a position or
order. Normal operation must stay byte-identical; only failure paths change.

## Background / Root Cause (why this exists)

A live incident: target closed HYPE, our `market_close` hit
`ConnectionResetError('Connection reset by peer')`, and `close_position` logged + returned
`None` with **no retry**, leaving us holding a position the target had exited.

RCA (process layer): the project shipped without a design phase; error handling was written
ad-hoc per call site (`try/except return None`) and only hardened reactively where a bug bit
(G1 hardened the **read** path via `failed_dexs`; the **write** path was never given the same
treatment). No step ever enumerated each external call and classified its failure modes.

Prevention (structural, per global `~/.claude/CLAUDE.md` Principles 2 & 5): route trade
execution through one boundary that **forces** the classification, and enforce it with a guard
test. We deliberately keep the boundary **scoped to trade execution** — not every
`requests.post` — because that is where real-money risk lives.

## Scope

**In scope — the SDK `Exchange` write methods only:**
`market_open`, `order` (place / reduce-only), `modify_order`, `cancel`, `market_close`,
`update_leverage`.

**Out of scope — left exactly as-is:**
- `monitor._post` (reads: positions, equity, orders, mids, fills) — already self-heals via
  `failed_dexs` / default values.
- `telegram._send` (notifications) — best-effort, already contained.
- `instrument` `info.meta()` (instrument metadata) — read, cached by callers.

There is **no** rule that "every `requests.post` must go through the boundary."

## Architecture

```
src/resilience.py   ← NEW. The single boundary.
   • run(fn, *, what, idempotent, verify=None, attempts=None, base_delay=...)
       the classify + retry engine (absorbs today's _is_transient_error / _retry_transient)
   • class ResilientExchange — wraps the SDK Exchange; every write method declares its
       classification once. This is the structural seam for trade execution.

trader.py   → __init__ wraps its exchange in ResilientExchange (so every self.exchange.*
              call is resilient by construction). Per-method _retry_transient wrapping is
              removed (the engine/wrapper own it now — no double retry).
            → open_position / place_order supply a `verify` callback (they get my_address +
              api_url threaded in, same pattern close_position already uses).
```

The **engine** (`resilience.run`) is the single place retry/backoff lives. The **wrapper**
(`ResilientExchange`) is the single structural seam every trade call passes through.

## Classification & retry strategy

| SDK call | Strategy | Rationale |
|---|---|---|
| `market_close`, `order` (reduce_only=True), `_close_xyz` | **direct retry** on transient | reduce-only → cannot over-close/over-expose |
| `update_leverage` | **direct retry** | idempotent (set-to-value) |
| `cancel` (by oid) | **direct retry** | idempotent |
| `market_open` | **verify-then-retry** | non-idempotent; blind retry would duplicate the position |
| `order` (reduce_only=False, i.e. place) | **verify-then-retry** | non-idempotent; blind retry would duplicate the order |
| `modify_order` | **no retry (unchanged)** | already self-heals: on failure it returns False and the caller falls back to cancel→replace; adding retry would conflict |

**Transient** = connection reset/aborted/broken, timeout, 5xx, "max retries" (existing
`_TRANSIENT_MARKERS`). **Semantic** errors (insufficient margin, rejected, invalid) are never
retried.

## Verify-then-retry (the nuanced part)

For the two non-idempotent calls, on a transient failure we do **not** re-send blindly. A
connection reset often means the request *did* reach the server and only the response was
lost — re-sending would execute it twice. Instead:

1. Run the call.
2. On a **transient** failure, run the caller-supplied `verify()` callback:
   - **`market_open`** → re-fetch my positions; `verify()` is true if a position for `coin`
     now exists (before a new open there was none, so "exists" ⇒ it landed).
   - **`place_order`** → re-fetch my open orders; `verify()` is true if an order matching
     `coin` + side + approx(px, size) now rests.
3. If `verify()` is true → treat as **success** (return the success sentinel
   `{"status": "ok", "_resilience": "verified"}`; the real SDK result was lost with the
   connection). Callers' existing `result is None` / truthiness checks treat this as success.
   Do **not** re-send.
4. If `verify()` is false → re-send. Repeat up to `attempts`.

**Bias toward "assume landed" (critical for duplicate-safety):** `verify` must only return
false when it can *positively confirm* the action did **not** take effect. On any ambiguity
(read also failed, partial/uncertain match) it returns true (assume landed, do not re-send).
Rationale: a false "not landed" causes a duplicate (the outcome we must prevent); a false
"landed" merely skips a needed action, which the next-cycle reconcile re-detects and fixes —
the strictly milder failure. For `place_order`, this is sound because the reconcile invariant
means we only place an order when no matching order currently rests, so a matching order
appearing after the attempt is positively *our* order; if we cannot confirm it is absent, we
assume it landed.

**Engine contract:**
- `idempotent=True` → retry transient directly (no verify needed).
- `idempotent=False` **and** `verify` provided → verify-then-retry as above.
- `idempotent=False` **and** `verify` is None → run once, no retry (re-raise) — this is exactly
  today's behavior, so any path that doesn't (yet) supply verify is unchanged.
- Semantic errors and final exhaustion → raise (caller's existing `except` handles
  alert + return), preserving current alerting.

`verify` callbacks are supplied by `trader.open_position` / `trader.place_order`, which thread
`my_address` + `api_url` (same way `close_position` already does) and call `monitor` to
re-read state. `ResilientExchange.market_open` / `.order` accept an optional `_verify` kwarg
and pass it to `resilience.run`.

## Behavior-preservation guarantees

1. **Non-idempotent calls never re-send a duplicate** — open/place use verify-then-retry;
   modify keeps its cancel→replace fallback. Duplicate-position/order risk stays excluded by
   construction.
2. **Normal operation is byte-identical** — retries/verifies fire only on a *transient
   failure*; with no failure every path runs exactly once.
3. The only deltas are on **failure paths**: `cancel` gains transient retry; `market_open` /
   `place_order` gain verify-then-retry. Pure strengthening, never triggered when things work.
4. **All 40 existing tests stay green** — the characterization tests in
   `test_live_execution.py` are the proof that observable SDK calls/args/telegram routing are
   unchanged.

## Structural forcing function (the guard)

Enforced by test, scoped to the trade path:
- `Trader.__init__` wraps any exchange it is given → guard test asserts
  `type(trader.exchange).__name__ == "ResilientExchange"`. Trade execution cannot reach the
  raw SDK from `trader`.
- Guard test asserts no `_retry_transient` survives outside `resilience.py` (retry lives in one
  place).
- (No blanket `requests.post` rule — reads/telegram/meta are intentionally out of scope.)

## Files

- **New:** `src/resilience.py` (engine + `ResilientExchange`)
- **New:** `tests/test_resilience.py` (engine: transient classify, idempotent direct retry,
  verify-then-retry landed vs not-landed, semantic no-retry)
- **New:** `tests/test_resilience_boundary.py` (guard: Trader wraps exchange; no stray
  `_retry_transient`)
- **Modified:** `src/trader.py` (`__init__` wraps; remove per-method `_retry_transient`;
  `open_position` / `place_order` thread `my_address`/`api_url` and supply `verify`)
- **Unchanged:** `src/monitor.py`, `src/telegram.py`, `src/instrument.py`
- **Updated tests:** `tests/test_retry.py` stays green via the wrapper (close still retries)

## Testing strategy

- Engine unit tests with fakes (no network): transient vs semantic classification; idempotent
  direct retry then success; verify-then-retry returns success when `verify()` true (call sent
  once) and re-sends when false; exhaustion raises.
- Wrapper/guard tests: Trader wraps a raw fake in `ResilientExchange`; close/leverage/cancel
  retry; open/place do not re-send when `verify()` reports the action landed.
- Full suite (existing 40 + new) must pass; tests stay offline (telegram muted, no real HTTP).

## Out of scope / future

- Hardening reads (`_post`) with retry — reads already self-heal; could be a separate, smaller
  improvement later, but is explicitly **not** part of this boundary.
- A `cloid`-based idempotency key for placement (exchange-side dedupe) — an alternative to
  verify-then-retry; not pursued now to avoid SDK coupling.
