import pytest


@pytest.fixture(autouse=True)
def steward_home(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    monkeypatch.delenv("STEWARD_GITHUB_TOKEN", raising=False)
    return tmp_path


class FakeGitHub:
    """Records every write so tests can check exactly what reached 'GitHub'."""

    def __init__(self):
        self.calls = []
        self._next = 1000

    def add_assignee(self, number, login):
        self.calls.append(("assign", number, login))

    def remove_assignee(self, number, login):
        self.calls.append(("unassign", number, login))

    def comment(self, number, body):
        self._next += 1
        self.calls.append(("comment", number, body))
        return self._next

    def delete_comment(self, cid):
        self.calls.append(("delete_comment", cid))

    def add_label(self, number, label):
        self.calls.append(("label", number, label))

    def remove_label(self, number, label):
        self.calls.append(("unlabel", number, label))

    def set_state(self, number, state):
        self.calls.append(("state", number, state))


@pytest.fixture
def fake_github():
    return FakeGitHub()
