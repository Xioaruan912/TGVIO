from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlayerDeploymentArtifactTests(unittest.TestCase):
    def test_player_compose_is_isolated_and_loopback_only(self) -> None:
        compose = (ROOT / "docker-compose.player.yml").read_text(encoding="utf-8")
        self.assertIn("tgvio-player:", compose)
        self.assertIn('"127.0.0.1:${TGVIO_PLAYER_PORT:-8790}:8790"', compose)
        self.assertIn("/var/lib/tgvio-player", compose)
        volume_targets = [
            line.strip()
            for line in compose.splitlines()
            if line.strip().startswith("target:")
        ]
        self.assertEqual(volume_targets, ["target: /var/lib/tgvio-player"])
        environment_keys = [
            line.strip().split(":", 1)[0]
            for line in compose.splitlines()
            if line.startswith("      TGVIO_")
        ]
        self.assertTrue(all(key.startswith("TGVIO_PLAYER_") for key in environment_keys))

    def test_player_dockerfile_copies_no_bot_package_or_runtime_volume(self) -> None:
        dockerfile = (ROOT / "Dockerfile.player").read_text(encoding="utf-8")
        self.assertIn("COPY src/tgvio_player", dockerfile)
        copy_lines = [line.strip() for line in dockerfile.splitlines() if line.startswith("COPY ")]
        self.assertEqual(
            copy_lines,
            [
                "COPY --from=player-web package.json package-lock.json ./",
                "COPY --from=player-web index.html tsconfig.json vite.config.ts ./",
                "COPY --from=player-web public ./public",
                "COPY --from=player-web src ./src",
                "COPY requirements.player.lock ./requirements.player.lock",
                "COPY src/tgvio_player ./src/tgvio_player",
                "COPY --from=player-web-build --chown=65532:65532 /web/dist ./player-web",
            ],
        )

    def test_player_image_records_the_pinned_front_end(self) -> None:
        dockerfile = (ROOT / "Dockerfile.player").read_text(encoding="utf-8")
        release = (ROOT / "scripts" / "player_release.sh").read_text(encoding="utf-8")
        self.assertIn("io.tgvio.player-web.revision=${PLAYER_WEB_COMMIT}", dockerfile)
        self.assertIn('--build-context "player-web=$web_source"', release)
        self.assertIn('--build-arg "PLAYER_WEB_COMMIT=$web_commit"', release)
        self.assertIn("scripts/player_web_source.sh", release)

    def test_bot_images_carry_no_front_end_sources(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        test_stage = dockerfile.split("FROM runtime-base AS test", 1)[1].split(
            "FROM runtime-base AS runtime", 1
        )[0]
        # The front end lives in TGVIO-Player; offline tests only need its pin.
        self.assertNotIn("COPY player", dockerfile)
        self.assertIn("player-web.lock", test_stage)

    def test_player_lock_excludes_bot_dependencies(self) -> None:
        lock = (ROOT / "requirements.player.lock").read_text(encoding="utf-8")
        self.assertIn("aiohttp==", lock)
        for forbidden in ("telethon==", "cryptg==", "yt-dlp=="):
            self.assertNotIn(forbidden, lock)

    def test_player_scripts_are_shell_valid_and_target_only_player(self) -> None:
        for name in ("player_release.sh", "player_deploy.sh", "player_rollback.sh", "player_remote_release.sh"):
            path = ROOT / "scripts" / name
            subprocess.run(["bash", "-n", str(path)], check=True)
        deploy = (ROOT / "scripts" / "player_deploy.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "scripts" / "player_rollback.sh").read_text(encoding="utf-8")
        self.assertIn("--no-deps tgvio-player", deploy)
        self.assertIn("--no-deps tgvio-player", rollback)
        self.assertNotIn("docker-compose.yml", deploy)
        self.assertNotIn("docker-compose.yml", rollback)

    def test_remote_player_release_keeps_rollback_and_bot_guards(self) -> None:
        remote = (ROOT / "scripts" / "player_remote_release.sh").read_text(encoding="utf-8")
        self.assertNotIn("docker-compose.yml", remote)
        self.assertIn("player_deploy.sh", remote)
        # A rollback point exists before the switch, with a consistent database copy.
        for marker in ("player-image.id", "player.env", "player.sqlite3.before", ".backup(copy)"):
            self.assertIn(marker, remote)
        self.assertLess(remote.index("rollback-point"), remote.index("stage=switch"))
        # An unhealthy image is rolled back; the live env only changes after health.
        self.assertIn("player_rollback.sh", remote)
        self.assertLess(remote.index("stage=health"), remote.index('mv -f "$ENV_FILE.next" "$ENV_FILE"'))
        self.assertIn("the Bot container changed during a Player release", remote)
        self.assertIn("flock -n 9", remote)
        # Image IDs differ between Docker image stores; provenance is checked by label.
        self.assertIn('"io.tgvio.player-web.revision"', remote)
        self.assertNotIn("loaded image id mismatch", remote)

    def test_remote_player_release_rejects_malformed_arguments(self) -> None:
        script = ROOT / "scripts" / "player_remote_release.sh"
        good = ["player-abcdef0-20261008T000000Z", "a" * 40, "b" * 40, "c" * 64, "d" * 64]
        for index, bad in ((0, "../escape"), (1, "main"), (2, "latest"), (3, "x"), (4, "y")):
            arguments = list(good)
            arguments[index] = bad
            result = subprocess.run(["bash", str(script), *arguments], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("player_release_failed", result.stderr)

    def test_one_deploy_entry_releases_bot_and_player(self) -> None:
        entry = (ROOT / "scripts" / "deploy_hostdzire.py").read_text(encoding="utf-8")
        self.assertIn('choices=("all", "bot", "player")', entry)
        self.assertIn('default="all"', entry)
        self.assertIn("deploy_player(repo, head, ssh, scp", entry)
        player = (ROOT / "scripts" / "player_hostdzire.py").read_text(encoding="utf-8")
        # The image must carry both the release commit and the pinned front end.
        self.assertIn('"org.opencontainers.image.revision"', player)
        self.assertIn('"io.tgvio.player-web.revision"', player)
        self.assertIn("player_release.sh", player)

    def test_player_env_template_has_no_bot_credentials(self) -> None:
        template = (ROOT / "deploy" / "player.env.example").read_text(encoding="utf-8")
        self.assertIn("TGVIO_PLAYER_ACCESS_SECRET=", template)
        self.assertIn("TGVIO_PLAYER_WEBDAV_PASSWORD=", template)
        for forbidden in ("BOT_TOKEN=", "API_HASH=", "API_ID=", "TGVIO_SOURCE_SESSION="):
            self.assertNotIn(forbidden, template)

    def test_recovery_key_is_passed_only_to_the_player_container(self) -> None:
        compose = (ROOT / "docker-compose.player.yml").read_text(encoding="utf-8")
        template = (ROOT / "deploy" / "player.env.example").read_text(encoding="utf-8")
        self.assertIn("TGVIO_PLAYER_RECOVERY_KEY:", compose)
        self.assertIn("TGVIO_PLAYER_RECOVERY_KEY=", template)

    def test_player_compose_renders_with_an_isolated_environment(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not installed")
        values = {
            "TGVIO_PLAYER_IMAGE": "tgvio-player:test",
            "TGVIO_PLAYER_ENABLED": "true",
            "TGVIO_PLAYER_PORT": "8790",
            "TGVIO_PLAYER_HOST_DATA_DIR": "/tmp/tgvio-player-test-data",
            "TGVIO_PLAYER_ACCESS_SECRET": "test-only-access-secret",
            "TGVIO_PLAYER_RECOVERY_KEY": "r" * 43,
            "TGVIO_PLAYER_WEBDAV_URL": "https://webdav.invalid/read-only",
            "TGVIO_PLAYER_WEBDAV_USER": "test-reader",
            "TGVIO_PLAYER_WEBDAV_PASSWORD": "test-only-password",
            "TGVIO_PLAYER_REMOTE_ROOT": "TGVIO",
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in values.items()))
            handle.flush()
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    handle.name,
                    "-f",
                    str(ROOT / "docker-compose.player.yml"),
                    "--profile",
                    "player",
                    "config",
                    "--quiet",
                ],
                cwd=ROOT,
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
