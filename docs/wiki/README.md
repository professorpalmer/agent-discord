# Wiki

Portable LLM wiki over HTTP. Same results as wiki MCP. No MCP session inside the Discord worker.

```bash
discord-os add wiki --url https://portablellm.wiki/you --token …
discord-os wiki query "what is Discord OS?"
discord-os wiki search discord-os
```

Workers: `discord-os wiki query "…"`.

## Env

| Key | Role |
|---|---|
| `WIKI_BASE_URL` or `PORTABLE_WIKI_URL` | Base, no trailing slash |
| `WIKI_OWNER_TOKEN` / `OWNER_TOKEN` / `WIKI_SHARE_TOKEN` | Bearer + `X-Share-Token` |

If a token is set and no URL is set, default is `http://127.0.0.1:8000`.

Restart the host after `add wiki` so the running process sees the env.

## Code

- `src/agent_discord/host/wiki.py` — stdlib urllib `POST /wiki/query`, `GET /wiki/search`
- `src/agent_discord/cli.py` — `cmd_wiki`
