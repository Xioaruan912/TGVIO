"""Compatibility exports for presentation helpers.

New code should import from :mod:`src.views`. This shim remains during R1 so
older imports can move incrementally without maintaining a second renderer.
"""

from .views import (
    MODE_NAMES,
    SESSION_BTN_BEGIN,
    SESSION_BTN_END,
    PendingQueueItemView,
    QueueItemView,
    QueueViewState,
    mode_buttons,
    queue_view as render_queue_view,
    reply_keyboard,
)


def queue_view(pipeline, user_id: int):
    """Legacy adapter for callers that still pass a pipeline instance."""
    active = []
    for seq in sorted(pipeline.active_seqs):
        info = pipeline.active.get(seq) or {}
        if seq in pipeline._download_tasks:
            state = "download"
        elif seq == pipeline._uploading:
            state = "upload"
        elif seq in pipeline._paused_files:
            state = "paused"
        elif seq in pipeline.jobs:
            state = "ready"
        else:
            state = "queued"
        active.append(
            QueueItemView(
                seq=seq,
                position=pipeline._queue_position(seq),
                state=state,
                pct=info.get("pct"),
                item=info.get("item", 1),
                items=info.get("items", 1),
            )
        )
    pending = tuple(
        PendingQueueItemView(
            seq=seq,
            position=index,
            kind=pipeline.pending[seq].kind,
        )
        for index, seq in enumerate(sorted(pipeline.pending), start=1)
    )
    return render_queue_view(
        QueueViewState(
            show_progress=pipeline._show_progress(user_id),
            active=tuple(active),
            pending=pending,
            has_sessions=bool(pipeline.sessions),
            session_media=sum(s.media_count for s in pipeline.sessions.values()),
            session_texts=sum(s.text_count for s in pipeline.sessions.values()),
        )
    )


__all__ = [
    "MODE_NAMES",
    "SESSION_BTN_BEGIN",
    "SESSION_BTN_END",
    "mode_buttons",
    "reply_keyboard",
    "queue_view",
]
