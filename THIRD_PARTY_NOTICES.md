# Third-Party Notices

**Discord OS** is a GitHub-first, open-source Discord harness. It does **not** copy source code from the upstream Discord MCP servers below. It talks to them as optional external processes or HTTP endpoints through a product-owned facade and thin provider adapters. Default I/O is official Discord REST.

## SaseQ / discord-mcp

- Repository: https://github.com/SaseQ/discord-mcp
- License: MIT (see upstream repository)
- Role in this project: optional MCP Discord provider adapter (`saseq`)
- HTTP convention: when `DISCORD_MCP_TRANSPORT=http`, this project expects an MCP-over-HTTP endpoint at `SASEQ_MCP_HTTP_URL` (default `http://127.0.0.1:8085/mcp`, matching upstream docs). Exact tool names and payloads are discovered at runtime via MCP catalog listing; this repository does not vendor upstream tool implementations.
- Stdio convention: when `DISCORD_MCP_TRANSPORT=stdio`, **`DISCORD_MCP_STDIO_COMMAND` is required**. There is no fabricated default npm package for SaseQ; prefer the upstream HTTP/Docker profile.

## BrainDAO / mcp-discord (`@iqai/mcp-discord`)

- Repository: https://github.com/BrainDAO/mcp-discord
- Package family: `@iqai/mcp-discord` (see upstream)
- License: MIT (see upstream repository)
- Role in this project: optional MCP Discord provider adapter (`braindao`)
- Sampling / tool convention: BrainDAO sampling-compatible ingress is exposed as an **adapter seam** on the product facade. This project models **one active Gateway owner per bot token** and does **not** require a second Discord Gateway solely to accept sampling-compatible tool traffic.
- HTTP / stdio: same transport knobs as above via `BRAINDAO_MCP_HTTP_URL` / `DISCORD_MCP_STDIO_COMMAND`. For stdio, set an explicit command such as the documented `npx -y @iqai/mcp-discord`.

## Puppetmaster

- Puppetmaster is an external local harness/CLI used as the **default** backend for agent runs.
- This project invokes the public Cursor worker CLI: `puppetmaster cursor --model grok-4.5 …`.
- Receipts and audit keep the canonical pin `cursor/grok-4-5` (adapter name `grok-4.5`) with an allowlist containing only that model. There is **no silent model fallback**.
- Do not edit Puppetmaster or Marionette from this repository.

## Marionette (optional)

- Marionette is an optional external HTTP worker API. This repository ships a thin adapter only (`AGENT_DISCORD_BACKEND=marionette`).
- Endpoint paths are configurable (`MARIONETTE_BASE_URL`, `MARIONETTE_SESSIONS_PATH`, `MARIONETTE_JOBS_PATH`). The adapter documents an expected contract; it does not vendor Marionette and does not claim an unverified upstream shape is guaranteed.
- When selected without a base URL, or when the transport fails, the bridge fails closed. There is **no silent fallback** to Puppetmaster.
- The model field on Marionette dispatches still carries the canonical pin `cursor/grok-4-5` / adapter `grok-4.5`.

## Other

- Python standard library and optional `pytest` for development tests.
- No Discord Gateway library is vendored here; Discord I/O goes through the selected MCP provider.
