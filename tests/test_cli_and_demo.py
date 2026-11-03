from steward import cli, db, demo


def test_propose_queue_and_digest(capsys):
    with db.session() as conn:
        for n in range(1, 21):
            db.upsert_issue(conn, {"number": n, "title": f"issue {n}", "labels": ["good first issue"], "source": "github"})
        demo.play(conn, speed=0, quiet=True)
    assert cli.main(["claims", "--contested"]) == 0
    out = capsys.readouterr().out
    assert "#4" in out and "kofi-codes" in out and "asha-dev" in out
    assert cli.main(["claims", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "#9" in out and "maria-s" in out
    assert cli.main(["prs"]) == 0
    out = capsys.readouterr().out
    assert "#101" in out and "likely spam" in out
    assert cli.main(["propose", "release", "9", "--login", "maria-s", "--text", "Releasing so others can help.", "--reason", "quiet 96h"]) == 0
    assert capsys.readouterr().out.startswith("P1")
    assert cli.main(["digest"]) == 0
    out = capsys.readouterr().out
    assert "Contested issues (1)" in out and "[P1]" in out and "dev-rush-2026: 4 open claims: #1, #3, #5, #19" in out
    assert "@asha-dev: 2 open claims: #4 (contested), #16" in out


def test_propose_needs_reason_and_known_target(capsys):
    assert cli.main(["propose", "comment", "99", "--text", "hi", "--reason", "r"]) == 1
    with db.session() as conn:
        db.upsert_issue(conn, {"number": 3, "title": "t", "source": "github"})
    assert cli.main(["propose", "assign", "3", "--reason", "r"]) == 2


def test_demo_reset_keeps_real_data():
    with db.session() as conn:
        db.upsert_issue(conn, {"number": 4, "title": "real", "source": "github"})
        demo.play(conn, speed=0, quiet=True)
        assert demo.reset(conn) > 0
        assert db.get_issue(conn, 4)["title"] == "real"
        assert db.open_prs(conn) == []


def test_only_the_reviewer_drafts_reviews(capsys):
    with db.session() as conn:
        db.upsert_issue(conn, {"number": 4, "title": "t", "source": "github"})
        demo.play(conn, speed=0, quiet=True)
    assert cli.main(["propose", "review", "101", "--by", "steward", "--text", "likely spam", "--reason", "flagged"]) == 2
    assert "@reviewer please look at PR" in capsys.readouterr().err
    assert cli.main(["propose", "close", "101", "--by", "reviewer", "--text", "Thanks, but...", "--reason", "spam signals"]) == 0
