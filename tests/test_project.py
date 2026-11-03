import subprocess

from steward import cli, project


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_code_command_reads_the_checkout(steward_home, capsys):
    root = project.code_dir()
    root.mkdir()
    _git("init", "-q", cwd=root)
    (root / "tinytask").mkdir()
    (root / "tinytask" / "storage.py").write_text("def next_id(tasks):\n    return len(tasks) + 1\n")
    _git("add", ".", cwd=root)

    assert cli.main(["code"]) == 0
    out = capsys.readouterr().out
    assert str(root) in out and "tinytask/storage.py" in out

    assert cli.main(["code", "tinytask/storage.py"]) == 0
    out = capsys.readouterr().out
    assert "   2     return len(tasks) + 1" in out

    assert cli.main(["code", "--grep", "len(tasks)"]) == 0
    assert "tinytask/storage.py:2:" in capsys.readouterr().out

    # Nothing outside the checkout can be read through it.
    (steward_home / "github-token").write_text("secret")
    assert cli.main(["code", "../github-token"]) == 0
    out = capsys.readouterr().out
    assert "secret" not in out and "no such file" in out


def test_token_never_shows_in_errors():
    assert project._clean("fatal: could not read from https://abc123@github.com", "abc123") == (
        "fatal: could not read from https://***@github.com"
    )
    assert "abc123" not in project._auth("abc123")[1]
    assert project._auth("") == []


def test_checkout_without_repo():
    assert project.checkout("") == "no repository in policy.yaml"


def test_template_is_built_from_the_briefs():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, str(root / "scripts" / "build_template.py"), "--check"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout

    import yaml

    doc = yaml.safe_load((root / "switch" / "steward-crew.template.yaml").read_text())
    names = [a["name"] for a in doc["agents"]]
    assert names == ["stu", "steward", "reviewer"]
    for agent in doc["agents"]:
        assert agent["directory"] == "{$agents_dir}/{agent}"
        assert "Bash(gh:*)" in agent["advanced_config"]["disallowedTools"]
        assert "Write" not in agent["advanced_config"]["tools"]
        assert agent["instructions"].strip()
