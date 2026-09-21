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
from remote_preflight import _database_schema_is_known  # noqa: E402


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
        self.assertEqual(result["python_files"], 142)

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

    def test_oversized_source_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "src" / "tgvio" / "domain"
            package.mkdir(parents=True)
            (package / "huge.py").write_text(
                "value = 1\n" * 1700,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GuardError, "source budget"):
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

    def test_expected_runtime_lease_is_safe_but_conflict_or_phase_claim_blocks_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            self._database(database)
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE runtime_leases (
                    lease_name TEXT PRIMARY KEY,
                    holder_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    acquired_at INTEGER NOT NULL,
                    heartbeat_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE job_phase_claims (
                    job_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    holder_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    heartbeat_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(job_id, phase)
                );
                """
            )
            future = 4_000_000_000
            connection.execute(
                "INSERT INTO runtime_leases VALUES('telegram-runtime','runtime-a',1,1,1,?)",
                (future,),
            )
            connection.commit()
            connection.close()

            report = database_report(database)
            self.assertTrue(report["safe_to_deploy"])
            self.assertEqual(report["runtime_leases_active"], 1)
            self.assertEqual(report["blocking"]["claims_or_leases"], 0)
            self.assertEqual(report["blocking"]["runtime_lease_conflicts"], 0)

            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO runtime_leases VALUES('unexpected-runtime','runtime-b',1,1,1,?)",
                (future,),
            )
            connection.commit()
            connection.close()
            conflicted = database_report(database)
            self.assertFalse(conflicted["safe_to_deploy"])
            self.assertEqual(conflicted["blocking"]["runtime_lease_conflicts"], 1)

            connection = sqlite3.connect(database)
            connection.execute("DELETE FROM runtime_leases WHERE lease_name='unexpected-runtime'")
            connection.execute(
                "INSERT INTO job_phase_claims VALUES('job-1','publish','worker-a',1,1,1,?)",
                (future,),
            )
            connection.commit()
            connection.close()
            blocked = database_report(database)
            self.assertFalse(blocked["safe_to_deploy"])
            self.assertEqual(blocked["blocking"]["claims_or_leases"], 1)

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


class RemotePreflightSchemaTests(unittest.TestCase):
    def test_known_v0_schema_requires_no_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 0,
                    "migration_ledger_present": False,
                    "schema_sql_sha256": "d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d",
                }
            )
        )

    def test_known_v1_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 1,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6",
                }
            )
        )

    def test_known_v2_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 2,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443",
                }
            )
        )

    def test_known_v3_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 3,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b",
                }
            )
        )

    def test_known_v4_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 4,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017",
                }
            )
        )

    def test_known_v5_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 5,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "c70f05023d89fb89127070a4cffb7f6232609b578eb9e47e4d20fc79afb879c2",
                }
            )
        )

    def test_known_v6_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 6,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244",
                }
            )
        )

    def test_known_v7_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 7,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90",
                }
            )
        )

    def test_known_v18_schema_requires_ledger(self) -> None:
        self.assertTrue(
            _database_schema_is_known(
                {
                    "user_version": 18,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "89a43aa364df5d1f2264cd424be0811c8b8f503fc6a7dc0cb2f5aea0a3c67072",
                }
            )
        )

    def test_unknown_hash_or_ledger_mismatch_fails_closed(self) -> None:
        self.assertFalse(
            _database_schema_is_known(
                {
                    "user_version": 1,
                    "migration_ledger_present": True,
                    "schema_sql_sha256": "0" * 64,
                }
            )
        )
        self.assertFalse(
            _database_schema_is_known(
                {
                    "user_version": 1,
                    "migration_ledger_present": False,
                    "schema_sql_sha256": "593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6",
                }
            )
        )


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
        self.assertIn("RELEASE_ID=${RELEASE_ID}", dockerfile)
        self.assertIn("SOURCE_MANIFEST=${SOURCE_MANIFEST}", dockerfile)
        runtime = dockerfile.split("FROM runtime-base AS runtime", 1)[1]
        self.assertNotIn("COPY tests", runtime)
        self.assertNotIn("build-essential", runtime)
        self.assertIn("**/__pycache__", dockerignore)
        self.assertIn("**/*.pyc", dockerignore)

    def test_foundation_gate_does_not_inherit_a_deployment_proxy(self) -> None:
        foundation = (ROOT / "scripts" / "check_foundation.sh").read_text(encoding="utf-8")
        self.assertIn("export TGVIO_STATIC_PROXY_URL=", foundation)

    def test_compose_requires_release_identity_and_shared_volume_paths(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("TGVIO_IMAGE:?", compose)
        self.assertIn("APP_COMMIT:?", compose)
        self.assertIn("TGVIO_ENV_FILE:?", compose)
        self.assertIn("TGVIO_HOST_DATA_DIR:?", compose)
        self.assertIn("stop_grace_period: 45s", compose)

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

    def test_schema_release_rehearses_production_backup_before_cutover(self) -> None:
        script = (ROOT / "scripts" / "remote_release.sh").read_text(encoding="utf-8")
        rehearsal = script.index("stage=migration-rehearsal")
        cutover = script.index("stage=preflight-before-cutover")
        self.assertLess(rehearsal, cutover)
        self.assertIn('"$rollback_dir/state-pre.sqlite3" "$rehearsal_dir/state-copy.sqlite3"', script)
        self.assertIn(
            'PYTHONPATH="$source_dir/src" python3 "$source_dir/scripts/rehearse_migration.py"',
            script,
        )
        self.assertIn('migration-rehearsal.stderr.log', script)
        self.assertIn('migration.applied_now', script)
        self.assertIn('before.schema_sql_sha256', script)
        self.assertIn('backup.schema_sql_sha256', script)
        self.assertIn('find "$rehearsal_dir" -depth -delete', script)

    def test_log_query_wrapper_uses_host_source_and_shared_log(self) -> None:
        wrapper = (ROOT / "scripts" / "logs.sh").read_text(encoding="utf-8")
        self.assertIn('python3 "$repo_root/scripts/logs.py"', wrapper)
        self.assertIn("/root/TGVIO/logs/tgvio.jsonl", wrapper)
        self.assertNotIn("/app/scripts/logs.py", wrapper)

    def test_known_hosts_contains_only_the_pinned_alias(self) -> None:
        lines = [
            line
            for line in (ROOT / "deploy" / "hostdzire_known_hosts").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("HostDZire ssh-ed25519 "))


    def test_release_id_guards_accept_optional_work_package_suffix(self) -> None:
        deploy = (ROOT / "scripts" / "deploy_hostdzire.py").read_text(encoding="utf-8")
        remote = (ROOT / "scripts" / "remote_release.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "scripts" / "rollback_hostdzire.sh").read_text(encoding="utf-8")

        self.assertIn(r'r2-[0-9]{2}(?:[a-z][0-9]*)?-[0-9a-f]{7}', deploy)
        self.assertIn("R2-07A3", deploy)
        shell_pattern = r'r2-[0-9]{2}([a-z][0-9]*)?-[0-9a-f]{7}'
        self.assertIn(shell_pattern, remote)
        self.assertIn(shell_pattern, rollback)


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

    def test_declared_migration_records_schema_change(self) -> None:
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
                '{"database":{"user_version":1,"schema_sql_sha256":"schema-b"}}',
                encoding="utf-8",
            )
            inspection.write_text('{"status":"passed"}', encoding="utf-8")
            args = SimpleNamespace(
                preflight=preflight,
                postflight=postflight,
                image_inspection=inspection,
                backup=None,
                previous=None,
                migration="0001_baseline",
                status="deployed",
                release_id="r2-03-abcdef0-20260912T000000Z",
                git_commit="a" * 40,
                git_archive_sha256="b" * 64,
                source_manifest="c" * 64,
                requirements_lock_sha256="d" * 64,
                dockerfile_sha256="e" * 64,
                base_image="base@sha256:" + "f" * 64,
                test_image_id="sha256:" + "1" * 64,
                runtime_image_id="sha256:" + "2" * 64,
                tests=182,
                test_seconds=1.0,
            )
            manifest, ledger = build_manifest(args)
            database = manifest["database"]
            assert isinstance(database, dict)
            self.assertEqual(database["migration"], "0001_baseline")
            self.assertEqual(database["user_version_before"], 0)
            self.assertEqual(database["user_version_after"], 1)
            self.assertEqual(database["schema_sql_sha256_before"], "schema-a")
            self.assertEqual(database["schema_sql_sha256_after"], "schema-b")
            self.assertEqual(ledger["status"], "deployed")


if __name__ == "__main__":
    unittest.main()
