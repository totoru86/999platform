"""텔레그램 알림 (토큰이 없으면 로그만)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        msg = f"{self.prefix}{text}"
        log.info("알림: %s", msg)
        if not self.enabled:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": msg}, timeout=5)
        except requests.RequestException as e:
            log.warning("텔레그램 전송 실패: %s", e)


class NullNotifier(Notifier):
    def send(self, text: str) -> None:
        log.debug("알림(무시): %s", text)
