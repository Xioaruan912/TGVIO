from __future__ import annotations

from tgvio.adapters.telegram.publish_transport_support import *  # noqa: F401,F403


class PublishLargeFileMixin:
    async def _send_split_bundle(
        self,
        target,
        reply_to,
        step: PublishStep,
        item: MediaItem,
        bundle,
        *,
        job_id: str,
    ) -> list[PublishReceipt]:
        receipts: list[PublishReceipt] = []
        playable = bundle.mode == "playable_video_segments"
        caption = item.caption if step.params.get("forward_caption", True) else ""
        header = self._caption_header(item, step)
        template = str(step.params.get("caption_template") or "").strip()
        note = (
            f"🎞 可独立播放视频分段：{bundle.original_name}\n"
            f"共 {len(bundle.parts)} 段；manifest 含原文件和每段 SHA-256。"
            if playable
            else (
                f"📦 可校验分卷：{bundle.original_name}\n"
                f"共 {len(bundle.parts)} 卷；下载 manifest 和全部 part 后可校验重组。"
            )
        )
        note = "\n".join(
            part for part in (header, note, caption or "", template) if part
        )
        note = self._with_footer(note, self._footer(step))
        try:
            manifest_sent = await self._send_visible_file(
                target,
                str(bundle.manifest_path),
                caption=note,
                force_document=True,
                reply_to=reply_to,
            )
            receipts.extend(
                self._tag_split_receipts(
                    self._receipts(manifest_sent, step, [item]),
                    role="manifest",
                    mode=bundle.mode,
                    part_index=None,
                    part_count=len(bundle.parts),
                )
            )
            for part_index, part in enumerate(bundle.parts, start=1):
                part_file = await self._prepare_local_media(
                    part,
                    kind=MediaKind.VIDEO if playable else MediaKind.DOCUMENT,
                    force_document=not playable,
                    supports_streaming=playable,
                    spoiler=item.spoiler,
                    job_id=job_id,
                    item_index=item.index,
                )
                part_sent = await self._send_visible_file(
                    target,
                    part_file,
                    caption=(
                        f"视频分段 {part_index}/{len(bundle.parts)}"
                        if playable
                        else f"分卷 {part_index}/{len(bundle.parts)}"
                    ),
                    force_document=not playable,
                    supports_streaming=playable,
                    reply_to=reply_to,
                )
                receipts.extend(
                    self._tag_split_receipts(
                        self._receipts(part_sent, step, [item]),
                        role="part",
                        mode=bundle.mode,
                        part_index=part_index,
                        part_count=len(bundle.parts),
                    )
                )
        except Exception as exc:
            if receipts:
                raise PublishTransportPartialError(str(exc), tuple(receipts)) from exc
            raise
        return receipts

    @staticmethod
    def _tag_split_receipts(
        receipts: list[PublishReceipt],
        *,
        role: str,
        mode: str,
        part_index: int | None,
        part_count: int,
    ) -> list[PublishReceipt]:
        return [
            PublishReceipt(
                effect_type=receipt.effect_type,
                external_chat_id=receipt.external_chat_id,
                external_message_id=receipt.external_message_id,
                detail={
                    **{
                        key: value
                        for key, value in receipt.detail.items()
                        if key != "reusable_ref"
                    },
                    "split_role": role,
                    "split_mode": mode,
                    "part_index": part_index,
                    "part_count": part_count,
                },
            )
            for receipt in receipts
        ]
