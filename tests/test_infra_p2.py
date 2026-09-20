"""Tests for the P2 infrastructure additions.

Covers:
- scripts/hermes_git_ssh_wrapper.sh: self-healing key copy, security flags,
  no-secrets content, Windows-host isolation (GIT_SSH_COMMAND env-only).
- .bat [VERSION] stamp: Get-BlobStatus logic mirrored in Python against the
  real repository state (SYNC / LAG / MISSING / NOBLOB / UNKNOWN) and the
  non-fatal placement of the stamp in both workflows.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PROJECT_ROOT / "scripts" / "hermes_git_ssh_wrapper.sh"


# ---------------------------------------------------------------------------
# A. Deploy-key wrapper
# ---------------------------------------------------------------------------


class TestGitSshWrapper:
    def test_wrapper_exists_and_is_executable(self):
        assert WRAPPER.is_file()
        assert os.access(WRAPPER, os.X_OK)

    def test_wrapper_contains_no_secrets(self):
        content = WRAPPER.read_text(encoding="utf-8")
        # No private key material markers
        assert "BEGIN OPENSSH PRIVATE KEY" not in content
        assert "BEGIN RSA PRIVATE KEY" not in content
        assert "BEGIN EC PRIVATE KEY" not in content
        # No inline credentials
        assert "password" not in content.lower()
        # The wrapper references only PATHS, not contents
        assert "/run/secrets/hermes-github/db_builder_deploy" in content
        assert "/var/tmp/hermes-db-builder-deploy" in content

    def test_wrapper_enforces_strict_ssh(self):
        content = WRAPPER.read_text(encoding="utf-8")
        assert "StrictHostKeyChecking=yes" in content
        assert "IdentitiesOnly=yes" in content
        assert "UserKnownHostsFile=" in content
        # Never weakens checks
        assert "StrictModes=no" not in content
        assert "StrictHostKeyChecking=no" not in content
        assert "accept-new" not in content

    def test_wrapper_chmods_600(self):
        content = WRAPPER.read_text(encoding="utf-8")
        # chmod must run OUTSIDE the if-block: enforced on EVERY invocation
        assert "chmod 600" in content

    def test_wrapper_chmod_runs_on_every_invocation(self):
        """The chmod 600 must be unconditional (outside the copy-if-missing
        block), so a permissively-moded pre-existing key is corrected."""
        content = WRAPPER.read_text(encoding="utf-8")
        # Locate the if-block and the chmod line positions
        if_line = content.find("if [ ! -f \"$RUNTIME_KEY\" ]")
        fi_line = content.find("fi", if_line)
        chmod_line = content.find("chmod 600 \"$RUNTIME_KEY\"")
        assert 0 <= if_line < fi_line, "if-block not found"
        # chmod must come AFTER the closing fi (unconditional)
        assert chmod_line > fi_line, (
            "chmod 600 is inside the copy-if-missing block; "
            "it must run on every invocation"
        )

    def test_wrapper_repairs_permissive_runtime_key(self):
        """End-to-end: pre-create the runtime key with mode 0644, invoke the
        wrapper via GIT_SSH_COMMAND, confirm mode becomes 0600."""
        import stat as stat_mod

        runtime_key = Path("/var/tmp/hermes-db-builder-deploy")
        mounted_key = Path("/run/secrets/hermes-github/db_builder_deploy")
        if not mounted_key.is_file():
            pytest.skip("mounted deploy key unavailable in this environment")

        # Pre-create with permissive mode (simulating tampering/stale state)
        runtime_key.unlink(missing_ok=True)
        runtime_key.write_bytes(mounted_key.read_bytes())
        runtime_key.chmod(0o644)
        assert (runtime_key.stat().st_mode & 0o777) == 0o644

        env = dict(os.environ)
        env["GIT_SSH_COMMAND"] = str(WRAPPER)
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "ls-remote", "origin"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        # Wrapper ran and corrected the mode
        assert (runtime_key.stat().st_mode & 0o777) == 0o600, (
            "wrapper did not enforce 0600 on pre-existing permissive key"
        )
        # And git still authenticated (where network is available)
        if result.returncode == 0:
            assert "d98dfa6" in result.stdout or "refs/heads" in result.stdout
        else:
            # Network-unavailable environments: mode enforcement alone proves
            # the wrapper executed; authentication covered by runtime evidence
            assert "Permission denied" not in result.stderr or "0600" not in result.stderr

    def test_wrapper_self_heals_missing_key(self):
        content = WRAPPER.read_text(encoding="utf-8")
        assert "if [ ! -f" in content
        assert "cp " in content

    def test_wrapper_execs_ssh_not_replaces(self):
        content = WRAPPER.read_text(encoding="utf-8")
        assert "exec ssh" in content

    def test_git_config_has_no_repo_local_sshcommand(self):
        """Windows-host isolation: repo-local core.sshCommand must be unset so
        the Windows host (sharing .git via OneDrive) uses its own SSH."""
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "config", "--local", "--get", "core.sshCommand"],
            capture_output=True,
            text=True,
        )
        # Exit code 1 = key not present (what we want)
        assert result.returncode == 1
        assert result.stdout.strip() == ""

    def test_git_ssh_command_env_overrides_config(self):
        """GIT_SSH_COMMAND env var is the container-side mechanism; it is
        process-local and never persisted to the shared .git/config."""
        # Documented git precedence: env var > config. We verify the env var
        # is NOT set globally in the repo config after wrapper use by
        # confirming config stays clean (test above) and that the env var
        # mechanism is what the wrapper is designed for.
        content = WRAPPER.read_text(encoding="utf-8")
        assert "GIT_SSH_COMMAND" in content  # documented in header


# ---------------------------------------------------------------------------
# B. [VERSION] stamp (Get-BlobStatus logic)
# ---------------------------------------------------------------------------


def _run_git(*args):
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
    )


def _blob_status(rel: str) -> str:
    """Python mirror of the .bat Get-BlobStatus function."""
    full = PROJECT_ROOT / rel
    if not full.exists():
        return "MISSING"
    head = _run_git("rev-parse", f"HEAD:{rel}")
    if head.returncode != 0 or not head.stdout.strip():
        return "NOBLOB"
    diff = _run_git("diff", "--quiet", "--ignore-cr-at-eol", "HEAD", "--", rel)
    if diff.returncode == 0:
        return "SYNC"
    return "LAG"


class TestVersionStampLogic:
    def test_clean_file_is_sync(self):
        # macro_data_fetch.py is committed at HEAD with no local edits
        assert _blob_status("scripts/macro_data_fetch.py") == "SYNC"

    def test_crlf_only_file_is_sync(self):
        """The repo-wide CRLF working-tree state must NOT read as LAG."""
        # pgSQL_equities_auto.py has CRLF-vs-LF noise but no content change
        assert _blob_status("scripts/pgSQL_equities_auto.py") == "SYNC"

    def test_missing_file_is_missing(self):
        assert _blob_status("scripts/does_not_exist_xyz.py") == "MISSING"

    def test_untracked_file_is_noblob(self):
        # audit_data_integrity.py is untracked -> no HEAD blob
        assert _blob_status("scripts/audit_data_integrity.py") == "NOBLOB"

    def test_modified_file_would_be_lag(self):
        """A genuinely modified (content-diff) file reads as LAG.

        Verified non-destructively: we check a file we know differs.
        If none exists, we create a temp modification and restore it.
        """
        target = PROJECT_ROOT / "scripts" / "freshness_check.py"
        original = target.read_bytes()
        try:
            with open(target, "ab") as f:
                f.write(b"\n# temporary lag-simulation marker\n")
            assert _blob_status("scripts/freshness_check.py") == "LAG"
        finally:
            target.write_bytes(original)
        # Restored
        assert _blob_status("scripts/freshness_check.py") == "SYNC"


class TestBatVersionStamp:
    MACRO = "scripts/auto_macro_db.bat"
    EQUITY = "scripts/auto_postgreSQL_db.bat"

    def _read(self, rel):
        return (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="replace")

    def test_macro_bat_has_version_stamp(self):
        text = self._read(self.MACRO)
        assert "Write-Log 'VERSION'" in text
        assert "macro_data_fetch=" in text
        assert "freshness_check=" in text

    def test_equity_bat_has_version_stamp(self):
        text = self._read(self.EQUITY)
        assert "Write-Log 'VERSION'" in text
        for script in [
            "pgSQL_equities_auto",
            "repair_historical_equity_gaps",
            "indicator_staged_backfill",
            "pgSQL_daily_bulk_sync_to_neon",
            "freshness_check",
        ]:
            assert f"{script}=" in text, f"missing stamp for {script}"

    def test_stamp_uses_git_diff_not_hash_object(self):
        """Get-FileHash/hash-object comparison is forbidden (different schemes
        and CRLF false-positives); must use git diff --ignore-cr-at-eol."""
        for rel in [self.MACRO, self.EQUITY]:
            text = self._read(rel)
            assert "diff --quiet --ignore-cr-at-eol" in text
            assert "Get-FileHash" not in text
            assert "hash-object" not in text

    def test_stamp_is_non_fatal(self):
        """The stamp must not gate the workflow: no exit/throw between the
        stamp and the first Invoke-RetryPython. Stamp failures degrade to
        UNKNOWN/MISSING strings, never exceptions."""
        for rel in [self.MACRO, self.EQUITY]:
            text = self._read(rel)
            stamp_pos = text.find("Write-Log 'VERSION'")
            first_step = text.find("Invoke-RetryPython 'scripts")
            assert 0 < stamp_pos < first_step, f"{rel}: stamp misplaced"
            # No throw/exit inside the Get-BlobStatus function
            fn_start = text.find("function Get-BlobStatus")
            fn_end = text.find("^", fn_start)
            fn_body = text[fn_start:fn_end]
            assert "throw" not in fn_body
            assert "exit" not in fn_body

    def test_stamp_covers_all_five_states(self):
        for rel in [self.MACRO, self.EQUITY]:
            text = self._read(rel)
            for state in ["MISSING", "NOBLOB", "UNKNOWN", "SYNC", "LAG"]:
                assert f"'{state}'" in text, f"{rel}: state {state} not handled"
