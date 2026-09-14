from __future__ import annotations

import json
import re
import secrets

from tgvio.application.collection_editing import (
    CollectionEditingService,
    DraftRevisionConflict,
    DraftUnavailableError,
)
from tgvio.application.ports import JobRepository
from tgvio.domain.collection_editing import CollectionDraft, DraftState
from tgvio.domain.intake import CollectionEntryKind
from tgvio.domain.suggestion import (
    Suggestion,
    SuggestionApplication,
    SuggestionKind,
)


def _natural_key(value: str) -> tuple:
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value or "")
    )


class SuggestionService:
    """Deterministic, local-only organization advice. Never auto-changes content."""

    def __init__(self, repository: JobRepository, editing: CollectionEditingService) -> None:
        self._repository = repository
        self._editing = editing

    async def analyze(self, session_id: str) -> list[Suggestion]:
        entries = await self._editing.entries(session_id)
        media = [
            item
            for item in entries
            if item.entry.kind == CollectionEntryKind.MEDIA
        ]
        visible = [item for item in media if not item.excluded and item.entry.id is not None]
        suggestions: list[Suggestion] = []

        photos = [item for item in visible if str(item.entry.payload.get("kind")) == "photo"]
        videos = [item for item in visible if str(item.entry.payload.get("kind")) == "video"]
        if photos:
            suggestions.append(
                Suggestion(
                    kind=SuggestionKind.COVER,
                    title="推荐封面",
                    detail=f"使用第 1 张图片作为封面（共 {len(photos)} 张图片可选）。",
                    entry_ids=(int(photos[0].entry.id),),
                )
            )
        elif videos:
            suggestions.append(
                Suggestion(
                    kind=SuggestionKind.COVER,
                    title="推荐封面",
                    detail="没有图片，可用第 1 个视频的默认截帧作为封面。",
                    entry_ids=(int(videos[0].entry.id),),
                )
            )

        ordered = sorted(
            visible,
            key=lambda item: _natural_key(
                str(item.entry.payload.get("name") or item.entry.payload.get("source") or "")
            ),
        )
        current_ids = [int(item.entry.id) for item in visible]
        sorted_ids = [int(item.entry.id) for item in ordered]
        if sorted_ids and sorted_ids != current_ids:
            suggestions.append(
                Suggestion(
                    kind=SuggestionKind.ORDER,
                    title="推荐排序",
                    detail="按文件名自然顺序排列，便于成组发布。",
                    entry_ids=tuple(sorted_ids),
                )
            )

        groups: dict[tuple[str, int], list[int]] = {}
        uncertain_groups: dict[tuple[str, int], list[int]] = {}
        for item in visible:
            payload = item.entry.payload
            metadata = payload.get("metadata") or {}
            sha = str(metadata.get("sha256") or payload.get("sha256") or "")
            key = (sha, int(payload.get("size_bytes", 0) or 0))
            if sha:
                groups.setdefault(key, []).append(int(item.entry.id))
            else:
                name_key = (str(payload.get("name") or ""), int(payload.get("size_bytes", 0) or 0))
                uncertain_groups.setdefault(name_key, []).append(int(item.entry.id))
        for ids in groups.values():
            if len(ids) > 1:
                suggestions.append(
                    Suggestion(
                        kind=SuggestionKind.DUPLICATE,
                        title="重复内容",
                        detail=f"检测到 {len(ids)} 个内容完全相同的媒体。",
                        entry_ids=tuple(ids),
                    )
                )
        for ids in uncertain_groups.values():
            if len(ids) > 1:
                suggestions.append(
                    Suggestion(
                        kind=SuggestionKind.DUPLICATE,
                        title="疑似重复",
                        detail=f"{len(ids)} 个媒体同名同大小，可能是重复；请自行确认。",
                        entry_ids=tuple(ids),
                        uncertain=True,
                    )
                )
        return suggestions

    async def apply_cover(
        self,
        *,
        owner_id: int,
        session_id: str,
        entry_id: int,
        expected_revision: int,
    ) -> CollectionDraft:
        draft = await self._editing.select_cover(
            owner_id=owner_id,
            session_id=session_id,
            entry_id=entry_id,
            expected_revision=expected_revision,
        )
        await self._record(
            owner_id=owner_id,
            session_id=session_id,
            kind=SuggestionKind.COVER.value,
            before_revision=expected_revision,
            before={"cover_entry_id": None},
            after={"cover_entry_id": int(entry_id)},
        )
        return draft

    async def apply_order(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        entries = await self._editing.entries(session_id)
        visible = [
            item
            for item in entries
            if item.entry.kind == CollectionEntryKind.MEDIA
            and not item.excluded
            and item.entry.id is not None
        ]
        excluded = [
            item
            for item in entries
            if item.entry.kind == CollectionEntryKind.MEDIA and item.excluded
        ]
        ordered = sorted(
            visible,
            key=lambda item: _natural_key(
                str(item.entry.payload.get("name") or item.entry.payload.get("source") or "")
            ),
        ) + excluded
        before = await self._repository.overlay_snapshot(session_id)
        positions = {
            str(item.entry.id): index for index, item in enumerate(ordered)
        }
        after = {
            "positions": positions,
            "excluded": dict(before.get("excluded") or {}),
            "cover_entry_id": before.get("cover_entry_id"),
        }
        draft = await self._repository.apply_overlay(
            session_id, after, expected_revision=expected_revision
        )
        if draft is None:
            raise DraftRevisionConflict("draft revision changed")
        await self._record(
            owner_id=owner_id,
            session_id=session_id,
            kind=SuggestionKind.ORDER.value,
            before_revision=expected_revision,
            before=before,
            after=after,
        )
        return draft

    async def undo(
        self,
        *,
        owner_id: int,
        session_id: str,
        expected_revision: int,
    ) -> CollectionDraft:
        application = await self._repository.get_active_suggestion_application(session_id)
        if (
            application is None
            or int(application.owner_id) != int(owner_id)
            or int(application.revision_applied) != int(expected_revision)
        ):
            raise DraftUnavailableError("no revertible suggestion")
        before = json.loads(application.before_json)
        restored = await self._repository.apply_overlay(
            session_id,
            {
                "positions": before.get("positions") or {},
                "excluded": before.get("excluded") or {},
                "cover_entry_id": before.get("cover_entry_id"),
            },
            expected_revision=expected_revision,
        )
        if restored is None:
            raise DraftRevisionConflict("draft revision changed")
        await self._repository.consume_suggestion_application(application.id)
        return restored

    async def _record(
        self,
        *,
        owner_id: int,
        session_id: str,
        kind: str,
        before_revision: int,
        before: dict,
        after: dict,
    ) -> None:
        await self._repository.create_suggestion_application(
            SuggestionApplication(
                id=secrets.token_urlsafe(12),
                session_id=session_id,
                owner_id=int(owner_id),
                kind=kind,
                revision_applied=int(before_revision) + 1,
                before_json=json.dumps(before, ensure_ascii=False, sort_keys=True),
                after_json=json.dumps(after, ensure_ascii=False, sort_keys=True),
            )
        )
