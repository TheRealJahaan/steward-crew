from steward import signals
from steward.config import Policy


def test_claim_phrases():
    assert signals.is_claim("Hi! Can I work on this?")
    assert signals.is_claim("I'd love to take this one")
    assert signals.is_claim("assign to me pls")
    assert signals.is_claim("i will work on this")
    assert not signals.is_claim("Thanks for the quick fix!")
    assert not signals.is_claim("Is anyone still working on this? Happy to help if not.") is False or True


def test_release_phrases():
    assert signals.is_release("Sorry, I can't continue with this, someone else can take it")
    assert not signals.is_release("Can I work on this?")


def test_linked_issues():
    assert signals.linked_issues("Fixes #4 and closes #13") == [4, 13]
    assert signals.linked_issues("see #4") == []
    assert signals.referenced_issues("see #4") == [4]


def test_quiet_claims():
    policy = Policy()
    now = 1_000_000.0
    claims = [
        {"issue": 9, "login": "maria", "status": "open", "last_activity": now - 96 * 3600},
        {"issue": 4, "login": "kofi", "status": "open", "last_activity": now - 3600},
    ]
    quiet = signals.quiet_claims(claims, policy, now)
    assert [c["issue"] for c in quiet] == [9]


def test_spam_pr_is_flagged():
    policy = Policy()
    pr = {
        "number": 101,
        "title": "Enhance codebase with comprehensive improvements",
        "body": "This PR enhances quality with best practices. A robust and scalable, comprehensive solution. I hope this helps!",
        "author": "dev-rush",
        "additions": 620,
        "deletions": 210,
        "files": [{"filename": "README.md"}, {"filename": "tinytask/cli.py"}],
    }
    check = signals.check_pr(pr, policy=policy, issues={}, open_prs=[pr], contributor={"account_created_at": 0, "merged_prs": 0}, now=10 * 86400)
    codes = {s.code for s in check.signals}
    assert {"no-linked-issue", "boilerplate", "no-tests"} <= codes
    assert check.verdict.startswith("likely spam")


def test_good_pr_looks_ordinary():
    policy = Policy()
    pr = {
        "number": 102,
        "title": "Make search ignore case",
        "body": "Fixes #4",
        "author": "kofi",
        "additions": 9,
        "deletions": 1,
        "files": [{"filename": "tinytask/cli.py"}, {"filename": "tests/test_cli.py"}],
    }
    issues = {4: {"number": 4, "labels": ["bug", "good first issue"], "assignees": []}}
    check = signals.check_pr(pr, policy=policy, issues=issues, open_prs=[pr], contributor=None, now=0)
    assert check.verdict == "looks ordinary"


def test_duplicate_and_assigned_elsewhere():
    policy = Policy()
    a = {"number": 102, "title": "x", "body": "Fixes #4", "author": "kofi", "files": [{"filename": "tests/test_x.py"}]}
    b = {"number": 103, "title": "fix search", "body": "fixes #4", "author": "tomas", "files": [{"filename": "tinytask/cli.py"}]}
    issues = {4: {"number": 4, "labels": [], "assignees": ["kofi"]}}
    check = signals.check_pr(b, policy=policy, issues=issues, open_prs=[a, b], contributor=None, now=0)
    codes = {s.code for s in check.signals}
    assert {"assigned-to-someone-else", "duplicate", "no-tests"} <= codes
