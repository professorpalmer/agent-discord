# CLI

```text
discord-os bootstrap [--workspace PATH]
discord-os check [--allow-empty-token] [--live] [--channel-id ID]
discord-os setup --channel-id ID
discord-os add realm NAME --channel-id ID
discord-os add memory --channel-id ID
discord-os add repo NAME --path PATH
discord-os add wiki --url URL [--token TOKEN]
discord-os add tool NAME [--bin BIN | --url URL] [--hint TEXT]
discord-os add github [--token TOKEN]
discord-os add list [--json]
discord-os host start|stop|status|run
discord-os run TASK --channel-id ID [--fake] [--json]
discord-os listen --channel-id ID [--once] [--fake]
discord-os wiki query|search …
discord-os recall [QUERY]
discord-os note TEXT [--channel-id ID]
discord-os connect [--from-env] [--ticket T]
discord-os status [--json]
discord-os invite
discord-os open {terminal,files,browser} [PATH_OR_URL]
discord-os pair --user-id ID [--role owner|operator]
discord-os schedule --every 1h --channel-id ID PROMPT
discord-os spend [--cap USD] [--halt] [--resume]
discord-os put|get|ls …
```

`python -m agent_discord` is the same entry. `--fake` is the hermetic path.

Pointer JSON from `put` / `get` / `ls` never includes a `url` key.
