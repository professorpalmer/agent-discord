# agent-discord

**GitHub-first, open-source Discord-native agent bridge** — download it, bootstrap your own Discord bot, workspace, and credentials, then issue natural-language tasks from Discord-shaped channels. The bridge assembles scoped context from SQLite memory, dispatches a **pinned** Puppetmaster worker, and returns durable progress/final receipts.

This is not a hosted multi-tenant SaaS. Bring your own harnesses, CLIs, and deployment choices; this project provides a small working bridge and leaves operations to you.

## What it does

1. **Intake** a natural-language task (CLI today; Discord via MCP providers).
2. **Snapshot** scoped context from SQLite memory + channel bindings.
3. **Dispatch** to Puppetmaster with an explicit Cursor model pin (`cursor/grok-4-5` / adapter `grok-4.5`). **No silent model fallback.**
4. **Persist** append-only events, artifacts, and usage/receipt metadata.
5. **Relay** Discord-safe progress updates and a final receipt (never raw hidden chain-of-thought).

## Attribution (upstream MCP Discord servers)

This project owns a thin facade + provider adapters. **Upstream source code is not copied.**

| Provider | Repository | License | Notes |
|----------|------------|---------|-------|
| **SaseQ / discord-mcp** | https://github.com/SaseQ/discord-mcp | MIT | HTTP endpoint convention via `SASEQ_MCP_HTTP_URL` (default `http://127.0.0.1:8085/mcp`). Prefer HTTP; stdio requires an explicit `DISCORD_MCP_STDIO_COMMAND` (no fabricated npm default). |
| **BrainDAO / mcp-discord** (`@iqai`) | https://github.com/BrainDAO/mcp-discord | MIT | Sampling/tool convention exposed as an adapter seam; **one Gateway owner per bot token** — no second Gateway required. Stdio requires an explicit `DISCORD_MCP_STDIO_COMMAND` such as `npx -y @iqai/mcp-discord`. |

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for details.

## Quick start

```bash
# Requires Python 3.11+
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env: set DISCORD_BOT_TOKEN, choose provider/transport

agent-discord bootstrap
agent-discord check

# Dry-run path (fake MCP + fake Puppetmaster — no network / no Cursor credits)
agent-discord run "Summarize open items" --channel-id 123 --fake --no-discord-post
```

### Discord MCP providers

Set in `.env`:

- `DISCORD_MCP_PROVIDER=saseq` or `braindao`
- `DISCORD_MCP_TRANSPORT=http` or `stdio`
- HTTP URLs: `SASEQ_MCP_HTTP_URL` (default `http://127.0.0.1:8085/mcp`) / `BRAINDAO_MCP_HTTP_URL`
- Stdio: `DISCORD_MCP_STDIO_COMMAND` is **required** when `transport=stdio` (SaseQ has no default package; BrainDAO users can set `npx -y @iqai/mcp-discord`)

Run the upstream MCP server separately (per their docs), then point this bridge at it. Catalog discovery happens at runtime; tool names are normalized by adapters. Transports perform standard MCP `initialize` / `notifications/initialized` sequencing; stdio matches JSON-RPC responses by request id.

### Puppetmaster model pin

| Field | Value |
|-------|-------|
| Canonical Cursor model (receipts/audit) | `cursor/grok-4-5` |
| Adapter / CLI model (`puppetmaster cursor --model`) | `grok-4.5` |
| Allowlist | **only** `cursor/grok-4-5` |

Requests for any other model raise an error. There is **no** silent remap. The default backend invokes `puppetmaster cursor …` (not a nonexistent `puppetmaster run --json -` shape). Set `PUPPETMASTER_CWD` to control `--cwd`.

### Optional Marionette backend

Default `AGENT_DISCORD_BACKEND=puppetmaster`. To opt in:

```bash
AGENT_DISCORD_BACKEND=marionette
MARIONETTE_BASE_URL=http://127.0.0.1:8787   # your local Marionette HTTP API
# Optional path overrides (defaults shown):
# MARIONETTE_SESSIONS_PATH=/v1/sessions
# MARIONETTE_JOBS_PATH=/v1/jobs
```

The adapter documents an expected session/job/events/status/cancel contract; it does **not** pretend an unverified endpoint is guaranteed. Missing `MARIONETTE_BASE_URL` or transport failures surface as configuration/transport errors.

## CLI

```text
agent-discord bootstrap [--workspace PATH]
agent-discord check [--allow-empty-token]
agent-discord run TASK --channel-id ID [--message-id ID] [--fake] [--no-discord-post] [--json]
```

Also: `python -m agent_discord …`

## Architecture (small & readable)

```text
CLI → Orchestrator → backend (Puppetmaster CLI default | optional Marionette HTTP | fake)
                  ↘ SQLite (bindings, tasks, runs, events, memory, artifacts,
                            inbound message dedupe, gateway ownership,
                            optional research claims / leases / negatives)
                  ↘ Discord facade → SaseQ | BrainDAO | fake MCP provider
```

- **stdlib-first** core; optional `pytest` for development.
- Explicit typed contracts + dependency injection — tests never need Discord, Cursor, or network.
- Gateway exclusivity is **durable** in SQLite across concurrent local processes (in-memory registry retained for unit tests).
- Inbound Discord message IDs are deduplicated at orchestration level (prior receipt reused or explicit ignored-duplicate result).
- Event/artifact payloads recursively strip forbidden hidden-reasoning keys.
- BrainDAO sampling-compatible ingress is an adapter seam on the same facade.
- **Research memory** (optional orchestration context): typed claims with deterministic fingerprints, atomic leases, provenance/evidence, and queryable negative findings. Ordinary memory recall is unchanged; research metadata is not required for normal tasks.
- **Marionette backend** (optional, explicit opt-in via `AGENT_DISCORD_BACKEND=marionette`): stdlib urllib adapter with configurable endpoint paths and injectable transport for tests. Default remains Puppetmaster. Unconfigured/unavailable Marionette fails closed — no silent fallback. Model field still carries the canonical pin `cursor/grok-4-5` → adapter `grok-4.5`.

## Development / tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use fake MCP, Puppetmaster, and Marionette providers only.

## License

MIT — see [`LICENSE`](LICENSE).
