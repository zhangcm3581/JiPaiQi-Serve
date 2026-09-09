"""Exercise the real updater with local Git/SQLite and isolated OS command doubles.

No system service, live repository, network dependency install, or production DB is used.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update.sh"


def run(*args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, capture_output=True, **kwargs
    ).stdout.strip()


def executable(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def deployment(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git_env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Update test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Update test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )

    def git(repo, *args):
        return run("git", "-C", str(repo), *args, env=git_env)

    git(origin, "init", "-b", "main")
    (origin / "app").mkdir()
    (origin / "scripts").mkdir()
    (origin / "app/main.py").write_text("VERSION = 1\n")
    (origin / "requirements.txt").write_text("# Test dependencies\n")
    (origin / ".gitignore").write_text(".venv/\n__pycache__/\n")
    shutil.copy(SCRIPT, origin / "scripts/update.sh")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "Initial deployment")
    before = git(origin, "rev-parse", "HEAD")
    app = tmp_path / "app"
    run("git", "clone", str(origin), str(app), env=git_env)
    (origin / "app/main.py").write_text("VERSION = 2\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "New server version")
    target = git(origin, "rev-parse", "HEAD")

    db_path = tmp_path / "var/jpq.sqlite3"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE hands(tenant_id TEXT, round_version INTEGER)")
        db.execute("INSERT INTO hands VALUES('001001', 7)")

    commands = tmp_path / "commands"
    state = tmp_path / "service-state"
    state.write_text("active")
    log = tmp_path / "calls"
    # Existing venv delegates Python work, including the actual SQLite backup.
    wrapper = f'#!/bin/sh\nexec {json.dumps(sys.executable)} "$@"\n'
    executable(app / ".venv/bin/python", wrapper)
    (app / ".venv/generation").write_text("old")

    # Runtime doubles allow testing root/systemd/pip paths safely on macOS and CI.
    executable(commands / "id", "#!/bin/sh\nprintf '0\\n'\n")
    executable(commands / "flock", '#!/bin/sh\n[ "${LOCK_FAIL:-0}" != 1 ]\n')
    executable(
        commands / "systemctl",
        f"""#!{sys.executable}
import os, sys
from pathlib import Path
state = Path(os.environ['TEST_SERVICE_STATE'])
with open(os.environ['TEST_CALL_LOG'], 'a') as log: log.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'is-active': sys.exit(0 if state.read_text() == 'active' else 3)
if sys.argv[1] == 'stop': state.write_text('inactive')
if sys.argv[1] == 'start': state.write_text('active')
""",
    )
    executable(
        commands / "curl",
        f"""#!{sys.executable}
import os
from pathlib import Path
code = (Path(os.environ['JPQ_APP_DIR'])/'app/main.py').read_text()
if os.environ.get('FAIL_NEW_HEALTH') == '1' and 'VERSION = 2' in code:
    print('{{"status":"broken"}}')
else: print('{{"status":"ok"}}')
""",
    )
    new_python = (
        f"#!{sys.executable}\nimport os,sys\n"
        "if sys.argv[1:3] == ['-m', 'pip']: sys.exit(1 if os.environ.get('FAIL_PIP') == '1' else 0)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])\n"
    )
    executable(
        commands / "python3",
        f"""#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1:3] == ['-m', 'venv']:
    folder = Path(sys.argv[3]); (folder/'bin').mkdir(parents=True)
    script = {new_python!r}
    (folder/'bin/python').write_text(script); (folder/'bin/python').chmod(0o755)
    (folder/'generation').write_text('new')
