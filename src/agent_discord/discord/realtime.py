"""Discord Gateway for button clicks. No public URL. Does not poll messages."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.layout import presence_update
from agent_discord.discord.rest import DISCORD_API_BASE, USER_AGENT
from agent_discord.discord.ws import WebSocketClient, WebSocketError


DispatchHandler = Callable[[str, dict[str, Any]], None]
PresenceSender = Callable[[str, str], None]
JsonSocket = Any

INTENTS_GUILDS = 1
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4013, 4014})


class GatewayClosed(RuntimeError):
    """Gateway session ended. Fatal closes should stop work."""

    def __init__(self, reason: str, *, fatal: bool = False) -> None:
        super().__init__(reason)
        self.fatal = fatal


def fetch_gateway_url(*, opener: Optional[Callable[..., Any]] = None) -> str:
    request = Request(
        f"{DISCORD_API_BASE}/gateway",
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    do_open = opener or urlopen
    try:
        with do_open(request, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ToolInvocationError("Discord gateway URL lookup failed") from exc
    url = str((raw or {}).get("url") or "").strip()
    if not url.startswith("wss://"):
        raise ToolInvocationError("Discord gateway URL was missing")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v=10&encoding=json"


def run_discord_gateway(
    token: str,
    on_dispatch: DispatchHandler,
    *,
    stop: Optional[threading.Event] = None,
    connect: Optional[Callable[[str], JsonSocket]] = None,
    gateway_url: Optional[str] = None,
    heartbeat_scale: float = 0.9,
    on_connected: Optional[Callable[[PresenceSender], None]] = None,
    presence_status: str = "idle",
    presence_name: str = "Discord OS",
) -> None:
    """Identify, heartbeat, and forward DISPATCH events until stop or fatal close.

    Message intake stays on REST. This socket exists so On/Off buttons work
    without a public Interactions URL.
    """

    halt = stop or threading.Event()
    url = gateway_url or fetch_gateway_url()
    opener = connect or WebSocketClient.connect
    sock = opener(url)
    seq: Optional[int] = None
    beat_stop = threading.Event()
    beater: Optional[threading.Thread] = None

    def send(payload: dict[str, Any]) -> None:
        sock.send_text(json.dumps(payload, separators=(",", ":")))

    try:
        while not halt.is_set():
            raw = sock.recv_text(timeout=1.0)
            if raw is None:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            op = message.get("op")
            data = message.get("d")
            if message.get("s") is not None:
                try:
                    seq = int(message["s"])
                except (TypeError, ValueError):
                    pass
            if op == 10:
                interval_ms = 41250
                if isinstance(data, dict):
                    try:
                        interval_ms = int(data.get("heartbeat_interval") or interval_ms)
                    except (TypeError, ValueError):
                        pass
                if beater is None:
                    beater = threading.Thread(
                        target=_heartbeat_loop,
                        args=(send, beat_stop, interval_ms, lambda: seq, heartbeat_scale),
                        daemon=True,
                    )
                    beater.start()
                send(
                    {
                        "op": 2,
                        "d": {
                            "token": token,
                            "intents": INTENTS_GUILDS,
                            "properties": {
                                "os": "unknown",
                                "browser": "discord-os",
                                "device": "discord-os",
                            },
                            "presence": presence_update(
                                status=presence_status,
                                name=presence_name,
                            )["d"],
                        },
                    }
                )
                continue
            if op == 9:
                raise GatewayClosed("identify rejected", fatal=True)
            if op == 0:
                event = str(message.get("t") or "")
                payload = data if isinstance(data, dict) else {}
                if event == "READY":
                    print("panel gateway ready", flush=True)
                    if on_connected is not None:
                        def _set_presence(status: str, name: str) -> None:
                            try:
                                send(presence_update(status=status, name=name))
                            except Exception:
                                pass

                        try:
                            on_connected(_set_presence)
                        except Exception:
                            pass
                try:
                    on_dispatch(event, payload)
                except Exception:
                    pass
    except WebSocketError as exc:
        raise GatewayClosed(str(exc), fatal=False) from exc
    finally:
        beat_stop.set()
        closer = getattr(sock, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def _heartbeat_loop(
    send: Callable[[dict[str, Any]], None],
    stop: threading.Event,
    interval_ms: int,
    seq_reader: Callable[[], Optional[int]],
    scale: float,
) -> None:
    delay = max(1.0, (interval_ms / 1000.0) * float(scale))
    while not stop.wait(delay):
        try:
            send({"op": 1, "d": seq_reader()})
        except Exception:
            return
        time.sleep(0)
