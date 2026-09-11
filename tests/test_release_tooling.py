from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_guard import (  # noqa: E402
    GuardError,
    check_architecture,
    database_report,
    source_manifest,
    sqlite_backup,
    sqlite_restore,
    verify_archive,
    verify_repository,
    verify_tree,
)
from write_release_manifest import build_manifest  # noqa: E402


class SourceGuardTests(unittest.TestCase):
    def test_source_manifest_matches_the_documented_shell_algorithm(self) -> None:
        expected = subprocess.run(
            [
                "sh",
                "-c",
                "find src -type f -name '*.py' -print0 | LC_ALL=C sort -z | "
                "xargs -0 sha256sum | sha256sum | cut -d' ' -f1",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(source_manifest(ROOT), expected)

    def test_manifest_fixture_is_filename_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "b.py").write_bytes(b"b = 2\n")
            (root / "src" / "a.py").write_bytes(b"a = 1\n")
            combined = hashlib.sha256()
            for name, content in (("src/a.py", b"a = 1\n"), ("src/b.py", b"b = 2\n")):
                combined.update(hashlib.sha256(content).hexdigest().encode("ascii"))
                combined.update(b"  " + name.encode("utf-8") + b"\n")
            self.assertEqual(source_manifest(root), combined.hexdigest())

    def test_tree_rejects_environment_and_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "ok.py").write_text("pass\n", encoding="utf-8")
            (root / ".env").write_text("EXAMPLE=value\n", encoding="utf-8")
            with self.assertRaisesRegex(GuardError, "environment-secret-file"):
                verify_tree(root)
            (root / ".env").unlink()
            (root / "session").mkdir()
            with self.assertRaisesRegex(GuardError, "runtime-volume"):
                verify_tree(root)

    def test_tree_rejects_high_confidence_token_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            token = "gh" + "p_" + "A" * 30
            (root / "src" / "bad.txt").write_text(token, encoding="utf-8")
            with self.assertRaisesRegex(GuardError, "github-token"):
                verify_tree(root)

    def test_archive_rejects_symbolic_links_and_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "bad.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                env = tarfile.TarInfo(".env")
                payload = b"placeholder\n"
                env.size = len(payload)
                archive.addfile(env, io.BytesIO(payload))
                link = tarfile.TarInfo("src/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                archive.addfile(link)
            with self.assertRaises(GuardError):
                verify_archive(archive_path)

    def test_current_repository_passes_source_guard(self) -> None:
        result = verify_tree(ROOT)
        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["files"], 80)


