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
                "COPY player/web/package.json player/web/package-lock.json ./",
                "COPY player/web/index.html player/web/tsconfig.json player/web/vite.config.ts ./",
                "COPY player/web/src ./src",
                "COPY requirements.player.lock ./requirements.player.lock",
                "COPY src/tgvio_player ./src/tgvio_player",
                "COPY --from=player-web-build --chown=65532:65532 /web/dist ./player-web",
            ],
        )

    def test_player_lock_excludes_bot_dependencies(self) -> None:
        lock = (ROOT / "requirements.player.lock").read_text(encoding="utf-8")
        self.assertIn("aiohttp==", lock)
        for forbidden in ("telethon==", "cryptg==", "yt-dlp=="):
            self.assertNotIn(forbidden, lock)

    def test_player_scripts_are_shell_valid_and_target_only_player(self) -> None:
        for name in ("player_release.sh", "player_deploy.sh", "player_rollback.sh"):
            path = ROOT / "scripts" / name
            subprocess.run(["bash", "-n", str(path)], check=True)
        deploy = (ROOT / "scripts" / "player_deploy.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "scripts" / "player_rollback.sh").read_text(encoding="utf-8")
        self.assertIn("--no-deps tgvio-player", deploy)
        self.assertIn("--no-deps tgvio-player", rollback)
        self.assertNotIn("docker-compose.yml", deploy)
        self.assertNotIn("docker-compose.yml", rollback)

    def test_player_env_template_has_no_bot_credentials(self) -> None:
        template = (ROOT / "deploy" / "player.env.example").read_text(encoding="utf-8")
        self.assertIn("TGVIO_PLAYER_ACCESS_SECRET=", template)
        self.assertIn("TGVIO_PLAYER_WEBDAV_PASSWORD=", template)
        for forbidden in ("BOT_TOKEN=", "API_HASH=", "API_ID=", "TGVIO_SOURCE_SESSION="):
            self.assertNotIn(forbidden, template)

    def test_player_compose_renders_with_an_isolated_environment(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not installed")
        values = {
            "TGVIO_PLAYER_IMAGE": "tgvio-player:test",
            "TGVIO_PLAYER_ENABLED": "true",
            "TGVIO_PLAYER_PORT": "8790",
            "TGVIO_PLAYER_HOST_DATA_DIR": "/tmp/tgvio-player-test-data",
            "TGVIO_PLAYER_ACCESS_SECRET": "test-only-access-secret",
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
