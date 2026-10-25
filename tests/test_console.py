from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio.adapters.web.auth import COOKIE, ConsoleSessions
from tgvio.adapters.web.server import ConsoleServer
from tgvio.application.bulk_retry import BulkRetryService
from tgvio.application.console import ConsoleBaseline, ConsoleService
from tgvio.application.job_control import JobControlService
from tgvio.application.job_runner import JobRunner
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository

SECRET = "s" * 40
OWNER = 7


class FakeSource:
    """The personal-account coordinator, as the console uses it."""

    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.awaiting = None
        self.grabbed: list[tuple] = []
        self.codes: list[str] = []

    def status_line(self) -> str:
        return "已登录 · user_id `1`" if self.active else "未登录"

    def whitelist(self) -> list[str]:
        return ["@clips"]

    def sources(self) -> list[dict]:
        return [{"index": 0, "id": "-1001", "label": "clips"}] if self.active else []

    async def request_code(self, phone: str) -> str:
        self.codes.append(phone)
        self.awaiting = "code"
        return "验证码已发送"

    async def list_media(self, index, *, page, page_size, refresh):
        item = SimpleNamespace(
            message_id=11, fingerprint="fp11", has_video=True, is_photo_only=False, item_count=1,
            video_count=1, photo_count=0, size_bytes=1000, duration_seconds=3.0, width=720, height=1280,
            date=None, caption="hello", is_ad=False, already_submitted=False,
        )
        return [item], False, "clips"

    async def grab_message(self, index, message_id, *, fingerprint="", options=None):
        self.grabbed.append(("one", index, message_id, fingerprint, options))
        return 1, "clips", 1, 0

    async def grab_selection(self, selections, *, fingerprints=(), options=None):
        self.grabbed.append(("merge", tuple(selections), tuple(fingerprints), options))
        return len(selections), "clips", 0, len(selections), 0


class ConsoleTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.scheduled: list[str] = []
        control = JobControlService(self.repo)
        flags = RuntimeFlags()
        await flags.load(self.repo)
        self.console = ConsoleService(
            self.repo,
            owner_id=OWNER,
            baseline=ConsoleBaseline(
                destination="@channel", cover_mode=True, forward_caption=True,
                archive_enabled=True, archive_root="115/videos", download_dir=Path(self.tmp.name),
            ),
            control=control,
            schedule=lambda job: self.scheduled.append(job.id),
            bulk_retry=BulkRetryService(self.repo, control),
            runtime_flags=flags,
        )
        self.source = FakeSource()
        self.server = ConsoleServer(
            self.console, ConsoleSessions(SECRET), host="127.0.0.1", port=0,
            source=self.source, archive_enabled=True,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def login(self) -> None:
        response = await self.client.post("/api/login", json={"secret": SECRET}, headers=self.origin)
        self.assertEqual(response.status, 200)
        # The cookie is Secure (HTTPS behind nginx); this plain-HTTP test client sends it by hand.
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly; Secure; SameSite=Strict", cookie)
        self.client.session.headers["Cookie"] = cookie.split(";", 1)[0]

    async def job(self, state: JobState = JobState.RECEIVED, **policy) -> Job:
        job = Job(
            owner_id=OWNER, destination="@channel", policy=dict(policy),
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="telegram:1:1", name="clip.mp4", size_bytes=10)],
        )
        await self.repo.create(job)
        if state != JobState.RECEIVED:
            job = await self.repo.transition(job.id, JobState.FAILED, event_type="test", error_code="download_failed")
        return job


class AuthTests(ConsoleTestCase):
    async def test_api_needs_a_session_and_the_secret(self) -> None:
        self.assertEqual((await self.client.get("/api/status")).status, 401)
        bad = await self.client.post("/api/login", json={"secret": "nope"}, headers=self.origin)
        self.assertEqual(bad.status, 401)
        await self.login()
        self.assertEqual((await self.client.get("/api/status")).status, 200)
        self.assertTrue(self.client.session.headers["Cookie"].startswith(f"{COOKIE}="))

    async def test_mutations_need_a_same_origin_header(self) -> None:
        await self.login()
        self.assertEqual((await self.client.post("/api/queue/pause")).status, 403)
        evil = await self.client.post("/api/queue/pause", headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status, 403)
        self.assertEqual((await self.client.post("/api/queue/pause", headers=self.origin)).status, 200)

    async def test_repeated_wrong_secrets_lock_the_client_out(self) -> None:
        for _ in range(5):
            await self.client.post("/api/login", json={"secret": "nope"}, headers=self.origin)
        locked = await self.client.post("/api/login", json={"secret": SECRET}, headers=self.origin)
        self.assertEqual(locked.status, 429)

    async def test_security_headers_and_no_store(self) -> None:
        await self.login()
        response = await self.client.get("/api/status")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class JobTests(ConsoleTestCase):
    async def test_jobs_list_detail_and_foreign_jobs_are_hidden(self) -> None:
        await self.login()
        mine = await self.job()
        foreign = Job(owner_id=99, destination="@x", items=[])
        await self.repo.create(foreign)
        listed = await (await self.client.get("/api/jobs?filter=running")).json()
        self.assertEqual([item["id"] for item in listed["items"]], [mine.id])
        self.assertEqual(listed["items"][0]["media"]["video"], 1)
        detail = await (await self.client.get(f"/api/jobs/{mine.id}")).json()
        self.assertEqual(detail["items"][0]["name"], "clip.mp4")
        self.assertEqual((await self.client.get(f"/api/jobs/{foreign.id}")).status, 404)

    async def test_failed_job_is_retried_and_rescheduled(self) -> None:
        await self.login()
        failed = await self.job(JobState.FAILED)
        failures = await (await self.client.get("/api/jobs?filter=failed")).json()
        self.assertTrue(failures["items"][0]["retryable"])
        response = await self.client.post(f"/api/jobs/{failed.id}/retry", headers=self.origin)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.scheduled, [failed.id])

    async def test_hold_resume_and_cancel(self) -> None:
        await self.login()
        job = await self.job()
        held = await (await self.client.post(f"/api/jobs/{job.id}/hold", headers=self.origin)).json()
        self.assertTrue(held["held"])
        await self.client.post(f"/api/jobs/{job.id}/resume", headers=self.origin)
        self.assertEqual(self.scheduled, [job.id])
        # A queued job stops at the worker's next checkpoint; the flag is durable.
        cancelled = await (await self.client.post(f"/api/jobs/{job.id}/cancel", headers=self.origin)).json()
        self.assertTrue(cancelled["cancel_requested"])
        failed = await self.job(JobState.FAILED)
        refused = await self.client.post(f"/api/jobs/{failed.id}/cancel", headers=self.origin)
        self.assertEqual(refused.status, 409)
        self.assertEqual((await self.client.post(f"/api/jobs/{job.id}/explode", headers=self.origin)).status, 404)


