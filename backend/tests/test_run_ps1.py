"""The Windows task runner (`run.ps1`) — the Makefile replacement.

These tests drive the real PowerShell, but only ever with `-DryRun` (which prints the commands
and changes nothing) or `help`, so nothing is installed, no server starts and no API is touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUN_PS1 = Path(__file__).resolve().parents[2] / "run.ps1"
REQUIRED_COMMANDS = ("install", "dev", "dev-sim", "test", "check")

# pwsh 7 first, then Windows PowerShell 5.1; skip the module where neither exists (e.g. Linux CI)
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on this platform")


def run_ps1(*args: str, script: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script or RUN_PS1), *args],
        capture_output=True,
        text=True,
        timeout=180,
    )


def dry_run(command: str, script: Path | None = None) -> str:
    r = run_ps1(command, "-DryRun", script=script)
    assert r.returncode == 0, f"{command} failed: {r.stdout}\n{r.stderr}"
    return r.stdout


def steps(output: str) -> list[str]:
    """The `==> [dir] exe args` lines, without the env-var lines."""
    return [line.strip() for line in output.splitlines() if line.strip().startswith("==> [")]


def test_run_ps1_exists_at_the_repo_root() -> None:
    assert RUN_PS1.is_file()


def test_script_has_no_syntax_errors() -> None:
    assert POWERSHELL is not None
    code = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{RUN_PS1}', [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 } else { 'OK' }"
    )
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True, text=True)
    assert r.returncode == 0, f"parse errors:\n{r.stdout}"


def test_help_lists_every_required_command() -> None:
    r = run_ps1("help")
    assert r.returncode == 0, r.stderr
    for cmd in REQUIRED_COMMANDS:
        assert cmd in r.stdout, f"help does not mention {cmd}"
    assert "127.0.0.1:5173" in r.stdout and "127.0.0.1:8000" in r.stdout


def test_no_argument_shows_help_instead_of_doing_something() -> None:
    r = run_ps1()
    assert r.returncode == 0 and "Usage:" in r.stdout


def test_an_unknown_command_is_rejected_with_a_non_zero_exit() -> None:
    r = run_ps1("deploy-to-production", "-DryRun")
    assert r.returncode != 0
    assert "deploy-to-production" in (r.stdout + r.stderr)


def test_install_syncs_backend_then_frontend() -> None:
    s = steps(dry_run("install"))
    assert len(s) == 2
    assert s[0].startswith("==> [backend]") and s[0].endswith("sync")
    assert s[1].startswith("==> [frontend]") and s[1].endswith("install")


def test_dev_starts_backend_and_frontend_together() -> None:
    s = steps(dry_run("dev"))
    assert len(s) == 2
    assert "uvicorn papermind.main:app --host 127.0.0.1 --port 8000 --reload" in s[0]
    assert s[0].startswith("==> [backend]")
    assert s[1].startswith("==> [frontend]") and s[1].endswith("run dev")


def test_dev_sim_sets_the_simulated_feed_and_its_own_database() -> None:
    out = dry_run("dev-sim")
    assert "set PAPERMIND_CRYPTO_PROVIDER=simulated" in out
    assert "set PAPERMIND_DB_URL=sqlite:///papermind-sim.db" in out
    # same two servers as `dev`, so the offline feed is the only difference
    assert steps(out) == steps(dry_run("dev"))


def test_test_runs_backend_coverage_then_frontend() -> None:
    s = steps(dry_run("test"))
    assert len(s) == 2
    assert "pytest --cov=papermind --cov-report=term-missing" in s[0]
    assert s[1].startswith("==> [frontend]") and s[1].endswith("test")


def test_check_runs_lint_and_types_before_the_slow_tests() -> None:
    s = steps(dry_run("check"))
    joined = "\n".join(s)
    assert "ruff check ." in joined
    assert "ruff format --check ." in joined
    assert "mypy papermind" in joined
    assert "run typecheck" in joined
    assert "pytest" in joined
    order = [i for i, line in enumerate(s) if "pytest" in line or "run test" in line or line.endswith("test")]
    lint_at = next(i for i, line in enumerate(s) if "ruff check" in line)
    assert lint_at < min(order), "lint must run before the tests so `check` fails fast"


def test_every_step_checks_the_exit_code_of_what_it_ran() -> None:
    body = RUN_PS1.read_text(encoding="utf-8")
    assert "$LASTEXITCODE -ne 0" in body, "a failing tool must fail the script"


@pytest.mark.parametrize("folder", ["a folder with spaces", "spaces and (parens) [here]"])
def test_it_works_from_a_path_containing_spaces(tmp_path: Path, folder: str) -> None:
    """The project lives under 'MY PROJECT\\Help Trading agent' — paths must survive that."""
    repo = tmp_path / folder
    (repo / "backend").mkdir(parents=True)
    (repo / "frontend").mkdir()
    copied = repo / "run.ps1"
    copied.write_bytes(RUN_PS1.read_bytes())

    out = dry_run("check", script=copied)
    # the working directories are reported relative to the script's own folder, so a
    # mangled $PSScriptRoot would show an absolute path here instead
    assert "==> [backend]" in out and "==> [frontend]" in out
    assert folder not in out  # nothing leaked an unresolved absolute path
    assert steps(out) == steps(dry_run("check"))
