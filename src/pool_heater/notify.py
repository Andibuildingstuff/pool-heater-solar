"""Telegram notifications.

A notification failing must never take the control loop down with it -- the
heater being switched correctly matters more than the message about it landing.
Every send is best-effort and returns whether it worked.
"""

from __future__ import annotations

import logging

import requests

from .config import Credentials

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 15


class Notifier:
    def __init__(
        self,
        credentials: Credentials,
        base_url: str = API_BASE,
        session: requests.Session | None = None,
    ):
        self._token = credentials.telegram_token
        self._chat_id = credentials.telegram_chat_id
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        if not self.configured:
            LOGGER.info("no Telegram credentials; notification not sent: %s", text)
            return False
        try:
            response = self._session.post(
                f"{self._base}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("Telegram send failed: %s", exc)
            return False
        if not response.ok:
            LOGGER.warning(
                "Telegram send returned %s: %s", response.status_code, response.text[:200]
            )
            return False
        return True
