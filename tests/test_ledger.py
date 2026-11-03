from steward import db
from steward.config import Policy
from steward.ledger import Ledger, parse_command


def seed_issue(conn, number=12, source="github"):
    db.upsert_issue(conn, {"number": number, "title": "Export to CSV", "labels": ["enhancement"], "assignees": [], "source": source})


def test_parse_command():
    assert parse_command("approve 7") == ("approve", "7")
    assert parse_command("@ledger approve 7 8") == ("approve", "7 8")
    assert parse_command("*decline* 9 too big") == ("decline", "9 too big")
    assert parse_command("<@U123> queue") == ("queue", "")
    assert parse_command("hello everyone") is None
    assert parse_command("approved by me") is None


def test_approve_assigns_on_github(fake_github):
    with db.session() as conn:
        seed_issue(conn)
        pid = db.add_proposal(conn, "assign", 12, login="ben", text="Hi @ben, it's yours!", reason="asked first", proposed_by="steward")
    ledger = Ledger(Policy(), None, fake_github)
    reply = ledger.handle("Jahaan", f"approve {pid}")
    assert reply.startswith(f"Done P{pid}")
    assert ("assign", 12, "ben") in fake_github.calls
    comment = [c for c in fake_github.calls if c[0] == "comment"][0]
    assert "it's yours" in comment[2] and "approved by a maintainer" in comment[2]
    with db.session() as conn:
        assert db.get_issue(conn, 12)["assignees"] == ["ben"]
        assert db.get_proposal(conn, pid)["status"] == "done"
        assert db.recent_audit(conn)[0]["actor"] == "Jahaan"


def test_nothing_happens_without_approval(fake_github):
    with db.session() as conn:
        seed_issue(conn)
        db.add_proposal(conn, "assign", 12, login="ben", reason="asked first")
    ledger = Ledger(Policy(), None, fake_github)
    assert ledger.handle("Jahaan", "queue").startswith("Waiting for you")
    assert fake_github.calls == []


def test_undo_reverses_exactly(fake_github):
    with db.session() as conn:
        seed_issue(conn)
        pid = db.add_proposal(conn, "assign", 12, login="ben", text="yours", reason="r")
    ledger = Ledger(Policy(), None, fake_github)
    ledger.handle("Jahaan", f"approve {pid}")
    reply = ledger.handle("Jahaan", f"undo {pid}")
    assert reply.startswith(f"Undid P{pid}")
    assert ("unassign", 12, "ben") in fake_github.calls
    assert any(c[0] == "delete_comment" for c in fake_github.calls)
    with db.session() as conn:
        assert db.get_issue(conn, 12)["assignees"] == []


def test_decline_and_edit(fake_github):
    with db.session() as conn:
        seed_issue(conn)
        a = db.add_proposal(conn, "comment", 12, text="old", reason="r")
        b = db.add_proposal(conn, "assign", 12, login="amy", text="hi", reason="r")
    ledger = Ledger(Policy(), None, fake_github)
    assert "Declined" in ledger.handle("Jahaan", f"decline {b} not now")
    assert "Updated" in ledger.handle("Jahaan", f"edit {a}: new words")
    ledger.handle("Jahaan", f"approve {a}")
    assert "new words" in [c for c in fake_github.calls if c[0] == "comment"][0][2]
    assert not any(c[0] == "assign" for c in fake_github.calls)


def test_only_maintainers(fake_github):
    with db.session() as conn:
        seed_issue(conn)
        pid = db.add_proposal(conn, "assign", 12, login="ben", reason="r")
    ledger = Ledger(Policy({**Policy().raw, "maintainers": ["Jahaan"]}), None, fake_github)
    assert "only maintainers" in ledger.handle("Mallory", f"approve {pid}")
    assert fake_github.calls == []
    assert ledger.handle("jahaan", f"approve {pid}").startswith("Done")


def test_demo_records_are_simulated(fake_github):
    with db.session() as conn:
        seed_issue(conn, number=4)
        db.upsert_contributor(conn, "kofi-codes", source="demo")
        pid = db.add_proposal(conn, "assign", 4, login="kofi-codes", text="yours", reason="r")
    ledger = Ledger(Policy(), None, fake_github)
    reply = ledger.handle("Jahaan", f"approve {pid}")
    assert "simulated" in reply
    assert fake_github.calls == []


def test_card_lists_how_to_answer():
    with db.session() as conn:
        seed_issue(conn)
        pid = db.add_proposal(conn, "assign", 12, login="ben", text="Hi", reason="asked first", proposed_by="steward")
        p = db.get_proposal(conn, pid)
    card = Ledger(Policy(), None, None).card(p)
    assert f"[P{pid}] Assign #12 to @ben" in card and f"approve {pid}" in card


def test_check_token_rejects_what_is_not_a_token():
    from steward.ledger import check_token

    assert check_token("", "o/r") == (False, "no GitHub token stored")
    ok, message = check_token("hunter2", "o/r")
    assert not ok and "github_pat_" in message
