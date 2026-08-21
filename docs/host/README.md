# Host

The host is the long-running process on this Mac. Discord is only the remote.

`discord-os setup` / `host start` detaches it and posts the HOST card: On, Off, Ask, a More menu (Pair / Halt / Gate / Roles / GitHub / Files / Terminal / Browser), and Recent jobs. Message intake is REST. A Gateway is open **only** so those controls work. Do not run a second bot process on the same token. Discord has no tabs — the More select is the grouping.

## Power

| Discord | Meaning |
|---|---|
| On | Armed. First click may become owner. |
| Off → Confirm | Disarmed. Helper stays so On still works. |
| Ask | Prompt into that channel. |
| Pair | Owner / operators. |
| Halt | Spend cap. `discord-os spend --resume` clears it. |

Work is accepted only while On, and only from a paired operator after the first pair.

## Login helper

macOS LaunchAgent (`com.discord-os.host`) or the Windows equivalent from `host/install.py`. After a PyPI bump, install into that venv and kick the helper.

```bash
discord-os host status
discord-os host stop
discord-os host start --channel-id ID
```

## Other host verbs

- `/open terminal|files|browser` — local surfaces, path-confined
- `schedule every 1h: run tests` — SQLite cron, listen loop fires it
- voice memo — local whisper CLI if on PATH

## Code

- `src/agent_discord/host/panel.py` — HOST card, Ask channel
- `src/agent_discord/host/power.py` — armed / pid
- `src/agent_discord/host/install.py` — login item
- `src/agent_discord/host/actions.py` — Terminal / files / browser
- `src/agent_discord/cli.py` — `cmd_host_*`, `cmd_setup`
