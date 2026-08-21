# Discord OS

Discord is the screen. This process is the computer. Your phone is the remote.

```bash
pip install discord-os
```

Your bot, on your machine. No hosted fleet. Leave the host running; turn work on and off from Discord.

Workers need [Puppetmaster](https://pypi.org/project/puppetmaster-ai/) too:

```bash
pip install discord-os puppetmaster-ai
```

The `agent-discord` command still works. Full setup, Discord verbs, and docs live in the repo — PyPI does not host those pages.

- [README](https://github.com/professorpalmer/discord-os/blob/master/README.md)
- [Setup](https://github.com/professorpalmer/discord-os/blob/master/docs/setup/README.md)
- [Docs index](https://github.com/professorpalmer/discord-os/blob/master/docs/README.md)
- [AGENTS.md](https://github.com/professorpalmer/discord-os/blob/master/AGENTS.md)
- [Changelog](https://github.com/professorpalmer/discord-os/blob/master/CHANGELOG.md)

![HOST card and a finished job thread](https://raw.githubusercontent.com/professorpalmer/discord-os/master/docs/screenshots/discord-host.png)

![Two asks at once, live thinking card](https://raw.githubusercontent.com/professorpalmer/discord-os/master/docs/screenshots/discord-jobs.png)

## First run

Once, in a browser: [Discord Developer Portal](https://discord.com/developers/applications) → New Application → **Bot** → Reset Token (`DISCORD_BOT_TOKEN`) → enable **Message Content Intent** → General Information → Application ID (`DISCORD_APPLICATION_ID`). Copy a private channel ID.

Then on the machine that will do the work (Python 3.11+):

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install discord-os puppetmaster-ai
export OPENROUTER_API_KEY=...      # never commit
discord-os bootstrap
# put DISCORD_BOT_TOKEN and DISCORD_APPLICATION_ID in .env
discord-os setup --channel-id YOUR_CHANNEL_ID
```

Open the invite URL, press **On**. After that, `discord-os add` and in-channel `bind` — not a wizard. See [docs/setup](https://github.com/professorpalmer/discord-os/blob/master/docs/setup/README.md).

MIT — [LICENSE](https://github.com/professorpalmer/discord-os/blob/master/LICENSE).
