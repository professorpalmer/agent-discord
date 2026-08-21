# Compute

Not a compute host. Discord OS dispatches Puppetmaster on this Mac.

`AGENT_DISCORD_COMPUTE=auto` (default): agentic `openrouter/auto` when an OpenRouter key is in env or the workspace vault; otherwise the Cursor pin `cursor/grok-4-5` → adapter `grok-4.5`. No silent model fallback. Requests for any other model fail closed.

Questions: `--mode analyze`. File work: `--mode implement --allow-dirty`. Auto writes unless HOST Gate is on.

## Keys

```bash
discord-os connect --from-env
# or /connect in Discord (inherit / ticket / shred)
```

Vault: `{workspace}/keys/`. Key goes into the **subprocess env** as `OPENROUTER_API_KEY`, never argv, never logs.

Optional Marionette HTTP: `AGENT_DISCORD_BACKEND=marionette` plus `MARIONETTE_BASE_URL`. Unconfigured Marionette fails closed.

## Code

- `src/agent_discord/config.py` — `resolve_compute`
- `src/agent_discord/puppetmaster/backend.py` — CLI cursor
- `src/agent_discord/puppetmaster/agentic.py` — OpenRouter
- `src/agent_discord/keys/` — connect + vault
