"""Player half of the HostDZire release, driven by deploy_hostdzire.py.

The image is built locally by player_release.sh from the pushed commit and the
TGVIO-Player commit pinned in player-web.lock, its labels are checked, then the
image and the release source travel to HostDZire with SHA-256 checks on both
ends. scripts/player_remote_release.sh performs the switch there.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

PLAYER_ROOT = "/root/tgvio-player"


class PlayerDeployError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_web_commit(repo: Path) -> str:
    match = re.search(r"^commit=([0-9a-f]{40})$", (repo / "player-web.lock").read_text(encoding="utf-8"), re.M)
    if not match:
        raise PlayerDeployError("player-web.lock does not pin a full commit")
    return match.group(1)


def _label(image: str, name: str) -> str:
    return subprocess.run(
        ["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{name}"}}}}', image],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def deploy_player(repo: Path, head: str, ssh: list[str], scp: list[str], target: str) -> str:
    """Build, transfer and switch the Player. Returns the release id."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release_id = f"player-{head[:7]}-{timestamp}"
    tag = f"tgvio-player:{release_id}"
    web_commit = _locked_web_commit(repo)

    subprocess.run(
        ["bash", str(repo / "scripts" / "player_release.sh"), "--tag", tag, "--commit", head, "--release-id", release_id],
        cwd=repo, check=True,
    )
    if _label(tag, "org.opencontainers.image.revision") != head:
        raise PlayerDeployError("candidate image revision label does not match HEAD")
    if _label(tag, "io.tgvio.player-web.revision") != web_commit:
        raise PlayerDeployError("candidate image front-end label does not match player-web.lock")

    remote_dir = f"{PLAYER_ROOT}/releases/{release_id}"
    with tempfile.TemporaryDirectory(prefix="tgvio-player-release-") as temporary:
        folder = Path(temporary)
        image_archive = folder / "player-image.tar.gz"
        with image_archive.open("wb") as output:
            save = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE)
            assert save.stdout is not None
            subprocess.run(["gzip", "-c"], stdin=save.stdout, stdout=output, check=True)
            save.stdout.close()
            if save.wait() != 0:
                raise PlayerDeployError("docker save failed")
        source_archive = folder / "source.tar.gz"
        subprocess.run(["git", "archive", "--format=tar.gz", f"--output={source_archive}", head], cwd=repo, check=True)
        image_sha, source_sha = _sha256(image_archive), _sha256(source_archive)

        quoted = shlex.quote(remote_dir)
        subprocess.run([*ssh, f"set -eu; umask 077; test ! -e {quoted}; mkdir -p {quoted}/incoming"], check=True)
        subprocess.run([*scp, str(image_archive), str(source_archive), f"{target}:{remote_dir}/incoming/"], check=True)

    # The remote script comes from the uploaded source itself, so the switch logic is
    # exactly the reviewed commit. It is extracted first, then run from there.
    extract_script = (
        f"set -eu; umask 077; d={shlex.quote(remote_dir)}; "
        f'test "$(sha256sum "$d/incoming/source.tar.gz" | cut -d" " -f1)" = {shlex.quote(source_sha)}; '
        'mkdir -p "$d/bootstrap"; tar -xzf "$d/incoming/source.tar.gz" -C "$d/bootstrap" scripts/player_remote_release.sh'
    )
    subprocess.run([*ssh, extract_script], check=True)
    remote = " ".join(shlex.quote(value) for value in [
        "bash", f"{remote_dir}/bootstrap/scripts/player_remote_release.sh", release_id, head, web_commit, image_sha, source_sha,
    ])
    subprocess.run([*ssh, remote], check=True)
    return release_id