class ArchitectureGateTests(unittest.TestCase):
    def test_current_architecture_passes(self) -> None:
        result = check_architecture(ROOT)
        self.assertEqual(result["python_files"], 44)

    def test_domain_cannot_import_an_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = root / "src" / "tgvio" / "domain"
            domain.mkdir(parents=True)
            (domain / "bad.py").write_text(
                "from tgvio.adapters import telegram\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GuardError, "domain imports"):
                check_architecture(root)


class DatabaseReleaseGateTests(unittest.TestCase):
    @staticmethod
    def _database(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                error_code TEXT
            );
            CREATE TABLE job_progress (job_id TEXT PRIMARY KEY, phase TEXT NOT NULL);
            CREATE TABLE publish_effects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                effect_type TEXT NOT NULL
            );
            """
        )
        connection.commit()
        connection.close()

    def test_active_job_blocks_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            self._database(database)
            self.assertTrue(database_report(database)["safe_to_deploy"])
            connection = sqlite3.connect(database)
            connection.execute("INSERT INTO jobs(id,state) VALUES('one','received')")
            connection.execute("INSERT INTO job_progress(job_id,phase) VALUES('one','queued')")
            connection.commit()
            connection.close()
            report = database_report(database)
            self.assertFalse(report["safe_to_deploy"])
            self.assertEqual(report["blocking"]["jobs"], 1)
            self.assertEqual(report["blocking"]["progress"], 1)

    def test_uncertain_or_uncommitted_publish_blocks_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            self._database(database)
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO jobs(id,state,error_code) VALUES('one','failed','publish_uncertain')"
            )
            connection.execute(
                """
                INSERT INTO publish_effects(plan_id,step_index,effect_type)
                VALUES('plan-one',0,'message')
                """
            )
            connection.commit()
            connection.close()
            report = database_report(database)
            self.assertFalse(report["safe_to_deploy"])
            self.assertEqual(report["blocking"]["publish_uncertain_or_partial"], 1)
            self.assertEqual(report["blocking"]["publish_uncommitted_effects"], 1)

    def test_backup_and_explicit_restore_use_sqlite_backup_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "state.sqlite3"
            backup = root / "backup.sqlite3"
            self._database(live)
            first = sqlite_backup(live, backup)
            self.assertEqual(first["quick_check"], "ok")
            connection = sqlite3.connect(live)
            connection.execute("INSERT INTO jobs(id,state) VALUES('later','succeeded')")
            connection.commit()
            connection.close()
            restored = sqlite_restore(backup, live)
            self.assertEqual(restored["quick_check"], "ok")
            self.assertEqual(database_report(live)["jobs_total"], 0)


class BuildContractTests(unittest.TestCase):
    def test_dependency_lock_matches_direct_requirements(self) -> None:
        direct = {
            line.split("==", 1)[0].lower(): line.split("==", 1)[1]
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        locked = {
            line.split("==", 1)[0].lower(): line.split("==", 1)[1].split()[0].rstrip("\\")
            for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.startswith("#")
        }
        self.assertEqual(
            direct,
            {name: locked[name] for name in ("aiosqlite", "telethon", "cryptg", "yt-dlp")},
        )
        self.assertEqual(
            set(locked),
            {"aiosqlite", "cryptg", "pyaes", "pyasn1", "rsa", "telethon", "yt-dlp"},
        )
        for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                self.assertIn("\\", line)

    def test_dockerfile_has_isolated_test_and_runtime_targets(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("AS test", dockerfile)
        self.assertIn("AS runtime", dockerfile)
        self.assertIn("--require-hashes", dockerfile)
        self.assertIn("python:3.11-slim@sha256:", dockerfile)
        runtime = dockerfile.split("FROM runtime-base AS runtime", 1)[1]
        self.assertNotIn("COPY tests", runtime)
        self.assertNotIn("build-essential", runtime)
        self.assertIn("**/__pycache__", dockerignore)
        self.assertIn("**/*.pyc", dockerignore)

    def test_compose_requires_release_identity_and_shared_volume_paths(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("TGVIO_IMAGE:?", compose)
        self.assertIn("APP_COMMIT:?", compose)
        self.assertIn("TGVIO_ENV_FILE:?", compose)
        self.assertIn("TGVIO_HOST_DATA_DIR:?", compose)

    def test_deployment_scripts_reject_unsafe_ssh_shortcuts(self) -> None:
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "scripts").iterdir()
            if path.suffix in {".py", ".sh"}
        )
        self.assertNotIn("StrictHostKeyChecking=no", scripts)
        self.assertNotIn("sshpass", scripts)
        self.assertNotIn("rm -rf", scripts)
        self.assertNotIn("eval ", scripts)
        self.assertIn("--network none", scripts)
        self.assertIn("database backup hash mismatch", scripts)
        self.assertIn("rollback image tag mismatch", scripts)
        self.assertIn("GIT_ASKPASS", scripts)
        self.assertIn("getpass.getpass", scripts)

    def test_known_hosts_contains_only_the_pinned_alias(self) -> None:
        lines = [
            line
            for line in (ROOT / "deploy" / "hostdzire_known_hosts").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("HostDZire ssh-ed25519 "))


class GitGateTests(unittest.TestCase):
    def test_dirty_repository_is_rejected(self) -> None:
        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[1:3] == ["rev-parse", "HEAD"]:
                output = "a" * 40 + "\n"
            elif args[1:3] == ["branch", "--show-current"]:
                output = "main\n"
            elif args[1:3] == ["status", "--porcelain"]:
                output = "?? untracked.txt\n"
            else:
                self.fail(f"unexpected git command: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch("release_guard._run", side_effect=fake_run):
            with self.assertRaisesRegex(GuardError, "dirty"):
                verify_repository(
                    ROOT,
                    require_clean=True,
                    require_pushed=False,
                    verify_remote=False,
                )

    def test_unpushed_commit_is_rejected(self) -> None:
        head = "a" * 40
        upstream = "b" * 40

        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            command = args[1:]
            if command == ["rev-parse", "HEAD"]:
                output = head + "\n"
            elif command == ["branch", "--show-current"]:
                output = "main\n"
            elif command == ["status", "--porcelain", "--untracked-files=all"]:
                output = ""
            elif command == ["rev-parse", "--abbrev-ref", "@{upstream}"]:
                output = "origin/main\n"
            elif command == ["rev-parse", "@{upstream}"]:
                output = upstream + "\n"
            else:
                self.fail(f"unexpected git command: {args}")
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch("release_guard._run", side_effect=fake_run):
            with self.assertRaisesRegex(GuardError, "not the pushed"):
                verify_repository(
                    ROOT,
                    require_clean=True,
                    require_pushed=True,
                    verify_remote=False,
                )


class ReleaseManifestTests(unittest.TestCase):
    def test_migration_free_manifest_requires_an_unchanged_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preflight = root / "preflight.json"
            postflight = root / "postflight.json"
            inspection = root / "inspection.json"
            preflight.write_text(
                '{"database":{"user_version":0,"schema_sql_sha256":"schema-a"}}',
                encoding="utf-8",
            )
            postflight.write_text(
                '{"database":{"user_version":0,"schema_sql_sha256":"schema-a"}}',
                encoding="utf-8",
            )
            inspection.write_text('{"status":"passed"}', encoding="utf-8")
            args = SimpleNamespace(
                preflight=preflight,
                postflight=postflight,
                image_inspection=inspection,
                backup=None,
                previous=None,
                migration="none",
                status="deployed",
                release_id="r2-02-abcdef0-20260911T000000Z",
                git_commit="a" * 40,
                git_archive_sha256="b" * 64,
                source_manifest="c" * 64,
                requirements_lock_sha256="d" * 64,
                dockerfile_sha256="e" * 64,
                base_image="base@sha256:" + "f" * 64,
                test_image_id="sha256:" + "1" * 64,
                runtime_image_id="sha256:" + "2" * 64,
                tests=156,
                test_seconds=1.0,
            )
            manifest, ledger = build_manifest(args)
            database = manifest["database"]
            assert isinstance(database, dict)
            self.assertEqual(database["user_version_before"], 0)
            self.assertEqual(ledger["status"], "deployed")

            postflight.write_text(
                '{"database":{"user_version":0,"schema_sql_sha256":"schema-b"}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "schema changed"):
                build_manifest(args)


if __name__ == "__main__":
    unittest.main()
