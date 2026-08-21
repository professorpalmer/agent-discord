# Think-tank memory

Other Discord channels are durable group-chat memory. Hermes persist-then-settle: the job finishes, then a short note lands in the **other** bank rooms, not the origin.

## Bind

```text
bind memory
```

```bash
discord-os add memory --channel-id ID
```

```bash
DISCORD_OS_MEMORY=ID,ID
```

## Recall / note

```bash
discord-os recall "bind memory"
discord-os note "staff uses bind memory" --channel-id ID
```

Recall skips harness `**Card**` / `**Receipt**` lines. It also reads SQLite `source=think-tank` rows so a `note` is findable even though the Discord post is a NOTE card.

Injected into the next worker as memories with `source=think-tank`.

## Code

- `src/agent_discord/host/memory.py` — bind, recall, settle
- `src/agent_discord/orchestration/cards.py` — `note_card`
- `src/agent_discord/orchestration/listen.py` — bind memory intercept
