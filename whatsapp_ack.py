"""WhatsApp ack — instant "received" notice sent when the adapter accepts a press.

Posts directly to the local Baileys bridge (port 3000), the same path the WhatsApp
platform adapter uses, so the ack lands in the group ~1-2s after the ring press,
before the agent has even been scheduled. Best-effort: any failure is logged and
swallowed; a dead bridge must never fail a ring press.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

log = logging.getLogger("index01.whatsapp_ack")

BRIDGE_PORT_DEFAULT = 3000


def _to_jid(chat_id: str) -> str:
    """Normalize a chat id to a WhatsApp JID (groups end in @g.us already)."""
    chat_id = chat_id.strip()
    if chat_id.endswith("@g.us") or chat_id.endswith("@s.whatsapp.net"):
        return chat_id
    # Bare phone number → DM jid
    digits = "".join(c for c in chat_id if c.isdigit())
    return f"{digits}@s.whatsapp.net"


async def send_ack(
    session: Optional[aiohttp.ClientSession],
    chat_id: str,
    text: str,
    bridge_port: int = BRIDGE_PORT_DEFAULT,
    timeout: float = 5.0,
) -> bool:
    """Fire-and-forget ack via the Baileys bridge. Returns True on 2xx.

    Retries once after a short delay — the bridge has known brief disconnect
    windows where /send fails with 'Server disconnected'.
    """
    if session is None:
        return False
    url = f"http://127.0.0.1:{bridge_port}/send"
    payload = {"chatId": _to_jid(chat_id), "message": text}
    import asyncio

    for attempt in range(2):
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning("whatsapp ack failed status=%s attempt=%d", resp.status, attempt + 1)
        except Exception as exc:
            log.warning("whatsapp ack error attempt=%d: %s", attempt + 1, exc)
        if attempt == 0:
            await asyncio.sleep(1.5)
    return False
