# Agents reading Discord OS

Discord is the screen. This process is the computer. The phone is the remote. One Mac. Not isolated VMs.

## Read order

1. [README.md](README.md) — setup, phone verbs, index
2. [docs/README.md](docs/README.md) — feature map
3. The feature folder that matches the ask (`docs/realms`, `docs/jobs`, …)
4. CodeGraph, then the files that page names

Do not crawl the tree first. Each `docs/<feature>/README.md` lists the modules that own that seam.

## Product truths (do not invent)

- Host tools are **CLI or HTTP**. There is no MCP bus inside Discord. Wiki MCP results come from `discord-os wiki query`.
- `bind puppetmaster` / `discord-os add realm` pins a channel to a checkout. Naming a repo in the prompt still overrides.
- Two Discord threads cook at once (`JobPool`). Implement writes serialize per realm.
- One live card per job. Edit in place. Do not flood the thread.
- Artifacts are Discord objects (channel / message / attachment / sha256). Never persist a CDN URL as the key.
- Default I/O is Discord REST. Gateway exists only so On/Off buttons work.
- Python 3.11+. `from __future__ import annotations`. JSON, never YAML. No emojis.

## Commands that matter

```text
discord-os setup --channel-id ID
discord-os add realm|memory|repo|wiki|tool|github|list
discord-os check
discord-os wiki query "…"
```

There is no setup wizard and no slash `/add`. Incremental `add` plus in-channel `bind`.

## Tests

```bash
python -m pytest tests -q
```

Fakes only. Do not point tests at a live bot token.
