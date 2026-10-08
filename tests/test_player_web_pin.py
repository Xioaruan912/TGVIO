"""The Player front end enters checks and images only as the pinned, pushed commit."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "player_web_source.sh"
REPOSITORY = "https://github.com/example/TGVIO-Player.git"


def git(cwd: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=environment,
    )
    return result.stdout.strip()


class LockFileTests(unittest.TestCase):
    def test_repository_lock_pins_a_full_commit_of_the_public_repository(self) -> None:
        lines = [
            line for line in (ROOT / "player-web.lock").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(lines), 2)
        values = dict(line.split("=", 1) for line in lines)
        self.assertEqual(values["repository"], "https://github.com/Xioaruan912/TGVIO-Player.git")
        self.assertRegex(values["commit"], r"^[0-9a-f]{40}$")


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class PinnedExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.remote = self.folder / "remote.git"
        self.clone = self.folder / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True)
        # The clone names GitHub (ssh spelling) while insteadOf routes fetches to a local bare repo.
        git(self.clone, "remote", "add", "origin", "git@github.com:example/TGVIO-Player.git")
        git(self.clone, "config", f"url.{self.remote}.insteadOf", "git@github.com:example/TGVIO-Player.git")
        self.pinned = self.commit("pinned")
        git(self.clone, "push", "-q", "origin", "main")

    def commit(self, marker: str) -> str:
        (self.clone / "src").mkdir(exist_ok=True)
        (self.clone / "src" / "main.ts").write_text(f"export const marker = {marker!r};\n", encoding="utf-8")
        (self.clone / "package.json").write_text("{}\n", encoding="utf-8")
        (self.clone / "package-lock.json").write_text("{}\n", encoding="utf-8")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", marker)
        return git(self.clone, "rev-parse", "HEAD")

    def lock(self, commit: str, repository: str = REPOSITORY) -> Path:
        path = self.folder / "player-web.lock"
        path.write_text(f"# test\nrepository={repository}\ncommit={commit}\n", encoding="utf-8")
        return path

    def export(self, lock: Path, dest: Path | None = None):
        dest = dest or self.folder / "export"
        return subprocess.run(
            ["bash", str(SCRIPT), "--dest", str(dest), "--lock", str(lock), "--git", str(self.clone)],
            capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
        ), dest

    def test_exports_the_pinned_tree_not_the_working_copy(self) -> None:
        self.commit("later")  # local HEAD moves past the pin
        (self.clone / "src" / "main.ts").write_text("uncommitted\n", encoding="utf-8")
        result, dest = self.export(self.lock(self.pinned))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.pinned)
        self.assertEqual((dest / "src" / "main.ts").read_text(encoding="utf-8"), "export const marker = 'pinned';\n")

    def test_refuses_a_commit_that_is_not_on_origin_main(self) -> None:
        unpushed = self.commit("unpushed")
        result, dest = self.export(self.lock(unpushed))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on origin/main", result.stderr)
        self.assertFalse(dest.exists())

    def test_fetches_a_pin_pushed_after_the_clone_last_fetched(self) -> None:
        pushed_later = self.commit("pushed later")
        git(self.clone, "push", "-q", str(self.remote), "main")  # remote-tracking ref stays stale
        result, _ = self.export(self.lock(pushed_later))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), pushed_later)

    def test_refuses_a_clone_of_another_repository(self) -> None:
        git(self.clone, "remote", "set-url", "origin", "https://github.com/someone/else.git")
        result, _ = self.export(self.lock(self.pinned))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match locked repository", result.stderr)

    def test_refuses_malformed_locks(self) -> None:
        for commit, repository, message in (
            (self.pinned[:12], REPOSITORY, "full 40-hex SHA"),
            ("main", REPOSITORY, "full 40-hex SHA"),
            (self.pinned, "git@github.com:example/TGVIO-Player.git", "https://github.com"),
        ):
            with self.subTest(commit=commit, repository=repository):
                result, _ = self.export(self.lock(commit, repository))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        extra = self.lock(self.pinned)
        extra.write_text(extra.read_text(encoding="utf-8") + "branch=main\n", encoding="utf-8")
        result, _ = self.export(extra)
        self.assertIn("unexpected lock line", result.stderr)

    def test_refuses_a_non_empty_destination(self) -> None:
        dest = self.folder / "busy"
        dest.mkdir()
        (dest / "stale.js").write_text("old build input\n", encoding="utf-8")
        result, _ = self.export(self.lock(self.pinned), dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not empty", result.stderr)
        self.assertEqual(sorted(path.name for path in dest.iterdir()), ["stale.js"])


class ExportCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def remove(self, target: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "--remove", str(target)],
            capture_output=True, text=True, env={**os.environ, "TMPDIR": str(self.tmp)},
        )

    def test_removes_only_its_own_export_directory(self) -> None:
        export = self.tmp / "tgvio-player-web.abc123"
        (export / "src").mkdir(parents=True)
        (export / "src" / "main.ts").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.remove(export).returncode, 0)
        self.assertFalse(export.exists())
        self.assertEqual(self.remove(export).returncode, 0)  # already gone is fine

    def test_refuses_other_names_locations_and_symlinks(self) -> None:
        other = self.tmp / "keep-me"
        other.mkdir()
        nested = self.tmp / "nested" / "tgvio-player-web.abc123"
        nested.mkdir(parents=True)
        link = self.tmp / "tgvio-player-web.link"
        link.symlink_to(other, target_is_directory=True)
        for target in (other, nested, link):
            with self.subTest(target=target.name):
                result = self.remove(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing unsafe cleanup", result.stderr)
        self.assertTrue(other.is_dir() and nested.is_dir() and link.is_symlink())


class DockerfileSourceTests(unittest.TestCase):
    def test_player_image_takes_front_end_only_from_the_named_context(self) -> None:
        dockerfile = (ROOT / "Dockerfile.player").read_text(encoding="utf-8")
        build_stage = dockerfile.split("AS player-web-build", 1)[1].split("FROM ", 1)[0]
        copies = re.findall(r"^COPY .*$", build_stage, flags=re.MULTILINE)
        self.assertTrue(copies)
        self.assertTrue(all(line.startswith("COPY --from=player-web ") for line in copies), copies)


if __name__ == "__main__":
    unittest.main()
