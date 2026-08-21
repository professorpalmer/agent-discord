# Next-Level Plan — Kimi K3 blueprint for Discord OS

> Recovered 2026-08-21. Originally laid out by Kimi K3 in a Marionette chat
> transcript against this repo (agent-discord / Discord OS); the original
> transcript was lost in a harness update. Persisted here so it can't vanish
> again. Symbol references verified against the tree on recovery day:
> `_parse_progress_line` lives in `puppetmaster/backend.py`; V2 layout
> primitives (`section`, `text_display`, `thumbnail`, …) live in
> `discord/layout.py`; `code_card()` / `diff_card()` do not exist yet.

## 1. Token-level streaming
Parse Puppetmaster `--json-lines` incrementally (`token` / `reasoning`
events); edit the Discord card every 200–500 ms; live thread phases:
Thinking → Plan → Code. Touch `_parse_progress_line` → add
`_parse_token_line`, plus a token buffer/flush in the orchestrator.

## 2. Multi-modal Discord surfaces
Syntax-highlighted code blocks, diff/patch attachments,
Approve/Cancel/Retry buttons, presence "Working on X…". New
`code_card()`, `diff_card()`, action rows.

## 3. Agent-to-agent
Swarm fans out to per-worker threads then aggregates; analyze → implement
handoff; reaction voting. New `dispatch_swarm()`, `--workers N`.

## 4. Persistent memory
Preference injection, code-style memory, failure memory. New preferences
table.

## 5. Real-time collab
Multi-user @mention threads, live "reading file X…" status, thread history
as prompt context.

## 6. Self-healing
Rate-limit resume from checkpoint, partial recovery, rollback-on-red-tests.

## 7. Voice + mobile
"Hey Discord OS, run tests" (voice + whisper), Discord push notifications,
home-screen widgets. New `discord/voice.py`.
