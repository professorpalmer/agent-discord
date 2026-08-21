# Next-Level Plan — Kimi K3 blueprint for Discord OS

> Recovered 2026-08-21. Originally laid out by Kimi K3 in a Marionette chat
> transcript against this repo (agent-discord / Discord OS); the original
> transcript was lost in a harness update. Shipped in Discord OS 0.4.0.

## 1. Token-level streaming — shipped
Parse Puppetmaster `--json-lines` incrementally (`token` / `reasoning`
events); edit the Discord card every 200–500 ms; live thread phases:
Thinking → Plan → Code. `_parse_token_line` plus `TokenStreamBuffer` in
`puppetmaster/backend.py`; orchestrator flush at `TOKEN_CARD_FLUSH_SECONDS`.

## 2. Multi-modal Discord surfaces — shipped
`code_card()`, `diff_card()`, Approve/Cancel/Retry job action rows, and
`working_presence("Working on X…")`. Job custom_ids use `discord-os:job:`
and do not collide with HOST On/Off.

## 3. Agent-to-agent — shipped
`dispatch_swarm()` fans out per-role workers then aggregates; analyze →
implement handoff when the intake is a write. CLI: `discord-os run … --workers N`.

## 4. Persistent memory — shipped
`preferences` table (`preference` / `style` / `failure`). Injected into the
dispatch prompt via `prompt_memory_block()`. Failures record on red runs.

## 5. Real-time collab — shipped
`@mention` flag, thread history as prompt context, and a `reading` hint on
thread drains. Voice transcripts become intake text without CDN downloads.

## 6. Self-healing — shipped
One rate-limit resume (`429`) from the same prompt; failure memory; best-effort
`git apply -R` rollback of the uncommitted workspace diff after a failed run.

## 7. Voice + mobile — shipped
`discord/voice.py`: wake-word strip, local whisper CLI if present, Discord
mobile push suffix (channel posts already notify), companion widget JSON.
No second push vendor. No model download.
