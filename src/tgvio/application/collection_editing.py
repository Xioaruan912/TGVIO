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
        submission = await self._repository.get_submission(session_id)
        if submission is not None:
            raise DraftUnavailableError("submission already accepted")
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

    # ------------------------------------------------------------------ style
    async def effective_style(self, owner_id: int, session_id: str) -> dict[str, Any]:
        """Priority: draft override > owner default > system default."""
        from tgvio.application.publish_styles import resolve_style

        draft = await self._repository.get_draft(session_id)
        if draft is not None and draft.style_json:
            _, policy = resolve_style(draft.style_json)
            return policy
        preference = await self._repository.get_user_preference(int(owner_id))
        _, policy = resolve_style(preference.style_json)
        return policy

    async def effective_style_name(self, owner_id: int, session_id: str) -> str:
        from tgvio.application.publish_styles import resolve_style

        draft = await self._repository.get_draft(session_id)
        if draft is not None and draft.style_json:
            name, _ = resolve_style(draft.style_json)
            return name
        preference = await self._repository.get_user_preference(int(owner_id))
        name, _ = resolve_style(preference.style_json)
        return name

    async def set_draft_style(
        self,
        *,
        owner_id: int,
        session_id: str,
        style_json: str | None,
        expected_revision: int,
    ) -> CollectionDraft:
        await self._require_editable(owner_id, session_id, expected_revision)
        target = await self._repository.set_draft_style(
            session_id, style_json=style_json, expected_revision=expected_revision
        )
        if target is None:
            raise DraftRevisionConflict("draft revision changed")
        return target

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

    async def preview_content(
        self,
        session_id: str,
    ) -> tuple[str, list[IncomingMedia]] | None:
        """Real caption and ordered media used when this draft is published.

        Mirrors the confirm snapshot so the effect preview can show the exact
        caption instead of a generic notice.
        """

        draft = await self._repository.get_draft(session_id)
        if draft is None:
            return None
        media, _entry_ids, caption = await self._ordered_media(session_id)
        return caption, media

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

    @staticmethod
    def _frozen_content(
        *,
        session_id: str,
        revision: int,
        media: list[IncomingMedia],
        caption: str,
        cover_entry_id: int | None,
        cover_index: int | None,
        spoiler_mode: SpoilerMode,
        style_policy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "session_id": str(session_id),
            "revision": int(revision),
            "media": [IntakeService._media_payload(item) for item in media],
            "caption": str(caption or ""),
            "cover_entry_id": None if cover_entry_id is None else int(cover_entry_id),
            "cover_index": None if cover_index is None else int(cover_index),
            "spoiler_mode": spoiler_mode.value,
            "style": dict(style_policy or {}),
        }

    @staticmethod
    def _content_hash(content: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

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
        content = self._frozen_content(
            session_id=session_id,
            revision=int(expected_revision),
            media=media,
            caption=caption,
            cover_entry_id=cover_entry_id,
            cover_index=cover_index,
            spoiler_mode=spoiler_mode,
            style_policy=style_policy,
        )
        return FrozenCollection(
            session_id=session_id,
            revision=int(expected_revision),
            media=tuple(media),
            caption=caption,
            cover_entry_id=cover_entry_id,
            cover_index=cover_index,
            snapshot_hash=self._content_hash(content),
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
        if style_policy is None:
            style_policy = await self.effective_style(int(owner_id), session_id)
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
        owner = int(owner_id)
        operation = None
        try:
            operation = await self._tokens.inspect(
                token=token, owner_id=owner, action=CONFIRM_ACTION
            )
        except OperationTokenInvalidError:
            operation = None

        # A consumed token may only resume the exact submission it authorized.
        if operation is None:
            submission = await self._repository.get_submission_by_token(token)
            if submission is None or int(submission.owner_id) != owner:
                raise OperationTokenInvalidError(
                    "operation expired, changed, or was already consumed"
                )
            if submission.state == "created":
                return await self._result_from_submission(submission.session_id, submission.job_ids)
            return await self._resume_submission(submission)

        session_id = str(operation.resource_id)
        existing = await self._repository.get_submission(session_id)
        if existing is not None:
            if existing.token_id == token and int(existing.owner_id) == owner:
                if existing.state == "created":
                    return await self._result_from_submission(session_id, existing.job_ids)
                return await self._resume_submission(existing)
            raise OperationTokenInvalidError("submission already accepted by another confirmation")

        draft = await self._repository.get_draft(session_id)
        if (
            draft is None
            or int(draft.owner_id) != owner
            or draft.state not in {DraftState.COLLECTING, DraftState.PREVIEW, DraftState.SAVED}
            or int(draft.revision) != int(operation.expected_revision)
        ):
            raise DraftRevisionConflict("draft revision changed")
        media, entry_ids, caption = await self._ordered_media(session_id)
        if not media:
            raise CollectionEmptyError("collection contains no media")
        cover_entry_id = draft.cover_entry_id
        cover_index: int | None = None
        if cover_entry_id is not None and int(cover_entry_id) in entry_ids:
            cover_index = entry_ids.index(int(cover_entry_id))
        spoiler_mode = (await self._intake.get_user_preference(owner)).spoiler_mode
        if style_policy is None:
            style_policy = await self.effective_style(owner, session_id)
        content = self._frozen_content(
            session_id=session_id,
            revision=int(operation.expected_revision),
            media=media,
            caption=caption,
            cover_entry_id=cover_entry_id,
            cover_index=cover_index,
            spoiler_mode=spoiler_mode,
            style_policy=style_policy,
        )
        snapshot_hash = self._content_hash(content)
        payload_hash = self._tokens.payload_hash({"snapshot_hash": snapshot_hash})
        if payload_hash != operation.payload_hash:
            raise OperationTokenInvalidError("operation payload changed")

        chunk_size = max(1, int(max_items))
        media_payload = list(content["media"])
        parts = [
            media_payload[start : start + chunk_size]
            for start in range(0, len(media_payload), chunk_size)
        ]
        frozen_json = json.dumps(
            {
                **content,
                "max_items": chunk_size,
                "ask_timeout_seconds": int(ask_timeout_seconds),
                "destination": str(destination),
                "parts": parts,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            submission, _created = await self._repository.commit_frozen_submission(
                session_id=session_id,
                owner_id=owner,
                revision=int(operation.expected_revision),
                snapshot_hash=snapshot_hash,
                frozen_json=frozen_json,
                token_id=token,
                token=token,
                action=CONFIRM_ACTION,
                resource_type=CONFIRM_RESOURCE_TYPE,
                payload_hash=payload_hash,
            )
        except ValueError as exc:
            if "token" in str(exc):
                raise OperationTokenInvalidError(
                    "operation expired, changed, or was already consumed"
                ) from exc
            raise DraftRevisionConflict("draft revision changed") from exc
        if submission.token_id != token or int(submission.owner_id) != owner:
            raise OperationTokenInvalidError("submission already accepted by another confirmation")
        if submission.state == "created":
            return await self._result_from_submission(session_id, submission.job_ids)
        return await self._resume_submission(submission)

    async def _resume_submission(self, submission) -> CollectionFinalizeResult:
        payload: dict[str, Any] = {}
        if submission.frozen_json:
            try:
                parsed = json.loads(submission.frozen_json)
                if isinstance(parsed, dict):
                    payload = parsed
            except (TypeError, ValueError):
                payload = {}
        if not payload or payload.get("version") != 1:
            raise DraftUnavailableError("frozen submission snapshot is missing")
        content = {
            key: payload.get(key)
            for key in (
                "version", "session_id", "revision", "media", "caption",
                "cover_entry_id", "cover_index", "spoiler_mode", "style",
            )
        }
        if self._content_hash(content) != str(submission.snapshot_hash):
            raise DraftUnavailableError("frozen submission snapshot mismatch")

        session_id = str(submission.session_id)
        parts = payload.get("parts") or []
        chunk_size = max(1, int(payload.get("max_items") or 1))
        destination = str(payload.get("destination") or "")
        spoiler_mode = SpoilerMode(str(payload.get("spoiler_mode") or SpoilerMode.SOURCE.value))
        cover_index = payload.get("cover_index")
        caption = str(payload.get("caption") or "")
        style = payload.get("style") or {}
        ask_timeout = int(payload.get("ask_timeout_seconds") or 60)
        job_ids = list(submission.job_ids)
        created_ids: set[str] = set()

        for part_index, part in enumerate(parts):
            existing_job = await self._repository.get_collection_part_job(session_id, part_index)
            if existing_job is not None:
                if existing_job not in job_ids:
                    job_ids.append(existing_job)
                continue
            media = [IntakeService._media_from_payload(item) for item in part]
            policy: dict[str, Any] = {
                "collection_id": session_id,
                "collection_part_index": part_index,
                "collection_part_count": len(parts),
                "display_expected": True,
            }
            if part_index == 0 and caption:
                policy["collection_caption"] = caption
            if isinstance(cover_index, int) and cover_index // chunk_size == part_index:
                policy["cover_item_index"] = cover_index % chunk_size
            if style:
                policy["publish_style"] = dict(style)
            accepted = await self._intake.accept_once(
                owner_id=int(submission.owner_id),
                destination=destination,
                media=media,
                policy=policy,
                spoiler_mode=spoiler_mode,
                ask_timeout_seconds=ask_timeout,
            )
            await self._repository.record_collection_part_job(session_id, part_index, accepted.job.id)
            await self._repository.append_submission_job(session_id, accepted.job.id)
            if accepted.created:
                created_ids.add(accepted.job.id)
            if accepted.job.id not in job_ids:
                job_ids.append(accepted.job.id)

        session = await self._repository.get_collection(session_id)
        if session is not None and session.state.value == "open":
            try:
                await self._repository.finalize_collection(session_id, tuple(job_ids))
            except ValueError:
                pass
        await self._repository.finish_submission(
            session_id, job_ids=tuple(job_ids), state="created"
        )
        await self._repository.mark_draft_submitted(session_id)
        return await self._result_from_submission(
            session_id, tuple(job_ids), created_ids=created_ids
        )

    async def recover_submission(self, session_id: str) -> CollectionFinalizeResult | None:
        """Resume only a previously authorized durable snapshot, never draft data."""
        submission = await self._repository.get_submission(session_id)
        if submission is None or submission.state != "creating" or not submission.token_id:
            return None
        return await self._resume_submission(submission)

    async def _result_from_submission(
        self,
        session_id: str,
        job_ids: tuple[str, ...],
        *,
        created_ids: set[str] | None = None,
    ) -> CollectionFinalizeResult:
        session = await self._repository.get_collection(session_id)
        if session is None:
            raise DraftUnavailableError("collection session is unavailable")
        from tgvio.application.intake import IntakeAcceptResult

        freshly_created = created_ids or set()
        results: list[IntakeAcceptResult] = []
        for job_id in job_ids:
            job = await self._repository.get(job_id)
            if job is not None:
                results.append(
                    IntakeAcceptResult(job=job, created=job_id in freshly_created)
                )
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
