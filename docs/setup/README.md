# Setup

First run is three facts: bot token, application id, one staff channel. Then `discord-os setup`.

There is no setup wizard. A 20-prompt walk fights the product (Discord is the screen). `setup` does invite + login helper + HOST card once. Everything else is `discord-os add` or an in-channel `bind`.

## First run

Browser (once):

1. Developer Portal → New Application → Bot → Reset Token → `DISCORD_BOT_TOKEN`
2. Enable Message Content Intent
3. Application ID → `DISCORD_APPLICATION_ID`
4. Copy a private channel ID

Machine:

```bash
pip install discord-os puppetmaster-ai
discord-os bootstrap
# edit .env
discord-os setup --channel-id YOUR_CHANNEL_ID
```

Open the invite. Press On.

## After setup (`add`)

```bash
discord-os add realm puppetmaster --channel-id ID
discord-os add memory --channel-id ID
discord-os add repo dugout --path ~/Projects/dugout
discord-os add wiki --url https://portablellm.wiki/you --token …
discord-os add github --token ghp_...
# or on this Mac: gh auth login
discord-os add tool aws --bin aws --hint "aws sts get-caller-identity"
discord-os add list
```

Realm and memory also write SQLite so a running host sees them on the next poll. Wiki, repo, tool, and github write `.env` — restart the host so the process sees them.

From Discord: `bind puppetmaster`, `bind memory`. Same SQLite rows. No slash `/add`.

## Code

- `src/agent_discord/cli.py` — `cmd_setup`, `cmd_add`
- `src/agent_discord/host/add.py` — dotenv upsert + binds
- `src/agent_discord/bootstrap.py` — workspace + `.env` template
- `src/agent_discord/host/install.py` — login helper
