# Snowflake objects

Artifacts are Discord objects, not a local disk dump with a progress relay.

```text
bytes → attachment + caption
     → Discord CDN (ephemeral ~24h)
     → durable key = channel_id / message_id / attachment_id (+ sha256)
     → get: re-fetch the message (fresh URL), then download
```

Never persist a CDN URL as the key. Jump links are for humans.

Default cap **10 MiB** (`DISCORD_MAX_OBJECT_BYTES`). Oversize becomes an `overflow` pointer + local stash. No DiscordFS multipart.

Channel id is the ACL. `get` refuses a mismatched caller channel.

```bash
discord-os put PATH --channel-id ID
discord-os get MESSAGE_ID --channel-id ID --out PATH
discord-os ls --channel-id ID
```

## Code

- `src/agent_discord/discord/object_store.py`
- `src/agent_discord/discord/rest.py`
- `src/agent_discord/contracts.py` — `DiscordObjectRef`
