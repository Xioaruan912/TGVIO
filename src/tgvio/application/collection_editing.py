from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from tgvio.application.intake import (
    CollectionEmptyError,
    CollectionFinalizeResult,
    IncomingMedia,
    IntakeService,
)
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.ports import JobRepository
from tgvio.domain.collection_editing import (
    CollectionDraft,
    DraftEntry,
    DraftState,
    EditingField,
    EditingInteraction,
    FrozenCollection,
)
from tgvio.domain.intake import CollectionEntryKind, CollectionPreview, SpoilerMode
from tgvio.domain.job import MediaKind


CONFIRM_ACTION = "collection_confirm"
CONFIRM_RESOURCE_TYPE = "collection"
CAPTION_TTL_SECONDS = 180
MAX_SAVED_DRAFTS = 20


class DraftRevisionConflict(RuntimeError):
    pass


class DraftUnavailableError(RuntimeError):
    pass


class CollectionEditingService:
    """Revision-based editing overlay over open collection sessions."""

    def __init__(
        self,
        repository: JobRepository,
        intake: IntakeService,
        tokens: OperationTokenService,
    ) -> None:
        self._repository = repository
        self._intake = intake
        self._tokens = tokens

    @property
    def repository(self) -> JobRepository:
        return self._repository

    async def draft_for(self, owner_id: int, chat_id: int) -> CollectionDraft | None:
        return await self._repository.get_active_draft(int(owner_id), int(chat_id))

    async def draft(self, session_id: str) -> CollectionDraft | None:
        return await self._repository.get_draft(session_id)

    async def entries(self, session_id: str) -> list[DraftEntry]:
        return await self._repository.list_draft_entries(session_id)

    async def _require_editable(
        self,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        draft = await self._repository.get_draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id) or draft.state not in {
            DraftState.COLLECTING,
            DraftState.PREVIEW,
            DraftState.SAVED,
        }:
            raise DraftUnavailableError("draft is unavailable")
        if int(draft.revision) != int(expected_revision):
            raise DraftRevisionConflict("draft revision changed")
        return draft

    async def toggle_exclude(
        self,
        *,
        owner_id: int,
        session_id: str,
        entry_id: int,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        entries = await self._repository.list_draft_entries(session_id)
        current = next((item for item in entries if item.entry_id == int(entry_id)), None)
        if current is None:
            raise DraftUnavailableError("entry is unavailable")
        target = await self._repository.set_entry_excluded(
            session_id,
            int(entry_id),
            excluded=not current.excluded,
            expected_revision=expected_revision,
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def move(
        self,
        *,
        owner_id: int,
        session_id: str,
        entry_id: int,
        direction: int,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        target = await self._repository.move_draft_entry(
            session_id,
            int(entry_id),
            direction=1 if int(direction) >= 0 else -1,
            expected_revision=expected_revision,
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def select_cover(
        self,
        *,
        owner_id: int,
        session_id: str,
        entry_id: int,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        entries = await self._repository.list_draft_entries(session_id)
        chosen = next((item for item in entries if item.entry_id == int(entry_id)), None)
        if (
            chosen is None
            or chosen.excluded
            or chosen.entry.kind != CollectionEntryKind.MEDIA
        ):
            raise DraftUnavailableError("entry cannot be a cover")
        target = await self._repository.set_draft_cover(
            session_id,
            entry_id=int(entry_id),
            expected_revision=expected_revision,
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def clear_cover(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        target = await self._repository.set_draft_cover(
            session_id, entry_id=None, expected_revision=expected_revision
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def begin_caption(
        self,
        *,
        owner_id: int,
        chat_id: int,
        session_id: str,
        expected_revision: int,
    ) -> EditingInteraction:
        await self._require_editable(owner_id, session_id, expected_revision)
        interaction = EditingInteraction(
            id=secrets.token_urlsafe(12),
            owner_id=int(owner_id),
            chat_id=int(chat_id),
            session_id=session_id,
            field=EditingField.CAPTION,
            expected_revision=int(expected_revision),
            expires_at=int(time.time()) + CAPTION_TTL_SECONDS,
        )
        return await self._repository.create_editing_interaction(interaction)

    async def apply_pending_caption(
        self,
        *,
        owner_id: int,
        chat_id: int,
        text: str,
    ) -> CollectionDraft | None:
        interaction = await self._repository.get_active_editing_interaction(
            int(owner_id), int(chat_id)
        )
        if interaction is None or interaction.field != EditingField.CAPTION:
            return None
        consumed = await self._repository.consume_editing_interaction(
            interaction.id,
            owner_id=int(owner_id),
            expected_revision=interaction.expected_revision,
        )
        if not consumed:
            return None
        draft = await self._repository.get_draft(interaction.session_id)
        if draft is None or draft.state == DraftState.SUBMITTED:
            return None
        return await self._repository.set_draft_caption(
            interaction.session_id,
            text=str(text)[:1024],
            expected_revision=int(draft.revision),
        )

    async def clear_caption(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        target = await self._repository.set_draft_caption(
            session_id, text=None, expected_revision=expected_revision
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def save(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        target = await self._repository.save_draft(
            session_id, expected_revision=expected_revision
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

    async def activate(self, *, owner_id: int, session_id: str) -> CollectionDraft:
        draft = await self._repository.get_draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            raise DraftUnavailableError("draft is unavailable")
        target = await self._repository.activate_draft(session_id)
        if target is None:
            raise DraftUnavailableError("draft is unavailable")
        return target

    async def discard(self, *, owner_id: int, session_id: str) -> None:
        draft = await self._repository.get_draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            raise DraftUnavailableError("draft is unavailable")
        session = await self._repository.get_collection(session_id)
        if session is not None and session.state.value == "open":
            await self._repository.cancel_collection(session_id)
        else:
            await self._repository.mark_draft_discarded(session_id)

    async def save_count(self, owner_id: int) -> int:
        drafts = await self._repository.list_drafts(int(owner_id), limit=MAX_SAVED_DRAFTS + 1)
        return sum(1 for draft in drafts if draft.state == DraftState.SAVED)

    # --------------------------------------------------------------- snapshot
    async def _ordered_media(
        self,
        session_id: str,
    ) -> tuple[list[IncomingMedia], list[int], str]:
        entries = await self._repository.list_draft_entries(session_id)
        media_entries = [
            item for item in entries if item.entry.kind == CollectionEntryKind.MEDIA
        ]
        visible = [item for item in media_entries if not item.excluded]
        media = [
            IntakeService._media_from_payload(item.entry.payload) for item in visible
        ]
        entry_ids = [int(item.entry.id) for item in visible if item.entry.id is not None]
        draft = await self._repository.get_draft(session_id)
        if draft is not None and draft.caption_override is not None:
            caption = str(draft.caption_override)
        else:
            text_entries = [item.entry for item in entries if item.entry.kind == CollectionEntryKind.TEXT]
            caption = IntakeService._join_collection_texts(text_entries)
        return media, entry_ids, caption

    async def preview(
        self,
        *,
        session_id: str,
        cover_mode: bool = True,
        cover_limit: int = 10,
        album_limit: int = 10,
    ) -> CollectionPreview | None:
        draft = await self._repository.get_draft(session_id)
        if draft is None:
            return None
        media, entry_ids, caption = await self._ordered_media(session_id)
        photo_count = sum(1 for item in media if item.kind == MediaKind.PHOTO)
        video_count = sum(1 for item in media if item.kind == MediaKind.VIDEO)
        document_count = sum(1 for item in media if item.kind == MediaKind.DOCUMENT)
        total_bytes = sum(int(item.size_bytes) for item in media)
        lines = len([line for line in caption.splitlines() if line.strip()])
        cover_custom = (
            draft.cover_entry_id is not None and int(draft.cover_entry_id) in entry_ids
        )
        if not cover_mode:
            cover_plan = "直发频道（非封面模式）"
            discussion_groups = 0
        else:
            limit = max(1, int(cover_limit))
            album = max(1, int(album_limit))
            if cover_custom:
                cover_plan = "已选自定义封面"
            elif photo_count:
                cover_plan = f"前 {min(photo_count, limit)} 张图片"
            elif video_count:
                cover_plan = "首个视频截帧"
            else:
                cover_plan = "无"
            groups = _ceil(photo_count, album) + _ceil(video_count, album)
            discussion_groups = int(groups)
        return CollectionPreview(
            media_count=len(media),
            photo_count=int(photo_count),
            video_count=int(video_count),
            document_count=int(document_count),
            total_bytes=int(total_bytes),
            cover_plan=cover_plan,
            discussion_groups=discussion_groups,
            caption_lines=int(lines),
            caption_chars=len(caption),
        )

    async def freeze(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
        spoiler_mode: SpoilerMode,
        style_policy: dict[str, Any] | None = None,
    ) -> FrozenCollection:
        draft = await self._require_editable(owner_id, session_id, expected_revision)
        media, entry_ids, caption = await self._ordered_media(session_id)
        if not media:
            raise CollectionEmptyError("collection contains no media")
        cover_entry_id = draft.cover_entry_id
        cover_index: int | None = None
        if cover_entry_id is not None and int(cover_entry_id) in entry_ids:
            cover_index = entry_ids.index(int(cover_entry_id))
        payload = {
            "session_id": session_id,
            "revision": int(expected_revision),
            "media_entry_ids": entry_ids,
            "caption": caption,
            "cover_entry_id": cover_entry_id,
            "spoiler_mode": spoiler_mode.value,
            "style": dict(style_policy or {}),
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return FrozenCollection(
            session_id=session_id,
            revision=int(expected_revision),
            media=tuple(media),
            caption=caption,
            cover_entry_id=cover_entry_id,
            cover_index=cover_index,
            snapshot_hash=snapshot_hash,
        )

    async def issue_confirm(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
        spoiler_mode: SpoilerMode,
        style_policy: dict[str, Any] | None = None,
    ) -> str:
        frozen = await self.freeze(
            owner_id=owner_id,
            session_id=session_id,
            expected_revision=expected_revision,
            spoiler_mode=spoiler_mode,
            style_policy=style_policy,
        )
        token = await self._tokens.issue(
            owner_id=int(owner_id),
            action=CONFIRM_ACTION,
            resource_type=CONFIRM_RESOURCE_TYPE,
            resource_id=session_id,
            expected_revision=int(expected_revision),
            payload={"snapshot_hash": frozen.snapshot_hash},
        )
        return token.token

    async def confirm(
        self,
        *,
        owner_id: int,
        chat_id: int,
        token: str,
        destination: str,
        max_items: int,
        ask_timeout_seconds: int,
        style_policy: dict[str, Any] | None = None,
    ) -> CollectionFinalizeResult:
        operation = await self._tokens.inspect(
            token=token, owner_id=int(owner_id), action=CONFIRM_ACTION
        )
        session_id = str(operation.resource_id)
        existing = await self._repository.get_submission(session_id)
        if existing is not None and existing.state == "created":
            return await self._result_from_submission(session_id, existing.job_ids)
        preference_session = await self._repository.get_collection(session_id)
        if preference_session is None:
            raise DraftUnavailableError("collection session is unavailable")
        spoiler_mode = (await self._intake.get_user_preference(int(owner_id))).spoiler_mode
        frozen = await self.freeze(
            owner_id=int(owner_id),
            session_id=session_id,
            expected_revision=int(operation.expected_revision),
            spoiler_mode=spoiler_mode,
            style_policy=style_policy,
        )
        await self._repository.begin_submission(
            session_id,
            owner_id=int(owner_id),
            revision=frozen.revision,
            snapshot_hash=frozen.snapshot_hash,
        )
        try:
            await self._tokens.consume(
                token=token,
                owner_id=int(owner_id),
                action=CONFIRM_ACTION,
                resource_type=CONFIRM_RESOURCE_TYPE,
                resource_id=session_id,
                expected_revision=frozen.revision,
                payload={"snapshot_hash": frozen.snapshot_hash},
            )
        except OperationTokenInvalidError:
            # A concurrent click consumed the same token; the submission row keeps
            # the operation idempotent, so continue the frozen snapshot once safe.
            pass
        result = await self._intake.finalize_media(
            owner_id=int(owner_id),
            chat_id=int(chat_id),
            session_id=session_id,
            destination=destination,
            media=list(frozen.media),
            caption=frozen.caption,
            cover_index=frozen.cover_index,
            max_items=max_items,
            spoiler_mode=spoiler_mode,
            ask_timeout_seconds=ask_timeout_seconds,
            extra_policy=(
                {"publish_style": dict(style_policy)} if style_policy else None
            ),
        )
        await self._repository.finish_submission(
            session_id,
            job_ids=tuple(accepted.job.id for accepted in result.jobs),
            state="created",
        )
        await self._repository.mark_draft_submitted(session_id)
        return result

    async def _result_from_submission(
        self,
        session_id: str,
        job_ids: tuple[str, ...],
    ) -> CollectionFinalizeResult:
        session = await self._repository.get_collection(session_id)
        if session is None:
            raise DraftUnavailableError("collection session is unavailable")
        from tgvio.application.intake import IntakeAcceptResult

        results: list[IntakeAcceptResult] = []
        for job_id in job_ids:
            job = await self._repository.get(job_id)
            if job is not None:
                results.append(IntakeAcceptResult(job=job, created=False))
        return CollectionFinalizeResult(
            session=session,
            jobs=tuple(results),
            media_count=len(results),
            text_count=0,
        )


def _ceil(value: int, divisor: int) -> int:
    if divisor <= 0:
        return 0
    return (max(0, int(value)) + divisor - 1) // divisor
