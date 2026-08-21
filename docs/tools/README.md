# Host tools

Workers already have a shell. Named binaries and HTTP recipes are the MCP-equivalent. There is **no MCP bus inside Discord**.

Custom MCPs become whatever CLI the owner already runs.

## Catalog

Discovered when ready:

- `wiki` — HTTP, see [wiki](../wiki/README.md)
- `gh` — if on PATH
- `aws` — if on PATH
- extras from `DISCORD_OS_TOOLS`

```bash
discord-os add github --token ghp_...
# or: gh auth login on this Mac
discord-os add tool aws --bin aws --hint "aws sts get-caller-identity"
discord-os add tool status --url http://127.0.0.1:8000/health
```

JSON in `.env`:

```bash
DISCORD_OS_TOOLS={"aws":{"bin":"aws","hint":"sts"}}
```

Host tool auth is one-time on the Mac. `discord-os add github` writes `GH_TOKEN` into the host `.env` (`~/discord-os/.env` or cwd). Every worker inherits `PATH` plus `GH_TOKEN` / `GITHUB_TOKEN`. Analyze-mode is never told to run `gh` itself — the host runs it when signed in. If `gh` is unsigned, the live card says one line plus how-to, then Done. No worker essay.

The worker prompt says: call these from the shell; do not wait for Cursor MCP.

## Code

- `src/agent_discord/host/tools.py` — catalog + reach block
- `src/agent_discord/host/github.py` — `gh_auth_state`, `host_github_report`
- `src/agent_discord/host/add.py` — `add_tool`, `add_github`
- `src/agent_discord/puppetmaster/backend.py` — `host_reach` in the worker prompt
