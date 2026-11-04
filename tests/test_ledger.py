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


def test_comment_naming_a_demo_person_never_reaches_github(fake_github):
    from steward import demo

    with db.session() as conn:
        for n in (1, 4, 6):
            seed_issue(conn, number=n)
        demo.play(conn, speed=0, quiet=True)
        # A note on a real issue, with no --login, that names demo people.
        by_text = db.add_proposal(conn, "comment", 4, text="Thanks @asha-dev! #4 went to the first person who asked.", reason="contested")
        by_reason = db.add_proposal(conn, "comment", 1, text="One claim at a time, please.", reason="dev-rush-2026 claimed four issues")
        no_names = db.add_proposal(conn, "comment", 4, text="Thanks for offering!", reason="contested")
        assert db.get_proposal(conn, no_names)["source"] == "demo"
        real = db.add_proposal(conn, "assign", 6, login="TheRealJahaan", text="Yours!", reason="asked in Discord")
        real_note = db.add_proposal(conn, "comment", 6, text="Welcome!", reason="new here")
        assert db.get_proposal(conn, real_note)["source"] == "github"
        assert db.get_proposal(conn, by_text)["source"] == "demo"
        assert db.get_proposal(conn, real)["source"] == "github"
        card = Ledger(Policy(), None, None).card(db.get_proposal(conn, by_text))
        assert "simulated" in card
    ledger = Ledger(Policy(), None, fake_github)
    assert "simulated" in ledger.handle("Jahaan", f"approve {by_text} {by_reason}")
    assert fake_github.calls == []
    assert ledger.handle("Jahaan", f"approve {real}").startswith(f"Done P{real}")
    assert ("assign", 6, "TheRealJahaan") in fake_github.calls


def test_demo_reset_never_turns_a_demo_proposal_real(fake_github):
    from steward import demo

    with db.session() as conn:
        seed_issue(conn, number=4)
        demo.play(conn, speed=0, quiet=True)
        pid = db.add_proposal(conn, "assign", 4, login="kofi-codes", text="Hi @kofi-codes", reason="asked first")
        demo.reset(conn)
        assert db.get_proposal(conn, pid) is None
        # Even if one survived, the stored mark keeps it simulated.
        conn.execute(
            "INSERT INTO proposals(kind, target, login, text, reason, status, created_at, source)"
            " VALUES('comment', 4, '', 'hi', 'r', 'pending', 0, 'demo')"
        )
        left = conn.execute("SELECT id FROM proposals WHERE source = 'demo'").fetchone()[0]
    reply = Ledger(Policy(), None, fake_github).handle("Jahaan", f"approve {left}")
    assert "simulated" in reply and fake_github.calls == []


def test_two_different_notes_are_two_proposals():
    with db.session() as conn:
        seed_issue(conn)
        a = db.add_proposal(conn, "comment", 12, text="note one", reason="r")
        b = db.add_proposal(conn, "comment", 12, text="note two", reason="r")
        again = db.add_proposal(conn, "comment", 12, text="note one", reason="r")
    assert a != b and again == a


def test_old_database_gains_the_source_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target INTEGER NOT NULL,"
        " login TEXT DEFAULT '', text TEXT DEFAULT '', reason TEXT DEFAULT '', proposed_by TEXT DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, decided_at REAL, decided_by TEXT DEFAULT '',"
        " announced INTEGER DEFAULT 0, result TEXT DEFAULT '', undo TEXT DEFAULT '')"
    )
    old.execute("INSERT INTO proposals(kind, target, created_at) VALUES('comment', 1, 0)")
    old.commit()
    old.close()
    with db.session(path) as conn:
        assert db.get_proposal(conn, 1)["source"] == "github"


def test_undo_leaves_no_claim_behind(fake_github):
    """Undoing an assignment that made the claim removes the claim, so the
    next morning's summary does not propose the same assignment again."""
    with db.session() as conn:
        seed_issue(conn, number=13)
        fresh = db.add_proposal(conn, "assign", 13, login="TheRealJahaan", text="yours", reason="asked in Discord")
        seed_issue(conn, number=12)
        db.add_claim(conn, 12, "ben", db.now(), via="comment")
        claimed = db.add_proposal(conn, "assign", 12, login="ben", text="yours", reason="asked first")
    ledger = Ledger(Policy(), None, fake_github)
    ledger.handle("Jahaan", f"approve {fresh} {claimed}")
    ledger.handle("Jahaan", f"undo {fresh}")
    ledger.handle("Jahaan", f"undo {claimed}")
    with db.session() as conn:
        assert db.claims(conn, issue=13) == []
        assert [c["status"] for c in db.claims(conn, issue=12)] == ["open"]
