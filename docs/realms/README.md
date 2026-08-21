# Realms

One Discord channel is one checkout. Bounce `#puppetmaster` / `#dugout` / `#marionette` on the phone. Same Mac.

Hermes steal: platform + channel = room. Not Bot Mode chrome.

## Bind

```text
bind puppetmaster
bind dugout
```

or

```bash
discord-os add realm puppetmaster --channel-id ID
```

or `.env`:

```bash
DISCORD_OS_CHANNELS=puppetmaster:ID,dugout:ID
DISCORD_OS_REPOS=puppetmaster:/Users/you/Projects/Puppetmaster
```

Repos also auto-discover git roots under `~/Projects` for known names (puppetmaster, dugout, marionette, discord-os, wiki).

## Who wins cwd

1. The prompt **names** a repo → that checkout
2. Else the channel bind
3. Else `PUPPETMASTER_CWD` (often the Discord OS runtime — not a product repo)

`.agent-discord` is never the subject repository.

## Code

- `src/agent_discord/host/realms.py` — parse, seed, bind, listen ids
- `src/agent_discord/host/repos.py` — catalog, name match, host reach
- `src/agent_discord/orchestration/listen.py` — `_absorb_bind`
- `src/agent_discord/persistence/sqlite.py` — `merge_binding_metadata` (must merge, not wipe)
