"""CLI: bootstrap, check, run, listen, put, get, ls — dependency-injected for testability."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional, Sequence, TextIO
from uuid import uuid4

from agent_discord import CLI_NAME, CLI_OWNER_PREFIX, PRODUCT_NAME, __version__
from agent_discord.bootstrap import bootstrap_workspace, describe_bootstrap
from agent_discord.config import (
    AppConfig,
    apply_runtime_secrets,
    check_config,
    discord_token_source,
    keys_dir,
    load_config,
    resolve_compute,
)
from agent_discord.contracts import (
    DiscordObjectRef,
    ObjectNotFoundError,
    PuppetmasterBackend,
    TaskIntake,
    discord_jump_url,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.gateway import SqliteGatewayOwnerRegistry
from agent_discord.discord.object_store import DiscordObjectStore
from agent_discord.discord.providers import select_provider
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.marionette.backend import MarionetteBackend, MarionetteEndpointConfig
from agent_discord.keys.connect import (
    bind_host_key,
    host_provider_secret,
    redeem_pairing_ticket,
)
from agent_discord.keys.vault import KeyVault
from agent_discord.host.install import install_login_host
from agent_discord.host.service import (
    clear_host_meta,
    host_log_path,
    host_run_argv,
    read_host_meta,
    running_host_pid,
    start_detached,
    stop_host,
    write_host_meta,
)
from agent_discord.orchestration.listen import (
    LISTEN_HISTORY_SLACK_MS,
    drain_inbound,
    publish_host_card,
)
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.persistence.research import ResearchMemoryStore
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.agentic import AgenticPuppetmasterBackend
from agent_discord.puppetmaster.backend import PuppetmasterCliBackend
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import AGENTIC_MODEL_PIN, DEFAULT_MODEL_PIN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description=(
            f"{PRODUCT_NAME}: Discord is the harness UI. This process is the "
            "kernel. Artifacts are Discord objects addressed by snowflake IDs — "
            "not a hosted multi-tenant service."
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
    p_check.add_argument(
        "--live",
        action="store_true",
        help="Probe Discord REST /users/@me (proves the bot token; no Gateway)",
    )
    p_check.add_argument(
        "--channel-id",
        default=None,
        help="With --live, GET one channel message (proves read + Message Content Intent)",
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

    p_put = sub.add_parser("put", help="Store a file as a Discord object (attachment + pointer)")
    p_put.add_argument("path", type=Path, help="Local file to upload")
    p_put.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_put.add_argument("--thread-id", default=None)
    p_put.add_argument("--guild-id", default=None)
    p_put.add_argument("--kind", default="blob", help="Object kind label (default: blob)")
    p_put.add_argument(
        "--fake",
        action="store_true",
        help="Use fake MCP (no network); blobs persist under the workspace",
    )
    p_put.add_argument("--json", action="store_true", help="Print pointer JSON (no url key)")

    p_get = sub.add_parser("get", help="Retrieve a Discord object by message id")
    p_get.add_argument("message_id", help="Discord message snowflake")
    p_get.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_get.add_argument("--attachment-id", default=None)
    p_get.add_argument("--out", type=Path, default=None, help="Write bytes to this path")
    p_get.add_argument("--fake", action="store_true", help="Use fake MCP (no network)")
    p_get.add_argument("--json", action="store_true", help="Print pointer JSON (no url key)")

    p_ls = sub.add_parser("ls", help="List Discord object pointers for a channel")
    p_ls.add_argument("--channel-id", required=True, help="Discord channel id")
    p_ls.add_argument("--run-id", default=None, help="Limit to one orchestrator run")
    p_ls.add_argument("--fake", action="store_true", help="Use local workspace only (no network)")
    p_ls.add_argument("--json", action="store_true", help="Print pointer JSON (no url key)")

    p_setup = sub.add_parser(
        "setup",
        help="One-time: invite, login helper, and On/Off panel in Discord",
    )
    p_setup.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_setup.add_argument("--workspace-id", default="default")
    p_setup.add_argument("--json", action="store_true")

    p_host = sub.add_parser(
        "host",
        help="Headless host: On/Off buttons in Discord start and stop work",
    )
    host_sub = p_host.add_subparsers(dest="host_command", required=True)
    p_host_start = host_sub.add_parser("start", help="Detach a host process for one channel")
    p_host_start.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_host_start.add_argument(
        "--workspace-id",
        default="default",
        help="Logical workspace id for bindings/memory (default: default)",
    )
    p_host_start.add_argument("--guild-id", default=None)
    p_host_start.add_argument("--thread-id", default=None)
    p_host_start.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between polls (default: 5)",
    )
    p_host_start.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Messages to read per drain (default: 20)",
    )
    p_host_start.add_argument(
        "--fake",
        action="store_true",
        help="Use fake Discord + fake Puppetmaster (tests)",
    )
    p_host_start.add_argument(
        "--no-discord-post",
        action="store_true",
        help="Skip posting progress/receipts to Discord",
    )
    p_host_start.add_argument("--json", action="store_true")
    host_sub.add_parser("stop", help="Stop the detached host process")
    p_host_status = host_sub.add_parser("status", help="Show whether the host is running and armed")
    p_host_status.add_argument("--json", action="store_true")
    p_host_run = host_sub.add_parser(
        "run",
        help="Foreground host loop (used by host start; prefer host start)",
    )
    p_host_run.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_host_run.add_argument("--workspace-id", default="default")
    p_host_run.add_argument("--guild-id", default=None)
    p_host_run.add_argument("--thread-id", default=None)
    p_host_run.add_argument("--interval", type=float, default=5.0)
    p_host_run.add_argument("--limit", type=int, default=20)
    p_host_run.add_argument("--once", action="store_true")
    p_host_run.add_argument("--fake", action="store_true")
    p_host_run.add_argument("--no-discord-post", action="store_true")
    p_host_run.add_argument("--json", action="store_true")

    p_listen = sub.add_parser(
        "listen",
        help="Foreground drain (debug). Leave-running path is host start.",
    )
    p_listen.add_argument("--channel-id", required=True, help="Discord channel id (ACL)")
    p_listen.add_argument(
        "--workspace-id",
        default="default",
        help="Logical workspace id for bindings/memory (default: default)",
    )
    p_listen.add_argument("--guild-id", default=None)
    p_listen.add_argument("--thread-id", default=None)
    p_listen.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between polls when looping (default: 5)",
    )
    p_listen.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Messages to read per drain (default: 20)",
    )
    p_listen.add_argument(
        "--once",
        action="store_true",
        help="Drain once and exit (tests / one-shot)",
    )
    p_listen.add_argument(
        "--fake",
        action="store_true",
        help="Use fake MCP + fake Puppetmaster (no network / no Cursor credits)",
    )
    p_listen.add_argument(
        "--no-discord-post",
        action="store_true",
        help="Skip posting progress/receipts to Discord",
    )
    p_listen.add_argument("--json", action="store_true", help="Print receipt JSON")

    p_connect = sub.add_parser(
        "connect",
        help="Bind an OpenRouter key from env, a pairing ticket, or stdin",
    )
    p_connect.add_argument("--provider", default="openrouter")
    p_connect.add_argument("--ticket", default=None, help="Pairing ticket from Discord")
    p_connect.add_argument(
        "--from-env",
        action="store_true",
        help="Inherit OPENROUTER_API_KEY from the host environment",
    )
    p_connect.add_argument("--json", action="store_true", help="Print public fingerprint JSON")

    p_status = sub.add_parser("status", help="Show resolved compute mode and public fingerprints")
    p_status.add_argument("--json", action="store_true", help="Print status JSON")

    p_invite = sub.add_parser(
        "invite",
        help="Print the bot invite URL (bot scope only; slash stays opt-in)",
    )
    p_invite.add_argument(
        "--application-id",
        default=None,
        help="Override DISCORD_APPLICATION_ID",
    )
    p_invite.add_argument("--json", action="store_true")

    p_open = sub.add_parser(
        "open",
        help="Open Terminal, files, or an allowlisted URL on this host",
    )
    p_open.add_argument(
        "surface",
        choices=("terminal", "files", "browser"),
        help="Host surface (poverty path; same engine as /open in listen)",
    )
    p_open.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Workspace-relative path, or allowlisted http(s) URL for browser",
    )
    p_open.add_argument("--json", action="store_true")

    p_ix = sub.add_parser(
        "interactions",
        help="Opt-in slash HTTP engine (off by default; does not replace listen)",
    )
    p_ix.add_argument(
        "--register",
        action="store_true",
        help="Register /connect and /open on the application or guild",
    )
    p_ix.add_argument("--guild-id", default=None, help="Register as guild commands (faster)")
    p_ix.add_argument("--serve", action="store_true", help="Serve loopback /interactions")
    p_ix.add_argument("--json", action="store_true")

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
        "\nDefault transport is Discord REST (no MCP, no Gateway). "
        "Optional MCP adapters: SaseQ / BrainDAO — upstream source is not copied.",
        file=out,
    )
    return 0


def cmd_check(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    problems = check_config(config, require_token=not args.allow_empty_token)
    info = describe_bootstrap(config)
    print(f"workspace:  {config.workspace}", file=out)
    print(f"database:   {config.database_path}", file=out)
    print(f"provider:   {config.discord_mcp_provider} / {config.discord_mcp_transport}", file=out)
    if config.discord_mcp_provider == "saseq":
        print(f"saseq url:  {config.saseq_mcp_http_url}", file=out)
    elif config.discord_mcp_provider == "braindao":
        print(f"braindao:   {config.braindao_mcp_http_url}", file=out)
    else:
        print("transport:  Discord REST (no MCP, no Gateway)", file=out)
    resolution = resolve_compute(config)
    print(f"backend:    {config.agent_backend}", file=out)
    print(f"compute:    {resolution.requested} -> {resolution.mode}", file=out)
    if resolution.mode == "agentic":
        print(
            f"model pin:  {resolution.model} (adapter {AGENTIC_MODEL_PIN.adapter_name})",
            file=out,
        )
    else:
        print(
            f"model pin:  {config.puppetmaster_model} (adapter {DEFAULT_MODEL_PIN.adapter_name})",
            file=out,
        )
    print(f"pm cwd:     {config.puppetmaster_cwd}", file=out)
    if config.agent_backend == "marionette":
        print(f"marionette: {config.marionette_base_url or '(unset)'}", file=out)
    print(f"bootstrapped: {info.get('bootstrapped', False)}", file=out)
    if shutil.which(config.puppetmaster_cli) is None:
        print(
            f"note:       {config.puppetmaster_cli} not on PATH "
            "(install puppetmaster-ai for live dispatch)",
            file=out,
        )
    if problems:
        print("Problems:", file=out)
        for p in problems:
            print(f"  - {p}", file=out)
        return 1
    if getattr(args, "live", False):
        if not config.discord_bot_token:
            print("live: DISCORD_BOT_TOKEN is empty", file=sys.stderr)
            return 1
        from agent_discord.discord.rest import fetch_bot_identity

        try:
            identity = fetch_bot_identity(token=config.discord_bot_token)
        except Exception as exc:
            print(f"live: Discord REST failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"live bot:   {identity.get('username') or '?'} ({identity.get('id') or '?'})",
            file=out,
        )
        channel_id = (getattr(args, "channel_id", None) or "").strip()
        if channel_id:
            from agent_discord.discord.rest import list_channel_messages

            try:
                messages = list_channel_messages(
                    token=config.discord_bot_token,
                    channel_id=channel_id,
                    limit=1,
                )
            except Exception as exc:
                print(f"live: channel read failed: {exc}", file=sys.stderr)
                return 1
            has_text = any((m.content or "").strip() for m in messages)
            print(
                f"live chan:  {len(messages)} message(s); "
                f"content={'yes' if has_text else 'empty-or-intent-off'}",
                file=out,
            )
    return 0


def cmd_run(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    config.workspace.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()

    resolution = resolve_compute(config)
    if args.fake:
        provider = FakeDiscordMCPProvider()
        backend = FakePuppetmasterBackend()
    else:
        provider = select_provider(config)
        backend = _select_backend(config)
        # Enforce pin against resolved compute (fail closed; no silent fallback)
        backend.resolve_model(resolution.model)

    gateway = SqliteGatewayOwnerRegistry(store)
    discord = DiscordFacade(
        provider,
        gateway=gateway,
        owner_id=f"{CLI_OWNER_PREFIX}{os.getpid()}-{uuid4().hex[:8]}",
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
            model=config.puppetmaster_model if args.fake else resolution.model,
            post_progress_to_discord=not args.no_discord_post,
            research=research,
            max_object_bytes=config.discord_max_object_bytes,
            workspace=config.workspace,
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
    resolution = resolve_compute(config)
    if resolution.mode == "agentic":
        return AgenticPuppetmasterBackend(
            cli=config.puppetmaster_cli,
            pin=AGENTIC_MODEL_PIN,
            cwd=config.puppetmaster_cwd,
            vault=KeyVault(keys_dir(config)),
        )
    return PuppetmasterCliBackend(
        cli=config.puppetmaster_cli,
        pin=DEFAULT_MODEL_PIN,
        cwd=config.puppetmaster_cwd,
    )


@contextmanager
def _object_runtime(config: AppConfig, *, fake: bool) -> Iterator[tuple[SQLiteStore, DiscordFacade]]:
    config = apply_runtime_secrets(config)
    config.workspace.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()
    if fake:
        provider = FakeDiscordMCPProvider(persist_dir=config.workspace / "fake_discord")
    else:
        provider = select_provider(config)
    discord = DiscordFacade(
        provider,
        gateway=SqliteGatewayOwnerRegistry(store),
        owner_id=f"{CLI_OWNER_PREFIX}{os.getpid()}-{uuid4().hex[:8]}",
        bot_token_fingerprint=config.bot_token_fingerprint or "local-dev",
    )
    claimed = False
    try:
        if not fake and config.discord_bot_token:
            discord.claim_gateway()
            claimed = True
        yield store, discord
    finally:
        try:
            if claimed:
                discord.release_gateway()
        finally:
            discord.close()
            store.close()


def _pointer_json(ref: DiscordObjectRef, **extra: object) -> dict[str, object]:
    payload = asdict(ref)
    payload.pop("url", None)
    payload["jump_url"] = discord_jump_url(ref.guild_id, ref.channel_id, ref.message_id)
    payload.update(extra)
    return payload


def cmd_put(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    path = Path(args.path)
    if not path.is_file():
        print(f"put: file not found: {path}", file=sys.stderr)
        return 1
    config = load_config()
    data = path.read_bytes()
    with _object_runtime(config, fake=args.fake) as (store, discord):
        objects = DiscordObjectStore(
            discord, max_bytes=config.discord_max_object_bytes, workspace=config.workspace
        )
        ref = objects.put(
            data,
            channel_id=args.channel_id,
            filename=path.name,
            kind=args.kind,
            thread_id=args.thread_id,
            guild_id=args.guild_id,
        )
        provenance: dict[str, object] = {"source": "cli-put"}
        if ref.guild_id:
            provenance["guild_id"] = ref.guild_id
        if ref.thread_id:
            provenance["thread_id"] = ref.thread_id
        store.add_artifact(
            artifact_id=uuid4().hex,
            task_id="cli",
            run_id="cli",
            kind=ref.kind,
            path=str(path.resolve()),
            provenance=provenance,
            channel_id=ref.channel_id,
            message_id=ref.message_id,
            attachment_id=ref.attachment_id,
            filename=ref.filename,
            sha256=ref.sha256,
            size=ref.size,
            content_type=ref.content_type,
        )
    if args.json:
        print(json.dumps(_pointer_json(ref), indent=2), file=out)
    else:
        jump = discord_jump_url(ref.guild_id, ref.channel_id, ref.message_id)
        print(f"{ref.kind} {ref.filename} {jump}", file=out)
        print(f"{ref.channel_id}/{ref.message_id}/{ref.attachment_id}", file=out)
        print(ref.sha256, file=out)
    return 0


def cmd_get(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = load_config()
    with _object_runtime(config, fake=args.fake) as (store, discord):
        objects = DiscordObjectStore(
            discord, max_bytes=config.discord_max_object_bytes, workspace=config.workspace
        )
        attachment_id = args.attachment_id or ""
        sha256 = ""
        filename = "object.bin"
        kind = "blob"
        size = 0
        guild_id = None
        thread_id = None
        for row in store.list_objects(args.channel_id, limit=200):
            if row.get("message_id") != args.message_id:
                continue
            if attachment_id and row.get("attachment_id") and row.get("attachment_id") != attachment_id:
                continue
            attachment_id = attachment_id or str(row.get("attachment_id") or "")
            sha256 = str(row.get("sha256") or "")
            filename = str(row.get("filename") or filename)
            kind = str(row.get("kind") or kind)
            try:
                size = int(row.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            guild = row.get("provenance", {}).get("guild_id") if isinstance(row.get("provenance"), dict) else None
            guild_id = str(guild) if guild else None
            thread = row.get("thread_id") or (
                row.get("provenance", {}).get("thread_id") if isinstance(row.get("provenance"), dict) else None
            )
            thread_id = str(thread) if thread else None
            break
        if not attachment_id:
            try:
                msg = discord.get_message(args.channel_id, args.message_id)
            except Exception as exc:
                print(f"get: {exc}", file=sys.stderr)
                return 1
            if not msg.attachments:
                print("get: message has no attachments", file=sys.stderr)
                return 1
            attachment_id = msg.attachments[0].attachment_id
            filename = msg.attachments[0].filename or filename
            size = msg.attachments[0].size or size
        ref = DiscordObjectRef(
            channel_id=args.channel_id,
            message_id=args.message_id,
            attachment_id=attachment_id,
            filename=filename,
            kind=kind,
            size=size,
            sha256=sha256,
            guild_id=guild_id,
            thread_id=thread_id,
        )
        try:
            data = objects.get(ref, channel_id=args.channel_id)
        except (ObjectNotFoundError, ValueError) as exc:
            print(f"get: {exc}", file=sys.stderr)
            return 1
    dest = args.out
    write_bytes = data
    if dest is not None:
        try:
            pointer = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pointer = None
        if isinstance(pointer, dict) and pointer.get("kind") == "overflow":
            stash_rel = str(pointer.get("local_stash") or "")
            stash_path = config.workspace / stash_rel if stash_rel else None
            if stash_path is not None and stash_path.is_file():
                write_bytes = stash_path.read_bytes()
        Path(dest).write_bytes(write_bytes)
    elif args.json:
        pass
    else:
        stream = sys.stdout
        if stream.isatty():
            print("get: refusing to write binary to a tty; pass --out PATH", file=sys.stderr)
            return 2
        stream.buffer.write(data)
    if args.json:
        print(
            json.dumps(
                _pointer_json(
                    DiscordObjectRef(
                        channel_id=ref.channel_id,
                        message_id=ref.message_id,
                        attachment_id=ref.attachment_id,
                        filename=ref.filename,
                        kind=ref.kind,
                        size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                        guild_id=ref.guild_id,
                        thread_id=ref.thread_id,
                        content_type=ref.content_type,
                    ),
                    out=str(dest) if dest is not None else None,
                ),
                indent=2,
            ),
            file=out,
        )
    return 0


def cmd_ls(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = load_config()
    config.workspace.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()
    try:
        rows = store.list_objects(args.channel_id, run_id=args.run_id, limit=50)
    finally:
        store.close()
    items = []
    for row in rows:
        channel_id = str(row.get("channel_id") or args.channel_id)
        message_id = str(row.get("message_id") or "")
        provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        guild = provenance.get("guild_id") if isinstance(provenance, dict) else None
        guild_id = str(guild) if guild else None
        item = {
            "artifact_id": row.get("artifact_id"),
            "kind": row.get("kind"),
            "channel_id": channel_id,
            "message_id": message_id,
            "attachment_id": row.get("attachment_id"),
            "filename": row.get("filename"),
            "sha256": row.get("sha256"),
            "size": row.get("size") or 0,
            "jump_url": discord_jump_url(guild_id, channel_id, message_id) if message_id else "",
        }
        items.append(item)
    if args.json:
        print(json.dumps(items, indent=2), file=out)
    else:
        for item in items:
            print(
                f"{item['kind']} {item['filename']} {item['jump_url']} "
                f"{item['channel_id']}/{item['message_id']}/{item['attachment_id']}",
                file=out,
            )
    return 0


def cmd_listen(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    config.workspace.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()
    resolution = resolve_compute(config)
    if args.fake:
        provider = FakeDiscordMCPProvider(persist_dir=config.workspace / "fake_discord")
        backend = FakePuppetmasterBackend()
    else:
        provider = select_provider(config)
        backend = _select_backend(config)
        backend.resolve_model(resolution.model)
    discord = DiscordFacade(
        provider,
        gateway=SqliteGatewayOwnerRegistry(store),
        owner_id=f"{CLI_OWNER_PREFIX}{os.getpid()}-{uuid4().hex[:8]}",
        bot_token_fingerprint=config.bot_token_fingerprint or "local-dev",
    )
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=discord,
        model=config.puppetmaster_model if args.fake else resolution.model,
        post_progress_to_discord=not args.no_discord_post,
        research=ResearchMemoryStore(store),
        max_object_bytes=config.discord_max_object_bytes,
        workspace=config.workspace,
    )
    claimed = False
    exit_code = 0
    panel_stop = threading.Event()
    discord_down = threading.Event()
    ignore_history_before_ms = int(time.time() * 1000) - LISTEN_HISTORY_SLACK_MS
    try:
        # Local process lock. Message intake stays REST. Host run opens a
        # Discord Gateway only so On/Off buttons work (no public URL).
        if not args.fake and config.discord_bot_token:
            discord.claim_gateway()
            claimed = True
        if getattr(args, "announce_host", False):
            write_host_meta(
                config.workspace,
                pid=os.getpid(),
                channel_id=args.channel_id,
            )
            store.set_host_control(args.channel_id, default_armed=False)
            if not args.no_discord_post:
                publish_host_card(
                    discord,
                    store,
                    args.channel_id,
                    thread_id=args.thread_id,
                )
            if (
                not args.fake
                and config.discord_bot_token
                and config.discord_mcp_provider == "rest"
            ):
                _start_panel_gateway(
                    token=config.discord_bot_token,
                    store=store,
                    channel_id=args.channel_id,
                    stop=panel_stop,
                    discord_down=discord_down,
                )
        while True:
            if discord_down.is_set():
                exit_code = 1
                break
            receipts = drain_inbound(
                orch,
                discord,
                channel_id=args.channel_id,
                workspace_id=args.workspace_id,
                guild_id=args.guild_id,
                thread_id=args.thread_id,
                limit=args.limit,
                workspace=config.workspace,
                since_ms=ignore_history_before_ms,
                host_roots=(
                    (config.puppetmaster_cwd, config.workspace)
                    if config.host_actions
                    else ()
                ),
            )
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "task_id": r.task_id,
                                "run_id": r.run_id,
                                "status": r.status.value,
                                "summary": r.summary,
                                "error": r.error,
                            }
                            for r in receipts
                        ],
                        indent=2,
                    ),
                    file=out,
                )
            else:
                for receipt in receipts:
                    print(render_receipt(receipt), file=out)
            if any(r.status.value != "completed" for r in receipts):
                exit_code = 1
            if args.once:
                break
            time.sleep(max(0.2, float(args.interval)))
    finally:
        try:
            if claimed:
                discord.release_gateway()
        finally:
            panel_stop.set()
            if getattr(args, "announce_host", False):
                meta = read_host_meta(config.workspace)
                try:
                    owned = int(meta.get("pid") or 0) == os.getpid()
                except (TypeError, ValueError):
                    owned = False
                if owned:
                    clear_host_meta(config.workspace)
            discord.close()
            store.close()
    return exit_code


def _start_panel_gateway(
    *,
    token: str,
    store: SQLiteStore,
    channel_id: str,
    stop: threading.Event,
    discord_down: threading.Event,
) -> None:
    def on_dispatch(event: str, payload: dict) -> None:
        if event != "INTERACTION_CREATE":
            return
        from agent_discord.host.panel import handle_gateway_interaction

        handle_gateway_interaction(store, channel_id, payload)

    def loop() -> None:
        from agent_discord.discord.realtime import GatewayClosed, run_discord_gateway

        while not stop.is_set():
            try:
                run_discord_gateway(token, on_dispatch, stop=stop)
            except GatewayClosed as exc:
                if exc.fatal:
                    try:
                        store.set_host_control(channel_id, armed=False)
                    except Exception:
                        pass
                    discord_down.set()
                    return
                time.sleep(2)
            else:
                return

    threading.Thread(target=loop, name="discord-os-panel", daemon=True).start()


def cmd_setup(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    if not describe_bootstrap(config).get("bootstrapped"):
        bootstrap_workspace(workspace=config.workspace)
        config = apply_runtime_secrets(load_config())
    problems = check_config(config, require_token=True)
    if problems:
        print("setup: fix these first:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    from agent_discord.discord.invite import InviteError, bot_invite_url
    from agent_discord.keys.connect import bind_host_key, host_provider_secret

    if host_provider_secret("openrouter"):
        bind_host_key(workspace=config.workspace, provider="openrouter", source="env")
    invite = ""
    try:
        invite = bot_invite_url(config.discord_application_id)
    except InviteError:
        invite = ""
    start_args = argparse.Namespace(
        channel_id=args.channel_id,
        workspace_id=args.workspace_id,
        guild_id=None,
        thread_id=None,
        interval=5.0,
        limit=20,
        fake=False,
        no_discord_post=False,
        json=args.json,
    )
    code = cmd_host_start(start_args, out=out)
    if not args.json and invite:
        print(f"invite: {invite}", file=out)
        print("Press On in the HOST card after the bot is in the channel.", file=out)
    return code


def cmd_host(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    command = getattr(args, "host_command", None)
    if command == "start":
        return cmd_host_start(args, out=out)
    if command == "stop":
        return cmd_host_stop(args, out=out)
    if command == "status":
        return cmd_host_status(args, out=out)
    if command == "run":
        args.announce_host = True
        return cmd_listen(args, out=out)
    print("host: start, stop, status, or run", file=sys.stderr)
    return 2


def cmd_host_start(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    config.workspace.mkdir(parents=True, exist_ok=True)
    (config.workspace / "logs").mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(config.database_path)
    store.initialize()
    control =     store.set_host_control(args.channel_id, default_armed=False)
    installed = install_login_host(
        channel_id=args.channel_id,
        workspace=config.workspace,
        cwd=Path.cwd(),
    )
    live = running_host_pid(config.workspace)
    if live is not None:
        print(f"host: already running pid={live}", file=sys.stderr)
        store.close()
        return 1
    for _ in range(8):
        time.sleep(0.15)
        live = running_host_pid(config.workspace)
        if live is not None:
            store.close()
            payload = {
                "pid": live,
                "channel_id": args.channel_id,
                "armed": bool(control.get("armed")),
                "log": str(host_log_path(config.workspace)),
                "login": installed,
            }
            if args.json:
                print(json.dumps(payload, indent=2), file=out)
            else:
                print(f"host running pid={live} channel={args.channel_id}", file=out)
                print("Discord: press On to start, Off to stop", file=out)
            return 0
    extra = [
        "--interval",
        str(args.interval),
        "--limit",
        str(args.limit),
        "--workspace-id",
        args.workspace_id,
    ]
    if args.guild_id:
        extra.extend(["--guild-id", args.guild_id])
    if args.thread_id:
        extra.extend(["--thread-id", args.thread_id])
    if args.fake:
        extra.append("--fake")
    if args.no_discord_post:
        extra.append("--no-discord-post")
    pid = start_detached(
        host_run_argv(args.channel_id, extra=extra),
        workspace=config.workspace,
        channel_id=args.channel_id,
        cwd=Path.cwd(),
    )
    store.close()
    payload = {
        "pid": pid,
        "channel_id": args.channel_id,
        "armed": bool(control.get("armed")),
        "log": str(host_log_path(config.workspace)),
        "login": installed,
    }
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(f"host started pid={pid} channel={args.channel_id}", file=out)
        print("Discord: press On to start, Off to stop", file=out)
        print(f"log: {payload['log']}", file=out)
    return 0


def cmd_host_stop(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = load_config()
    pid = stop_host(config.workspace)
    if pid is None:
        print("host: not running", file=out)
        return 0
    print(f"host stopped pid={pid}", file=out)
    return 0


def cmd_host_status(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    meta = read_host_meta(config.workspace)
    pid = running_host_pid(config.workspace)
    channel_id = str(meta.get("channel_id") or "")
    store = SQLiteStore(config.database_path)
    store.initialize()
    armed = store.host_is_armed(channel_id, default=True) if channel_id else None
    store.close()
    payload = {
        "running": pid is not None,
        "pid": pid,
        "channel_id": channel_id,
        "armed": armed,
        "log": str(host_log_path(config.workspace)),
    }
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        state = "running" if pid is not None else "stopped"
        power = "on" if armed else "off" if armed is False else "n/a"
        print(f"host:   {state}" + (f" pid={pid}" if pid else ""), file=out)
        print(f"power:  {power}", file=out)
        if channel_id:
            print(f"channel:{channel_id}", file=out)
    return 0


def cmd_connect(args: argparse.Namespace, *, out: TextIO | None = None, stdin: TextIO | None = None) -> int:
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    config = load_config()
    config.workspace.mkdir(parents=True, exist_ok=True)
    provider = (args.provider or "openrouter").strip().lower()
    if args.ticket:
        claimed = redeem_pairing_ticket(config.workspace, args.ticket)
        if claimed is None:
            print("connect: ticket is missing or expired", file=sys.stderr)
            return 1
        provider = claimed.get("provider") or provider
        secret = stdin.readline().strip()
        if not secret:
            print("connect: paste the key on stdin", file=sys.stderr)
            return 1
        result = bind_host_key(
            workspace=config.workspace,
            provider=provider,
            secret=secret,
            source="ticket",
        )
    elif args.from_env:
        result = bind_host_key(
            workspace=config.workspace,
            provider=provider,
            source="env",
        )
    elif not stdin.isatty():
        secret = stdin.read().strip()
        if not secret:
            if host_provider_secret(provider):
                result = bind_host_key(
                    workspace=config.workspace,
                    provider=provider,
                    source="env",
                )
            else:
                print("connect: no OpenRouter key; run with --from-env or paste on stdin", file=sys.stderr)
                return 1
        else:
            result = bind_host_key(
                workspace=config.workspace,
                provider=provider,
                secret=secret,
                source="cli",
            )
    elif host_provider_secret(provider):
        result = bind_host_key(
            workspace=config.workspace,
            provider=provider,
            source="env",
        )
    else:
        print("connect: no OpenRouter key; run with --from-env or paste on stdin", file=sys.stderr)
        return 1
    if result.error and not result.stored:
        print(f"connect: {result.error}", file=sys.stderr)
        return 1
    payload = {
        "provider": result.provider,
        "source": result.source,
        "fingerprint": result.fingerprint,
    }
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(
            f"connected {result.provider} source={result.source} fingerprint=…{result.fingerprint}",
            file=out,
        )
    return 0


def cmd_status(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    loaded = load_config()
    token_source = discord_token_source(loaded)
    config = apply_runtime_secrets(loaded)
    resolution = resolve_compute(config)
    vault = KeyVault(keys_dir(config))
    providers = vault.list_public()
    if config.openrouter_env_fingerprint and not any(
        item.get("provider") == "openrouter" for item in providers
    ):
        providers.append(
            {
                "provider": "openrouter",
                "source": "env",
                "fingerprint": config.openrouter_env_fingerprint,
                "created_at": "",
            }
        )
    if config.discord_mcp_provider == "saseq":
        mcp_url = config.saseq_mcp_http_url
    elif config.discord_mcp_provider == "braindao":
        mcp_url = config.braindao_mcp_http_url
    else:
        mcp_url = "https://discord.com/api/v10"
    payload = {
        "product": PRODUCT_NAME,
        "cli": CLI_NAME,
        "compute": resolution.requested,
        "compute_resolved": resolution.mode,
        "model": resolution.model,
        "providers": providers,
        "mcp_url": mcp_url,
        "discord_mcp_provider": config.discord_mcp_provider,
        "discord_max_object_bytes": config.discord_max_object_bytes,
        "discord_token_source": token_source,
        "host_actions": config.host_actions,
        "interactions": config.interactions,
    }
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(f"product:    {PRODUCT_NAME}", file=out)
        print(f"compute:    {resolution.requested} -> {resolution.mode}", file=out)
        print(f"model:      {resolution.model}", file=out)
        print(f"mcp:        {config.discord_mcp_provider} {mcp_url}", file=out)
        print(f"max bytes:  {config.discord_max_object_bytes}", file=out)
        print(f"token src:  {token_source}", file=out)
        print(f"host open:  {'on' if config.host_actions else 'off'}", file=out)
        print(f"slash:      {config.interactions}", file=out)
        if providers:
            print("providers:", file=out)
            for item in providers:
                print(
                    f"  {item['provider']} source={item['source']} fingerprint=…{item['fingerprint']}",
                    file=out,
                )
        else:
            print("providers:  (none)", file=out)
    return 0


def cmd_invite(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    from agent_discord.discord.invite import InviteError, bot_invite_url

    application_id = (args.application_id or config.discord_application_id or "").strip()
    try:
        url = bot_invite_url(application_id)
    except InviteError as exc:
        print(f"invite: {exc}", file=sys.stderr)
        return 1
    payload = {"application_id": application_id, "url": url, "scope": "bot"}
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(url, file=out)
    return 0


def cmd_open(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    if not config.host_actions:
        print("open: host actions are disabled (AGENT_DISCORD_HOST_ACTIONS=off)", file=sys.stderr)
        return 1
    from agent_discord.host.verbs import handle_open_message

    result = handle_open_message(
        f"/open {args.surface} {args.target}".strip(),
        roots=(config.puppetmaster_cwd, config.workspace),
    )
    payload = {
        "surface": result.surface,
        "target": result.target,
        "opened": result.opened,
        "error": result.error,
    }
    if args.json:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(result.card or result.error or f"open {result.surface}", file=out)
    return 0 if result.opened else 1


def cmd_interactions(args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    config = apply_runtime_secrets(load_config())
    from agent_discord.discord.interactions import (
        register_opt_in_commands,
        serve_interactions,
    )

    if config.interactions != "http" and not args.register and not args.serve:
        print(
            "interactions: default is off. Set AGENT_DISCORD_INTERACTIONS=http "
            "and pass --register and/or --serve.",
            file=sys.stderr,
        )
        return 1
    names: list[str] = []
    if args.register:
        try:
            names = register_opt_in_commands(
                token=config.discord_bot_token,
                application_id=config.discord_application_id,
                guild_id=args.guild_id or "",
            )
        except Exception as exc:
            print(f"interactions: register failed: {exc}", file=sys.stderr)
            return 1
    payload = {
        "interactions": config.interactions,
        "registered": names,
        "listen": f"http://{config.interactions_host}:{config.interactions_port}/interactions",
    }
    if args.json and not args.serve:
        print(json.dumps(payload, indent=2), file=out)
    elif names:
        print("registered: " + ", ".join(names), file=out)
    if args.serve:
        if not config.discord_public_key:
            print("interactions: DISCORD_PUBLIC_KEY is required to serve", file=sys.stderr)
            return 1
        server = serve_interactions(
            public_key_hex=config.discord_public_key,
            workspace=config.workspace,
            roots=(config.puppetmaster_cwd, config.workspace),
            host=config.interactions_host,
            port=config.interactions_port,
        )
        print(
            f"serving {payload['listen']}  (tunnel this URL to Discord; Ctrl-C to stop)",
            file=out,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    if not args.register:
        print(
            "interactions: pass --register and/or --serve",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "setup":
        return cmd_setup(args)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "put":
        return cmd_put(args)
    if args.command == "get":
        return cmd_get(args)
    if args.command == "ls":
        return cmd_ls(args)
    if args.command == "host":
        return cmd_host(args)
    if args.command == "listen":
        return cmd_listen(args)
    if args.command == "connect":
        return cmd_connect(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "invite":
        return cmd_invite(args)
    if args.command == "open":
        return cmd_open(args)
    if args.command == "interactions":
        return cmd_interactions(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