else: os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
""",
    )
    env = dict(
        git_env,
        PATH=str(commands) + os.pathsep + os.environ["PATH"],
        JPQ_APP_DIR=str(app),
        JPQ_DB_PATH=str(db_path),
        JPQ_BACKUP_DIR=str(tmp_path / "backups"),
        JPQ_LOCK_FILE=str(tmp_path / "update.lock"),
        JPQ_HEALTH_ATTEMPTS="1",
        TEST_SERVICE_STATE=str(state),
        TEST_CALL_LOG=str(log),
    )

    def update(**overrides):
        return subprocess.run(
            ["bash", str(app / "scripts/update.sh")],
            env=dict(env, **overrides),
            text=True,
            capture_output=True,
            timeout=30,
        )

    return {
        "app": app,
        "origin": origin,
        "git": git,
        "update": update,
        "before": before,
        "target": target,
        "db": db_path,
        "backups": tmp_path / "backups",
        "log": log,
        "state": state,
    }


def assert_data_unchanged(path):
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM hands").fetchall() == [("001001", 7)]


def test_success_backs_up_data_updates_code_and_restarts(deployment):
    d = deployment
    result = d["update"]()
    assert result.returncode == 0, result.stdout + result.stderr
    assert d["git"](d["app"], "rev-parse", "HEAD") == d["target"]
    assert (d["app"] / ".venv/generation").read_text() == "new"
    assert d["state"].read_text() == "active"
    backup = next(d["backups"].iterdir())
    assert (backup / "venv-before/generation").read_text() == "old"
    assert_data_unchanged(backup / "database.sqlite3")
    assert_data_unchanged(d["db"])
    assert backup.stat().st_mode & 0o077 == 0
    assert (
        d["app"] / "app/main.py"
    ).stat().st_mode & 0o004  # service user can read code
    assert (d["app"] / ".venv").stat().st_mode & 0o005 == 0o005


@pytest.mark.parametrize("failure", ["FAIL_PIP", "FAIL_NEW_HEALTH"])
def test_failed_update_restores_code_dependencies_and_service(deployment, failure):
    d = deployment
    result = d["update"](**{failure: "1"})
    assert result.returncode != 0
    assert "旧版本已恢复" in result.stdout, result.stdout + result.stderr
    assert d["git"](d["app"], "rev-parse", "HEAD") == d["before"]
    assert d["git"](d["app"], "symbolic-ref", "--short", "HEAD") == "main"
    assert (d["app"] / ".venv/generation").read_text() == "old"
    assert d["state"].read_text() == "active"
    assert_data_unchanged(d["db"])


def test_no_changes_does_not_restart_service(deployment):
    d = deployment
    assert d["update"]().returncode == 0
    d["log"].write_text("")
    result = d["update"]()
    assert result.returncode == 0 and "已是最新版本" in result.stdout
    assert d["log"].read_text() == ""
    assert len(list(d["backups"].iterdir())) == 1


@pytest.mark.parametrize("untracked", [False, True])
def test_local_changes_are_not_overwritten(deployment, untracked):
    d = deployment
    path = d["app"] / ("local-notes.txt" if untracked else "app/main.py")
    path.write_text("local change")
    result = d["update"]()
    assert result.returncode != 0 and "本地修改" in result.stderr
    assert path.read_text() == "local change"
    assert not d["log"].exists()
    assert d["git"](d["app"], "rev-parse", "HEAD") == d["before"]


def test_diverged_branch_is_rejected_before_service_stop(deployment):
    d = deployment
    (d["app"] / "extra.txt").write_text("local commit")
    d["git"](d["app"], "add", ".")
    d["git"](d["app"], "commit", "-m", "Server local commit")
    before = d["git"](d["app"], "rev-parse", "HEAD")
    result = d["update"]()
    assert result.returncode != 0 and "分叉" in result.stderr
    assert d["git"](d["app"], "rev-parse", "HEAD") == before
    assert not d["log"].exists()


def test_backup_failure_restarts_original_service_without_changing_code(deployment):
    d = deployment
    d["db"].write_text("invalid database")
    result = d["update"]()
    assert result.returncode != 0
    assert d["git"](d["app"], "rev-parse", "HEAD") == d["before"]
    assert (d["app"] / ".venv/generation").read_text() == "old"
    assert d["state"].read_text() == "active"
    assert d["db"].read_text() == "invalid database"


def test_update_lock_prevents_a_second_update(deployment):
    d = deployment
    result = d["update"](LOCK_FAIL="1")
    assert result.returncode != 0 and "另一个更新任务" in result.stderr
    assert not d["log"].exists()
