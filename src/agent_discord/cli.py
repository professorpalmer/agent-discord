"""CLI: bootstrap, check, run — dependency-injected for testability."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence, TextIO
from uuid import uuid4

from agent_discord import __version__
from agent_discord.bootstrap import bootstrap_workspace, describe_bootstrap
from agent_discord.config import AppConfig, check_config, load_config
from agent_discord.contracts import PuppetmasterBackend, TaskIntake
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.gateway import SqliteGatewayOwnerRegistry
from agent_discord.discord.providers import select_provider
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.marionette.backend import MarionetteBackend, MarionetteEndpointConfig
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.persistence.research import ResearchMemoryStore
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.backend import PuppetmasterCliBackend
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-discord",
        description=(
            "Local Discord-native agent bridge. Bootstrap your own bot, "
            "workspace, and credentials — not a hosted multi-tenant service."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_boot = sub.add_parser("bootstrap", help="Create local workspace and SQLite DB")
    p_boot.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace directory (default: AGENT_DISCORD_WORKSPACE or .agent-discord)",
    )

    p_check = sub.add_parser("check", help="Validate local configuration")
    p_check.add_argument(
        "--allow-empty-token",
        action="store_true",
        help="Do not require DISCORD_BOT_TOKEN (useful for dry runs)",
    )

    p_run = sub.add_parser("run", help="Dispatch a natural-language task through the bridge")
    p_run.add_argument("task", help="Natural-language task text")
    p_run.add_argument("--channel-id", required=True, help="Discord channel id")
    p_run.add_argument(
        "--workspace-id",
        default="default",
        help="Logical workspace id for bindings/memory (default: default)",
    )
    p_run.add_argument("--guild-id", default=None)
    p_run.add_argument("--thread-id", default=None)
    p_run.add_argument(
        "--message-id",
        default=None,
        help="Optional Discord message id for durable inbound deduplication",
    )
    p_run.add_argument(
        "--fake",
        action="store_true",
        help="Use fake MCP + fake Puppetmaster (no network / no Cursor credits)",
    )
    p_run.add_argument(
        "--no-discord-post",
        action="store_true",
        help="Skip posting progress/receipts to Discord",
    )
    p_run.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable receipt JSON",
    )

    return parser


def cmd_bootstrap(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    result = bootstrap_workspace(workspace=args.workspace)
    print("Bootstrapped local workspace:", file=out)
    print(f"  workspace: {result['workspace']}", file=out)
    print(f"  database:  {result['database']}", file=out)
    print(f"  marker:    {result['marker']}", file=out)
    if result["created_env"]:
        print("  created .env from .env.example — fill in DISCORD_BOT_TOKEN", file=out)
    print(
        "\nAttribution: uses external MCP providers "
        "(https://github.com/SaseQ/discord-mcp, "
        "https://github.com/BrainDAO/mcp-discord) — upstream source is not copied.",
        file=out,
    )
    return 0


def cmd_check(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = load_config()
    problems = check_config(config, require_token=not args.allow_empty_token)
    info = describe_bootstrap(config)
    print(f"workspace:  {config.workspace}", file=out)
    print(f"database:   {config.database_path}", file=out)
    print(f"provider:   {config.discord_mcp_provider} / {config.discord_mcp_transport}", file=out)
    print(f"saseq url:  {config.saseq_mcp_http_url}", file=out)
    print(f"backend:    {config.agent_backend}", file=out)
    print(f"model pin:  {config.puppetmaster_model} (adapter {DEFAULT_MODEL_PIN.adapter_name})", file=out)
    print(f"pm cwd:     {config.puppetmaster_cwd}", file=out)
    if config.agent_backend == "marionette":
        print(f"marionette: {config.marionette_base_url or '(unset)'}", file=out)
    print(f"bootstrapped: {info.get('bootstrapped', False)}", file=out)
    if problems:
        print("Problems:", file=out)
        for p in problems:
            print(f"  - {p}", file=out)
        return 1
    print("OK", file=out)
    return 0


def cmd_run(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = load_config()
    config.workspace.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()

    if args.fake:
        provider = FakeDiscordMCPProvider()
        backend = FakePuppetmasterBackend()
    else:
        provider = select_provider(config)
        backend = _select_backend(config)
        # Enforce pin against config (fail closed; no silent fallback)
        backend.resolve_model(config.puppetmaster_model)

    gateway = SqliteGatewayOwnerRegistry(store)
    discord = DiscordFacade(
        provider,
        gateway=gateway,
        owner_id=f"agent-discord-cli-{os.getpid()}-{uuid4().hex[:8]}",
        bot_token_fingerprint=config.bot_token_fingerprint or "local-dev",
    )
    research = ResearchMemoryStore(store)

    gateway_claimed = False
    try:
        if not args.fake and config.discord_bot_token:
            discord.claim_gateway()
            gateway_claimed = True
        orch = AgentOrchestrator(
            store=store,
            backend=backend,
            discord=discord,
            model=config.puppetmaster_model,
            post_progress_to_discord=not args.no_discord_post,
            research=research,
        )
        intake = TaskIntake(
            text=args.task,
            channel_id=args.channel_id,
            workspace_id=args.workspace_id,
            guild_id=args.guild_id,
            thread_id=args.thread_id,
            message_id=args.message_id,
        )
        receipt = orch.run_task(intake)
    finally:
        try:
            if gateway_claimed:
                discord.release_gateway()
        finally:
            discord.close()
            store.close()

    if args.json:
        payload = {
            "task_id": receipt.task_id,
            "run_id": receipt.run_id,
            "status": receipt.status.value,
            "summary": receipt.summary,
            "error": receipt.error,
            "usage": None
            if receipt.usage is None
            else {
                "model": receipt.usage.model,
                "adapter_name": receipt.usage.adapter_name,
                "input_tokens": receipt.usage.input_tokens,
                "output_tokens": receipt.usage.output_tokens,
            },
        }
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(render_receipt(receipt), file=out)

    return 0 if receipt.status.value == "completed" else 1


def _select_backend(config: AppConfig) -> PuppetmasterBackend:
    """Default remains Puppetmaster; Marionette is explicit opt-in via config."""
    if config.agent_backend == "marionette":
        return MarionetteBackend(
            base_url=config.marionette_base_url,
            pin=DEFAULT_MODEL_PIN,
            endpoints=MarionetteEndpointConfig(
                sessions_path=config.marionette_sessions_path,
                jobs_path=config.marionette_jobs_path,
            ),
            api_token=config.marionette_api_token,
        )
    return PuppetmasterCliBackend(
        cli=config.puppetmaster_cli,
        pin=DEFAULT_MODEL_PIN,
        cwd=config.puppetmaster_cwd,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "run":
        return cmd_run(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
