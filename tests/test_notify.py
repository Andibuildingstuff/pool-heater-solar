"""Notification channels, and how one is chosen."""

from __future__ import annotations

from datetime import datetime

from pool_heater.config import Credentials
from pool_heater.notify import (
    GitHubIssueNotifier,
    LoggingNotifier,
    TelegramNotifier,
    build_notifier,
)
from test_clients import FakeResponse, FakeSession


# --- choosing a channel ---------------------------------------------------------


def test_nothing_configured_falls_back_to_logging():
    notifier = build_notifier(Credentials(), env={})
    assert isinstance(notifier, LoggingNotifier)
    assert notifier.send("hello") is False


def test_inside_actions_the_github_issue_is_the_default():
    notifier = build_notifier(
        Credentials(), env={"GITHUB_REPOSITORY": "andi/pool", "GITHUB_TOKEN": "t"}
    )
    assert isinstance(notifier, GitHubIssueNotifier)


def test_telegram_wins_when_deliberately_configured():
    credentials = Credentials(telegram_token="tok", telegram_chat_id="42")
    notifier = build_notifier(
        credentials, env={"GITHUB_REPOSITORY": "andi/pool", "GITHUB_TOKEN": "t"}
    )
    assert isinstance(notifier, TelegramNotifier)


# --- the issue logbook ----------------------------------------------------------

WHEN = datetime(2026, 8, 23, 12, 0)


def issue_notifier(routes):
    return GitHubIssueNotifier("andi/pool", "tok", session=FakeSession(routes))


def test_the_first_event_opens_the_logbook_and_comments():
    session = FakeSession({
        "/issues/7/comments": FakeResponse(payload={}),
        "/repos/andi/pool/issues": lambda **kw: (
            FakeResponse(payload=[]) if "json" not in kw
            else FakeResponse(payload={"number": 7})
        ),
    })
    notifier = GitHubIssueNotifier("andi/pool", "tok", session=session)
    assert notifier.send("switch ON: surplus held", now=WHEN) is True

    opened = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert opened[0][2]["title"] == "Pool heater log (2026 season)"
    comment = [c for c in session.calls if "/comments" in c[1]][0]
    assert "switch ON" in comment[2]["body"]


def test_an_existing_logbook_is_reused_not_duplicated():
    session = FakeSession({
        "/issues/12/comments": FakeResponse(payload={}),
        "/repos/andi/pool/issues": FakeResponse(payload=[
            {"number": 3, "title": "Some other issue"},
            {"number": 9, "title": "A PR", "pull_request": {}},
            {"number": 12, "title": "Pool heater log (2026 season)"},
        ]),
    })
    notifier = GitHubIssueNotifier("andi/pool", "tok", session=session)
    assert notifier.send("switch OFF: importing", now=WHEN) is True
    assert not any(
        c[0] == "POST" and c[1].endswith("/issues") for c in session.calls
    ), "must comment on the existing issue, not open a second"


def test_the_issue_number_is_remembered_within_the_process():
    calls = {"lists": 0}

    def listing(**kw):
        calls["lists"] += 1
        return FakeResponse(payload=[{"number": 12, "title": "Pool heater log (2026 season)"}])

    session = FakeSession({
        "/issues/12/comments": FakeResponse(payload={}),
        "/repos/andi/pool/issues": listing,
    })
    notifier = GitHubIssueNotifier("andi/pool", "tok", session=session)
    notifier.send("one", now=WHEN)
    notifier.send("two", now=WHEN)
    assert calls["lists"] == 1, "the lookup happens once per process, not per event"


def test_a_github_outage_reports_failure_rather_than_raising():
    notifier = issue_notifier({
        "/repos/andi/pool/issues": FakeResponse(status_code=500, text="down"),
    })
    assert notifier.send("anything", now=WHEN) is False