class SettingsTests(ConsoleTestCase):
    async def test_style_spoiler_and_flags_round_trip(self) -> None:
        await self.login()
        current = await (await self.client.get("/api/settings")).json()
        self.assertEqual(current["style"], "default")
        self.assertEqual(current["style_policy"], {"cover_mode": True, "forward_caption": True})
        updated = await (await self.client.put(
            "/api/settings", headers=self.origin,
            json={"style": "minimal", "spoiler_mode": "always_spoiler", "flags": {"alerts_enabled": False}},
        )).json()
        self.assertEqual(updated["style"], "minimal")
        self.assertEqual(updated["spoiler_mode"], "always_spoiler")
        self.assertFalse(updated["flags"]["alerts_enabled"])
        bad = await self.client.put("/api/settings", headers=self.origin, json={"spoiler_mode": "ask"})
        self.assertEqual(bad.status, 400)


class SourceTests(ConsoleTestCase):
    async def test_browse_lists_the_source_media(self) -> None:
        await self.login()
        page = await (await self.client.get("/api/source/0/media")).json()
        self.assertEqual(page["items"][0]["message_id"], 11)
        self.assertEqual(page["items"][0]["kind"], "video")
        self.assertEqual((await self.client.get("/api/source/5/media")).status, 404)

    async def test_submit_one_job_per_row_or_merged_with_options(self) -> None:
        await self.login()
        rows = [{"source": 0, "message_id": 11, "fingerprint": "a"}, {"source": 0, "message_id": 12, "fingerprint": "b"}]
        separate = await (await self.client.post("/api/submit", headers=self.origin, json={"items": rows})).json()
        self.assertEqual(separate["jobs"], 2)
        merged = await (await self.client.post(
            "/api/submit", headers=self.origin,
            json={"items": rows, "merge": True, "mode": "archive", "spoiler": "always_normal"},
        )).json()
        self.assertEqual(merged["jobs"], 1)
        options = self.source.grabbed[-1][3]
        self.assertEqual(options, {"archive_only": True, "spoiler_mode": SpoilerMode.ALWAYS_NORMAL})

    async def test_submit_rejects_bad_rows(self) -> None:
        await self.login()
        for body in ({"items": []}, {"items": [{"source": 3, "message_id": 1}]},
                     {"items": [{"source": 0, "message_id": True}]},
                     {"items": [{"source": 0, "message_id": 1}], "mode": "forward"}):
            response = await self.client.post("/api/submit", headers=self.origin, json=body)
            self.assertEqual(response.status, 400, body)

    async def test_login_steps_go_through_the_coordinator(self) -> None:
        self.source.active = False
        await self.login()
        response = await (await self.client.post("/api/source/login/phone", headers=self.origin,
                                                 json={"value": "+1 555"})).json()
        self.assertEqual(self.source.codes, ["+1 555"])
        self.assertEqual(response["awaiting"], "code")
        self.assertEqual((await self.client.get("/api/source/0/media")).status, 409)


class ArchiveOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_planned_archive_only_job_completes_without_publishing(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = SQLiteJobRepository(Path(tmp) / "state.sqlite3")
            await repo.open()
            try:
                job = Job(owner_id=OWNER, destination="@c", items=[], policy={"archive_only": True})
                await repo.create(job)
                for state in (JobState.DOWNLOADING, JobState.DOWNLOADED, JobState.ANALYZING,
                              JobState.ANALYZED, JobState.PLANNED):
                    job = await repo.transition(job.id, state, event_type="test")

                class Execution:
                    async def execute(self, job, plan):
                        raise AssertionError("archive-only jobs are never published")

                runner = JobRunner(repo, ingestion=None, execution=Execution())  # type: ignore[arg-type]
                done = await runner.publish(job)
                self.assertEqual(done.state, JobState.SUCCEEDED)
            finally:
                await repo.close()


if __name__ == "__main__":
    unittest.main()
