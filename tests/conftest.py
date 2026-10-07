import pytest

from wanda.actions.slack import SlackActions

LISTED = SlackActions.conversations


@pytest.fixture(autouse=True)
def _no_conversations(monkeypatch):
    """A start reads back from Slack what was sent while she was not running,
    beginning with the conversations she is in: on the real SlackActions,
    as a daemon test starts it, there are none, so the read calls Slack for
    nothing more. A test of the read's own calls asks for `listed`."""
    async def conversations(self):
        return []
    monkeypatch.setattr(SlackActions, "conversations", conversations)


@pytest.fixture
def listed(monkeypatch):
    """SlackActions.conversations as it is, calling users.conversations."""
    monkeypatch.setattr(SlackActions, "conversations", LISTED)
