# Plan: Remembering Outcomes (Winners, Losers, and the Ones We Didn't Take)

Written 2026-08-28 in response to: build the feedback loop that's the biggest
gap between "runs modules on demand" and "actually gets smarter over time"
(see `docs/MODULE_REVIEW_2026-08-28.md`'s JARVIS section). This is Module 5
on the roadmap, made concrete.

## What already exists — don't rebuild this

Before proposing anything new: this repo already has most of the plumbing.

- **`mcp_server/conviction.py`** is a working conviction journal: `log_conviction()`
  writes an entry with entry price/date; `resolve_convictions()` checks every
  unresolved entry older than 20 hours against 1/5/10-day horizons, scores
  WIN/LOSS/PUSH against a 1.5% move threshold, and there's a scorecard/win-rate
  builder on top. Storage is flat JSON at `data/conviction_journal.json`, and
  the module's own docstring already states the reasoning: *"structured data →
  filtering, not vector search."* That's a real, already-made decision, not an
  oversight — see the ChromaDB section below for why it's still right.
- **`vesper/nodes/reflection.py`** already calls `log_conviction()` for every
  `execution_results` entry after a graph run. The wiring from Vesper into the
  journal exists today.
- **`mcp_server/knowledge.py`** already runs a persistent ChromaDB client at
  `data/chromadb/`, with a `knowledge_base` collection embedded via the Gemini
  embeddings API (`gemini-embedding-001`, 768-dim, `RETRIEVAL_DOCUMENT` /
  `RETRIEVAL_QUERY` task types). This is the exact machinery a semantic
  "have I seen a setup like this before" layer would reuse — same client, same
  embedding call, a second collection.

So the honest scope of "Module 5" isn't "build a memory system." It's: fix
two real gaps in the one that exists, then add a semantic layer on top using
infrastructure that's already a dependency.

## Bugs found while reading this for the plan

Not asked for, but they undermine anything built on top, so they're
prerequisites, not nice-to-haves:

1. **Duplicate journal entries.** `log_conviction()`'s `id` is
   `f"{ticker}:{now.strftime('%Y%m%d%H%M%S')}"` — second resolution only. Live
   `data/conviction_journal.json` right now has exact duplicate entries
   (same ticker, same id, entry_date ~700 microseconds apart) — e.g. two
   `AAPL:20260828201138` entries back to back. `_save_journal()` just appends,
   no id-uniqueness check. Every duplicate double-counts in
   `resolve_convictions()`'s win-rate math. **Root cause not yet found** — likely
   `reflection_node` running twice per session (LangGraph checkpoint replay is
   the prime suspect) rather than a single stray call; worth instrumenting
   before assuming the fix is "dedupe on write."
2. **Direction is guessed from a substring, not read from the proposal.**
   `reflection_node` sets `direction="bullish" if "BUY" in str(res.message) else "bearish"`.
   For a `REJECTED_BY_USER` or `BLOCKED_BY_GUARDRAIL` result, the message never
   contains "BUY" — so every rejected/blocked BUY gets logged as a bearish
   call, backwards. `execution_results` (the `ExecutionResult` objects) don't
   carry the original proposal's `side`; `reflection_node` needs to look the
   proposal up by `order_proposal_id` from `state["proposals"]` instead of
   string-sniffing a human-readable message.

## The actual gap: nothing is remembered that wasn't taken

`reflection_node` only iterates `execution_results` — which only contains
proposals that reached `executor_node` (approved, then dry-run-simulated,
submitted, or blocked/failed there). Two categories currently leave **zero**
trace:

- **Rejected by `risk_gate_node`** (e.g. cost exceeds live equity, quantity
  cap): the proposal is silently dropped from `state["proposals"]` before
  `executor_node` ever sees it, so no `ExecutionResult` exists and
  `reflection_node` never logs anything for it.
- **Never proposed at all**: a `Candidate` that `scanner_node` surfaced but
  that didn't clear whatever bar `playbooks_node` uses to draft a proposal.
  This is the hardest one to capture cheaply (see Phase 2 below) but it's
  also the most valuable — it's the only way to answer "is the playbook's bar
  even calibrated right, or is it letting winners walk while trading the
  choppy setups."

This is exactly what you asked to track and it doesn't exist today in any form.

## ChromaDB — direct answer

**Yes, but not instead of the structured journal — alongside it, for a
different job.**

- The win-rate math (per playbook, per signal type, per regime, over time) is
  aggregation and filtering over structured fields — `GROUP BY playbook`,
  `WHERE result = 'LOSS' AND regime = 'DEFENSIVE'`. That's exactly what the
  existing JSON-file-as-list approach already does adequately at this volume,
  and it's what `conviction.py`'s own docstring already concluded. A vector
  DB doesn't make that math better; it makes it harder to write.
- What a vector DB *does* buy you: given a **new** setup right now (a fresh
  thesis paragraph, a regime snapshot, a signal fingerprint), retrieve the
  most similar **past** setups and what happened to them — including ones
  that were rejected or skipped, phrased in free text a scanner's rigid
  fields can't capture ("compressed range into an air-pocket gap-fill, thin
  volume, right before an FOMC print"). That's semantic recall, and it's
  exactly ChromaDB's job — and it's already a dependency here for the same
  reason (`knowledge.py`'s 139-book RAG).
- **Recommendation:** reuse the existing `chromadb.PersistentClient` at
  `data/chromadb/` (same file `mcp_server/knowledge.py` already opens — same
  process, same path, a second collection, not a second database) and add a
  `trade_memory` collection. Structured facts (ticker, direction, result,
  playbook, regime, pct_move) stay exactly where they are, in the JSON
  journal `conviction.py` already maintains — indexed alongside as `metadata`
  on the Chroma entry so a semantic hit can be filtered by "similar AND was a
  LOSS" without needing the vector index to do double duty as a database.

If `data/conviction_journal.json` ever gets big enough that flat-file
filtering becomes the bottleneck (thousands of entries, not hundreds),
migrate it to sqlite the same way `data/leveraged_etfs.db` already is — that
migration is orthogonal to adding Chroma and shouldn't block this plan.

## Schema: one entry, three possible origins

Extend the existing journal entry shape (don't replace it — `resolve_convictions()`
and the scorecard already depend on the current fields) with an `origin` field
and, for anything that wasn't taken, a `not_taken_reason`:

```jsonc
{
  "id": "...",                    // existing
  "ticker": "...", "direction": "...", "confidence": 4,   // existing
  "reasoning": "...", "signals": "...",                    // existing
  "entry_price": 0.0, "entry_date": "...", "entry_ts": 0,  // existing
  "resolved": false, "resolutions": {},                    // existing

  "origin": "EXECUTED",           // NEW: EXECUTED | REJECTED_BY_RISK_GATE
                                   //      | REJECTED_BY_USER | NOT_PROPOSED
  "playbook": "momentum_squeeze", // NEW: promoted out of the reasoning string
                                   //      so it's filterable, not grep'd
  "regime_posture": "DEFENSIVE",  // NEW: same reason
  "not_taken_reason": null,       // NEW: e.g. "cost $4,800 exceeds equity $3,000"
                                   //      populated only when origin != EXECUTED
  "session_id": "sess-..."        // NEW: ties back to a specific graph run
}
```

`entry_price` still gets fetched at log time even for a rejected/not-proposed
signal — that's what makes it resolvable later exactly like a taken trade:
"if we'd taken this, would it have won?"

## Phased plan

### Phase 1 — fix the foundation (no new surface area)
1. Find and kill the duplicate-logging cause in `reflection_node` /
   `log_conviction` (instrument first, don't guess).
2. Fix `reflection_node` to read `side` off the matched `OrderProposal` (via
   `order_proposal_id` lookup in `state["proposals"]`) instead of string-
   sniffing `res.message`.
3. Add `origin`, `playbook`, `regime_posture`, `session_id` fields to
   `log_conviction()`'s entry shape (additive — `resolve_convictions()`
   doesn't need to change, it only reads the fields it already reads).

### Phase 2 — log what we didn't take
4. `risk_gate_node`: when it rejects a proposal, call `log_conviction()` with
   `origin="REJECTED_BY_RISK_GATE"` and `not_taken_reason=err` (the rejection
   string it already generates) instead of just dropping it silently.
5. `human_gate_node` / `reflection_node`: same for `origin="REJECTED_BY_USER"`
   — this one's easy, `REJECTED_BY_USER` `ExecutionResult`s already exist,
   `reflection_node` just needs to not mis-tag their direction (Phase 1.2
   fixes the prerequisite).
6. `NOT_PROPOSED` (candidates `scanner_node` found that never became a
   proposal) is the expensive one — it means `playbooks_node` has to log a
   lightweight entry for every candidate it *considers and declines*, not
   just the ones it drafts. Do this last, and consider a lighter-weight
   record for these specifically (skip the live price fetch that
   `log_conviction` does for every entry today, if scanner volume makes that
   costly against the 600 req/min market-data bucket) rather than paying full
   journal-entry cost for every candidate that didn't clear a threshold.

### Phase 3 — close the resolution loop automatically
7. Nothing in `vesper/` calls `resolve_convictions()` today — it only runs if
   someone asks a chat agent to check their track record. Add a call at the
   start of `reflection_node` (or a lightweight scheduled tick, matching
   Module 3's monitor-loop pattern) so resolution happens on its own cadence,
   not on demand.

### Phase 4 — semantic layer
8. New `vesper/memory.py` (or extend `mcp_server/knowledge.py` with a second
   collection — reuse its `_get_chroma()`/`_embed_texts()` rather than a
   second client): `ingest_outcome(entry)` embeds `reasoning` + `signals` +
   `origin`/`not_taken_reason` as the document text, stores the structured
   fields (ticker, result, playbook, regime, pct_move) as Chroma metadata.
   Call it wherever `log_conviction()` is called, and again when
   `resolve_convictions()` fills in a result (so the same memory entry
   updates from "open" to "resolved WIN/LOSS" rather than needing a second
   document).
9. `recall_similar_setups(thesis_text, top_k=5, filter=None)`: semantic query
   returning past entries (taken or not) with their eventual outcome —
   callable from `playbooks_node` before drafting a proposal ("this looks
   like 3 past setups, 2 of which were losses") and exposable as an MCP tool
   for chat, mirroring `search_knowledge()`'s existing shape.

### Phase 5 — feed it back into scoring
10. `scanner_node`/`playbooks_node`: adjust `Candidate.score` using the
    resolved hit rate for similar past setups (from Phase 4's recall) and/or
    the playbook's own aggregate win rate (from the structured journal). This
    is the step that actually makes the system behave differently because of
    what it remembers, rather than just remembering — everything before this
    is instrumentation; this is the payoff.

## Open decisions (yours to make, not mine)

- **Resolution horizons for a rejected/not-proposed signal**: same 1/5/10-day
  windows as a taken trade, or something 0DTE-aware for same-day signals?
  The existing horizons assume a swing-trade cadence; a rejected 0DTE call
  needs same-day resolution or the "would it have won" question is
  meaningless by the time it resolves.
- **Cost of Phase 2.6** (logging every declined candidate): worth checking
  actual candidate volume from a real `scanner_node` run before committing to
  logging all of them — if it's dozens per cycle, the live-price-fetch cost
  in `log_conviction()` needs trimming for this path specifically.
- **Who scores relevance in Phase 5**: a simple recency/hit-rate blend into
  `Candidate.score`, or should resolved outcomes actually adjust which
  playbooks run at all (e.g. auto-deprioritizing a playbook that's been
  losing for a month)? The latter is more "amazingly automated," the former
  is much easier to get right and audit.
