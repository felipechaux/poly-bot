"""
ntfy.sh notifier — publish via JSON to the server root (supports UTF-8 titles).
Docs: https://docs.ntfy.sh/publish/#publish-as-json

The topic name is effectively a password on the public server: use a long random one.
"""

from __future__ import annotations

import httpx

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)


class NtfyNotifier:
    def __init__(self, server: str, topic: str, token: str = "", click_url: str = "") -> None:
        self._server = server.rstrip("/")
        self._topic = topic
        self._token = token
        self._click = click_url

    @property
    def enabled(self) -> bool:
        return bool(self._topic)

    async def send(
        self,
        message: str,
        title: str = "",
        tags: list[str] | None = None,
        priority: int = 3,
    ) -> bool:
        if not self.enabled:
            log.info("ntfy.disabled", title=title)
            return False
        body: dict[str, object] = {
            "topic": self._topic,
            "message": message,
            "priority": priority,
        }
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if self._click:
            body["click"] = self._click
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self._server, json=body, headers=headers)
                r.raise_for_status()
            log.info("ntfy.sent", title=title)
            return True
        except httpx.HTTPError as exc:
            log.error("ntfy.failed", error=str(exc))
            return False
