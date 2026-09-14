from __future__ import annotations

from dataclasses import dataclass

from tgvio.application.ports import JobRepository
from tgvio.domain.job import Job, JobState
from tgvio.domain.operations import RevocationState

_VISIBLE_EFFECT_TYPES = {
    "telegram_channel_message",
    "telegram_discussion_message",
}


@dataclass(frozen=True, slots=True)
class ResultCard:
    job_id: str
    state: str
    media_total: int
    telegram_state: str
    confirmed_messages: int
    archive_state: str | None
    link_url: str | None
    link_reason: str | None
    undo_remaining: int = 0
    favorited: bool = False


def public_post_link(
    destination: str,
    channel_message_ids: list[str],
) -> tuple[str | None, str | None]:
    """Return a public t.me post link only when it can be proven.

    The link is built exclusively from a public ``@username`` destination and a
    confirmed channel message id. Numeric-id (private) destinations are never
    guessed, because Telegram exposes no verifiable public link for them here.
    """
    if not channel_message_ids:
        return None, "还没有确认的频道消息，暂时无法生成链接。"
    raw = str(destination or "").strip()
    if not raw.startswith("@"):
        return None, "目标频道不是公开 @用户名，无法安全生成可访问链接。"
    username = raw[1:].strip()
    if not username:
        return None, "目标频道用户名不可用，无法生成链接。"
    return f"https://t.me/{username}/{channel_message_ids[0]}", None


class ResultCardService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def build(
        self,
        job: Job,
        *,
        favorited: bool = False,
        undo_remaining: int = 0,
    ) -> ResultCard:
        plan = await self._repository.get_publish_plan(job.id)
        effects = await self._repository.list_publish_effects(plan.id) if plan else []
        visible = [
            effect for effect in effects if effect.effect_type in _VISIBLE_EFFECT_TYPES
        ]
        revocations = await self._repository.list_publish_effect_revocations(job.id)
        deleted_ids = {r.effect_id for r in revocations if r.state == RevocationState.DELETED}
        deleted = [e for e in visible if e.id in deleted_ids]
        visible = [e for e in visible if e.id not in deleted_ids]
        channel_ids = list(
            dict.fromkeys(
                str(effect.external_message_id)
                for effect in visible
                if effect.effect_type == "telegram_channel_message"
            )
        )
        package = await self._repository.get_archive_package_for_job(job.id)
        archive_state = package.state.value if package is not None else None

        if deleted:
            telegram_state = "partially_revoked" if visible else "revoked"
        elif job.state == JobState.SUCCEEDED:
            telegram_state = "succeeded"
        elif job.state == JobState.FAILED and job.error_code in {
            "publish_partial",
            "publish_uncertain",
        }:
            telegram_state = "uncertain"
        elif job.state == JobState.FAILED:
            telegram_state = "failed"
        elif job.state == JobState.CANCELLED:
            telegram_state = "cancelled"
        else:
            telegram_state = "pending"

        link_url, link_reason = public_post_link(job.destination, channel_ids)
        return ResultCard(
            job_id=job.id,
            state=job.state.value,
            media_total=len(job.items),
            telegram_state=telegram_state,
            confirmed_messages=len(visible),
            archive_state=archive_state,
            link_url=link_url,
            link_reason=link_reason,
            undo_remaining=int(undo_remaining),
            favorited=bool(favorited),
        )
