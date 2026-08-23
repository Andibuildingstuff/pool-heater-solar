"""Telling you what happened.

Notifications are best-effort by design. A heater switched correctly with no
message about it is a far better outcome than a heater left alone because the
messaging failed, so every send here catches its own errors and reports whether
it worked rather than raising.

Three channels, chosen by what is configured:

* GitHub issue -- the default in Actions. One issue per season, a comment per
  event. No account, no credentials beyond the token the workflow already has,
  and the thread becomes a searchable record of every decision.
* Telegram -- a real push notification, if you want one.
* Logging -- the fallback, so a misconfigured channel is visible in the run log
  rather than silent.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from .config import Credentials

LOGGER = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
GITHUB_API = "https://api.github.com"
TIMEOUT = 15

ISSUE_TITLE = "Pool heater log ({year} season)"
ISSUE_BODY = """\
This issue is the pool heater automation's logbook. It comments here whenever it
switches the heater, refuses to, or hits a problem.

Closing it is harmless: a new one is opened on the next event. Nothing reads the
comments back, so feel free to reply.
"""


class LoggingNotifier:
    """Says it out loud in the run log. Used when nothing else is configured."""

    configured = False

    def send(self, text: str) -> bool:
        LOGGER.info("notification (no channel configured): %s", text)
        return False


class TelegramNotifier:
    def __init__(self, credentials: Credentials, base_url: str = TELEGRAM_API, session=None):
        self._token = credentials.telegram_token
        self._chat_id = credentials.telegram_chat_id
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        try:
            response = self._session.post(
                f"{self._base}/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("Telegram send failed: %s", exc)
            return False
        if not response.ok:
            LOGGER.warning("Telegram returned %s: %s", response.status_code, response.text[:200])
            return False
        return True


class GitHubIssueNotifier:
    """Comments on one long-running issue, opening it the first time.

    The issue is found by title rather than by a stored id, so it survives a lost
    state file and there is nothing extra to keep in sync. The lookup happens
    once per process; in the control loop that is once an hour, not once a cycle.
    """

    def __init__(self, repository: str, token: str, base_url: str = GITHUB_API, session=None):
        self._repo = repository
        self._token = token
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._issue: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self._repo and self._token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _title(self, when: datetime) -> str:
        return ISSUE_TITLE.format(year=when.year)

    def _find_or_open(self, title: str) -> int | None:
        try:
            response = self._session.get(
                f"{self._base}/repos/{self._repo}/issues",
                params={"state": "open", "per_page": 100},
                headers=self._headers(),
                timeout=TIMEOUT,
            )
            if response.ok:
                for item in response.json():
                    # The issues endpoint returns pull requests too.
                    if "pull_request" in item:
                        continue
                    if item.get("title") == title:
                        return int(item["number"])
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            LOGGER.warning("could not list issues: %s", exc)

        try:
            created = self._session.post(
                f"{self._base}/repos/{self._repo}/issues",
                json={"title": title, "body": ISSUE_BODY},
                headers=self._headers(),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("could not open the log issue: %s", exc)
            return None
        if not created.ok:
            LOGGER.warning(
                "could not open the log issue: %s %s",
                created.status_code, created.text[:200],
            )
            return None
        try:
            return int(created.json()["number"])
        except (ValueError, KeyError, TypeError):
            return None

    def send(self, text: str, now: datetime | None = None) -> bool:
        if not self.configured:
            return False
        when = now or datetime.now()
        if self._issue is None:
            self._issue = self._find_or_open(self._title(when))
        if self._issue is None:
            return False

        body = f"**{when:%Y-%m-%d %H:%M %Z}**\n\n{text}"
        try:
            response = self._session.post(
                f"{self._base}/repos/{self._repo}/issues/{self._issue}/comments",
                json={"body": body},
                headers=self._headers(),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            LOGGER.warning("could not comment on the log issue: %s", exc)
            return False
        if not response.ok:
            LOGGER.warning(
                "could not comment on the log issue: %s %s",
                response.status_code, response.text[:200],
            )
            return False
        return True


def build_notifier(credentials: Credentials, env: dict[str, str] | None = None):
    """Pick a channel from what is actually configured.

    Telegram wins when set, because choosing it is deliberate. The GitHub issue
    is the default inside Actions, where the token and repository are already in
    the environment and cost nothing to use.
    """
    telegram = TelegramNotifier(credentials)
    if telegram.configured:
        return telegram

    environment = os.environ if env is None else env
    issues = GitHubIssueNotifier(
        environment.get("GITHUB_REPOSITORY", ""), environment.get("GITHUB_TOKEN", "")
    )
    if issues.configured:
        return issues

    return LoggingNotifier()
