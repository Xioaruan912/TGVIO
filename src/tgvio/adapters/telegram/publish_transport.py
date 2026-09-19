from __future__ import annotations

from tgvio.adapters.telegram.publish_transport_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.publish_albums import PublishAlbumMixin
from tgvio.adapters.telegram.publish_references import PublishReferenceMixin
from tgvio.adapters.telegram.publish_large_files import PublishLargeFileMixin


class TelethonPublishTransport(PublishAlbumMixin, PublishReferenceMixin, PublishLargeFileMixin):
    """Telegram transport for durable PublishPlan steps."""

    _UNSUPPORTED_STRATEGIES: set[str] = set()
    _KNOWN_STRATEGIES = {
        "native",
        "remux_faststart",
        "split_playable",
        "binary_volume",
        "reuse_reference",
        "document",
    }

    def __init__(
        self,
        client: TelegramClient,
        transformer: FFmpegMediaTransformer,
        work_root: Path,
        *,
        cover_width: int = 1280,
        split_part_bytes: int = 1900 * 1024 * 1024,
        discussion_resolver: DiscussionResolver | None = None,
        upload_workers: int = 1,
        upload_global_workers: int = 1,
        upload_part_size_kb: int = 512,
    ) -> None:
        self._client = client
        self._transformer = transformer
        self._work_root = work_root
        self._cover_width = cover_width
        self._split_part_bytes = split_part_bytes
        self._discussion_resolver = discussion_resolver
        self._discussion_root_lock = asyncio.Lock()
        self._uploader = (
            BoundedTelegramUploader(
                client,
                per_file_workers=upload_workers,
                global_workers=upload_global_workers,
                part_size_kb=upload_part_size_kb,
            )
            if upload_workers > 1
            else None
        )
        self._log = logging.getLogger("tgvio.telegram.publish")

    async def execute_step(
        self,
        job: Job,
        step: PublishStep,
        prior_effects: tuple[PublishEffect, ...],
    ) -> list[PublishReceipt]:
        # A linked discussion group exposes only the current automatic-forward
        # root through Bot API getChat. Serialize the small set of channel-root
        # steps so concurrent TGVIO jobs cannot race each other's root capture.
        if (
            self._discussion_resolver is not None
            and step.target == PublishTarget.CHANNEL
            and step.kind
            in {
                PublishStepKind.CHANNEL_COVER_ALBUM,
                PublishStepKind.CHANNEL_VIDEO_COVER,
            }
        ):
            async with self._discussion_root_lock:
                return await self._execute_step_unlocked(job, step, prior_effects)
        return await self._execute_step_unlocked(job, step, prior_effects)

    async def _execute_step_unlocked(
        self,
        job: Job,
        step: PublishStep,
        prior_effects: tuple[PublishEffect, ...],
    ) -> list[PublishReceipt]:
        items = self._step_items(job, step)
        self._validate_step(items, step)
        target, reply_to = await self._resolve_target(job, step, prior_effects)
        workdir = self._work_root / f"job-{job.id}" / "publish"

        split_strategies = {
            self._strategy(step, item.index)
            for item in items
            if self._strategy(step, item.index) in {"split_playable", "binary_volume"}
        }
        if split_strategies:
            if len(items) != 1 or len(split_strategies) != 1:
                raise UnsupportedPublishStep("large-file split step must contain exactly one media item")
            item = items[0]
            source = self._local_path(item)
            strategy = next(iter(split_strategies))
            if strategy == "split_playable":
                bundle = await self._transformer.make_playable_segments(
                    source,
                    workdir,
                    item_index=item.index,
                    part_bytes=self._split_part_bytes,
                    duration_seconds=item.duration_seconds,
                )
            else:
                bundle = await self._transformer.make_binary_volumes(
                    source,
                    workdir,
                    item_index=item.index,
                    part_bytes=self._split_part_bytes,
                )
            return await self._send_split_bundle(
                target,
                reply_to,
                step,
                item,
                bundle,
                job_id=job.id,
            )

        if step.kind == PublishStepKind.CHANNEL_VIDEO_COVER:
            item = items[0]
            source = self._local_path(item)
            cover = await self._custom_thumbnail(
                step,
                workdir,
                job_id=job.id,
                max_size=self._cover_width,
            )
            if cover is None:
                cover = await self._transformer.make_video_cover(
                    source,
                    workdir,
                    item_index=item.index,
                    duration_seconds=item.duration_seconds,
                    max_width=self._cover_width,
                )
            sent = await self._send_visible_file(
                target,
                str(cover),
                caption=self._caption(item, step),
                reply_to=reply_to,
            )
            return await self._finalize_receipts(job, step, self._receipts(sent, step, items))

        force_document = step.kind in {
            PublishStepKind.CHANNEL_DOCUMENT,
            PublishStepKind.DISCUSSION_DOCUMENT,
        } and not any(item.kind == MediaKind.AUDIO for item in items)
        files: list[object] = []
        for item in items:
            strategy = self._strategy(step, item.index)
            if strategy == "reuse_reference":
                reusable = await self._resolve_reusable_media(item)
                if reusable is not None:
                    files.append(reusable)
                    continue
            source = self._local_path(item)
            if strategy == "remux_faststart":
                source = await self._transformer.remux_faststart(
                    source,
                    workdir,
                    item_index=item.index,
                )
            thumbnail = None
            if item.kind == MediaKind.VIDEO:
                thumbnail = await self._custom_thumbnail(step, workdir, job_id=job.id)
                if thumbnail is None:
                    try:
                        thumbnail = await self._transformer.make_video_thumbnail(
                            source,
                            workdir,
                            item_index=item.index,
                            duration_seconds=item.duration_seconds,
                        )
                    except Exception as exc:
                        log_event(
                            self._log,
                            logging.WARNING,
                            "publish.thumbnail.failed",
                            "Video thumbnail generation failed",
                            job_id=job.id,
                            item_index=item.index,
                            exception_type=type(exc).__name__,
                            exc_info=True,
                        )
                if thumbnail is None:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "publish.thumbnail.missing",
                        "No non-blank thumbnail frame; relying on ffprobe video attributes",
                        job_id=job.id,
                        item_index=item.index,
                    )
            files.append(
                await self._prepare_local_media(
                    source,
                    kind=item.kind,
                    force_document=force_document,
                    supports_streaming=(not force_document and item.kind == MediaKind.VIDEO),
                    spoiler=item.spoiler,
                    thumbnail=thumbnail,
                    force_upload=len(items) > 1,
                    job_id=job.id,
                    item_index=item.index,
                    width=item.width,
                    height=item.height,
                    duration_seconds=item.duration_seconds,
                )
            )
        captions = [self._caption(item, step) for item in items]
        buttons = self._caption_buttons(step) if len(files) == 1 else None
        if len(files) > 1:
            sent = await self._send_album(
                target,
                files,
                captions,
                reply_to=reply_to,
                step=step,
                items=items,
            )
        else:
            sent = await self._send_visible_file(
                target,
                files[0],
                caption=captions[0],
                force_document=force_document,
                supports_streaming=not force_document and items[0].kind == MediaKind.VIDEO,
                reply_to=reply_to,
                buttons=buttons,
            )
        return await self._finalize_receipts(job, step, self._receipts(sent, step, items))

    async def _custom_thumbnail(
        self,
        step,
        workdir: Path,
        *,
        job_id: str,
        max_size: int = 320,
    ) -> Path | None:
        """Normalize an owner-supplied thumbnail, or return None to auto-generate."""

        custom = str(step.params.get("thumbnail_path", "") or "").strip()
        if not custom:
            return None
        path = Path(custom)
        if not path.is_file():
            log_event(
                self._log,
                logging.WARNING,
                "publish.thumbnail.custom_missing",
                "Custom thumbnail file is unavailable; falling back to a generated frame",
                job_id=job_id,
            )
            return None
        try:
            normalized = await self._transformer.normalize_thumbnail(
                path,
                workdir,
                max_size=max_size,
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "publish.thumbnail.custom_failed",
                "Custom thumbnail could not be normalized",
                job_id=job_id,
                exception_type=type(exc).__name__,
            )
            return None
        if normalized is None:
            log_event(
                self._log,
                logging.WARNING,
                "publish.thumbnail.custom_invalid",
                "Custom thumbnail is not a decodable image",
                job_id=job_id,
            )
        return normalized

    @staticmethod
    def _caption_buttons(step: PublishStep) -> list[list["Button"]] | None:
        raw = step.params.get("caption_buttons")
        if not raw:
            return None
        rows: list[list[Button]] = []
        for entry in raw:
            try:
                label, url = entry
            except (TypeError, ValueError):
                continue
            label = str(label).strip()
            url = str(url).strip()
            if not label or not url.lower().startswith(("http://", "https://")):
                continue
            rows.append([Button.url(label, url)])
        return rows or None

    async def _upload_local_file(
        self,
        source: Path,
        *,
        job_id: str | None,
        item_index: int | None,
    ):
        if self._uploader is None:
            return await self._client.upload_file(str(source))
        return await self._uploader.upload(
            source,
            job_id=job_id,
            item_index=item_index,
        )

    async def _send_visible_file(self, target, file, **kwargs):
        try:
            sent = await self._client.send_file(target, file, **kwargs)
        except PublishTransportUncertainError:
            raise
        except Exception as exc:
            raise PublishTransportUncertainError(str(exc)) from exc
        messages = self._normalize_messages(sent)
        if not messages or any(getattr(message, "id", None) is None for message in messages):
            raise PublishTransportUncertainError(
                "Telegram send returned no observable message receipt"
            )
        return sent

    def _validate_step(self, items: list[MediaItem], step: PublishStep) -> None:
        if not items:
            raise ValueError("publish step has no media items")
        strategies = {self._strategy(step, item.index) for item in items}
        unknown = strategies - self._KNOWN_STRATEGIES
        if unknown:
            raise UnsupportedPublishStep(
                f"unknown publish strategy: {','.join(sorted(unknown))}"
            )
        unsupported = strategies & self._UNSUPPORTED_STRATEGIES
        if unsupported:
            raise UnsupportedPublishStep(
                f"publish strategy not implemented: {','.join(sorted(unsupported))}"
            )

    async def _resolve_target(
        self,
        job: Job,
        step: PublishStep,
        prior_effects: tuple[PublishEffect, ...],
    ):
        destination = await self._client.get_input_entity(job.destination)
        if step.target == PublishTarget.CHANNEL:
            return destination, None

        root_effects = tuple(
            effect
            for effect in prior_effects
            if effect.effect_type == "telegram_channel_message"
            and effect.external_message_id is not None
            and effect.external_chat_id is not None
        )
        if not root_effects:
            raise RuntimeError("discussion publish requires a confirmed channel root message")

        for effect in root_effects:
            discussion_chat_id = effect.detail.get("discussion_chat_id")
            discussion_message_id = effect.detail.get("discussion_message_id")
            if discussion_chat_id is not None and discussion_message_id is not None:
                group = await self._client.get_input_entity(int(discussion_chat_id))
                return group, int(discussion_message_id)

        if self._discussion_resolver is None:
            raise RuntimeError("discussion root resolver is unavailable for bot publishing")
        channel_chat_id = root_effects[0].external_chat_id
        message_ids = tuple(int(effect.external_message_id) for effect in root_effects)
        root = await self._discussion_resolver.resolve(channel_chat_id, message_ids)
        if root is None:
            raise RuntimeError("linked discussion automatic-forward root was not observed")
        group = await self._client.get_input_entity(root.chat_id)
        return group, root.message_id

    async def _finalize_receipts(
        self,
        job: Job,
        step: PublishStep,
        receipts: list[PublishReceipt],
    ) -> list[PublishReceipt]:
        if (
            self._discussion_resolver is None
            or step.target != PublishTarget.CHANNEL
            or step.kind
            not in {
                PublishStepKind.CHANNEL_COVER_ALBUM,
                PublishStepKind.CHANNEL_VIDEO_COVER,
            }
        ):
            return receipts
        channel_receipts = [
            receipt
            for receipt in receipts
            if receipt.external_chat_id is not None and receipt.external_message_id is not None
        ]
        if not channel_receipts:
            return receipts
        channel_chat_id = channel_receipts[0].external_chat_id
        message_ids = tuple(int(receipt.external_message_id) for receipt in channel_receipts)
        try:
            root = await self._discussion_resolver.resolve(channel_chat_id, message_ids)
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "publish.discussion_root.capture_failed",
                "Could not capture linked discussion root after channel send",
                job_id=job.id,
                exception_type=type(exc).__name__,
            )
            return receipts
        if root is None:
            # Expected race: the automatic forward may not be visible yet. The
            # discussion steps resolve the root again themselves; only a real
            # failure there raises. Keep this off the WARNING channel.
            log_event(
                self._log,
                logging.DEBUG,
                "publish.discussion_root.not_observed",
                "Linked discussion automatic-forward was not observed after channel send",
                job_id=job.id,
            )
            return receipts
        enriched: list[PublishReceipt] = []
        for receipt in receipts:
            detail = dict(receipt.detail)
            if (
                receipt.external_message_id is not None
                and int(receipt.external_message_id) == root.source_message_id
            ):
                detail.update(
                    {
                        "discussion_chat_id": root.chat_id,
                        "discussion_message_id": root.message_id,
                    }
                )
            enriched.append(
                PublishReceipt(
                    effect_type=receipt.effect_type,
                    external_chat_id=receipt.external_chat_id,
                    external_message_id=receipt.external_message_id,
                    detail=detail,
                )
            )
        return enriched

    @staticmethod
    def _step_items(job: Job, step: PublishStep) -> list[MediaItem]:
        by_index = {item.index: item for item in job.items}
        missing = [index for index in step.item_indexes if index not in by_index]
        if missing:
            raise KeyError(f"publish step references missing media indexes: {missing}")
        return [by_index[index] for index in step.item_indexes]

    @staticmethod
    def _local_path(item: MediaItem) -> Path:
        if not item.local_path:
            raise FileNotFoundError(f"media item {item.index} has no local_path")
        path = Path(item.local_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    @staticmethod
    def _strategy(step: PublishStep, item_index: int) -> str:
        strategies = step.params.get("strategies", {})
        return str(strategies.get(str(item_index), "native"))

    @staticmethod
    def _caption(item: MediaItem, step: PublishStep) -> str:
        header = TelethonPublishTransport._caption_header(item, step)
        text = item.caption if step.params.get("forward_caption", True) else ""
        collection_caption = ""
        if (
            step.params.get("collection_caption_item_index") == item.index
            and step.params.get("collection_caption")
        ):
            collection_caption = str(step.params.get("collection_caption") or "").strip()
        template = str(step.params.get("caption_template") or "").strip()
        body = "\n".join(
            part for part in (header, collection_caption, text or "", template) if part
        )
        return TelethonPublishTransport._with_footer(
            body,
            TelethonPublishTransport._footer(step),
        )

    @staticmethod
    def _caption_header(item: MediaItem, step: PublishStep) -> str:
        """The set header, only on the first media of a step."""

        if step.params.get("caption_header_item_index") != item.index:
            return ""
        return str(step.params.get("caption_header") or "").strip()

    @staticmethod
    def _footer(step: PublishStep) -> str:
        return str(step.params.get("caption_footer", "") or "").strip()

    @staticmethod
    def _with_footer(text: str, footer: str) -> str:
        text = text or ""
        footer = footer or ""
        if not footer:
            return text[:1024]
        limit = 1024 - len(footer) - 1
        if limit <= 0:
            return footer[:1024]
        if text:
            return f"{text[:limit]}\n{footer}"
        return footer[:1024]

    @staticmethod
    def _normalize_messages(sent) -> list:
        if sent is None:
            return []
        if isinstance(sent, (list, tuple)):
            return list(sent)
        return [sent]

    def _receipts(
        self,
        sent,
        step: PublishStep,
        items: Iterable[MediaItem],
    ) -> list[PublishReceipt]:
        messages = self._normalize_messages(sent)
        if not messages:
            raise PublishTransportUncertainError(
                "Telegram send returned no observable messages"
            )
        indexes = [item.index for item in items]
        receipts: list[PublishReceipt] = []
        for position, message in enumerate(messages):
            try:
                message_id = getattr(message, "id", None)
                if message_id is None:
                    raise ValueError("Telegram message id is unavailable")
                peer_id = getattr(message, "peer_id", None)
                external_chat_id = (
                    str(utils.get_peer_id(peer_id)) if peer_id is not None else None
                )
                item_index = indexes[position] if position < len(indexes) else None
                reusable_ref = None
                if (
                    external_chat_id is not None
                    and item_index is not None
                    and step.kind != PublishStepKind.CHANNEL_VIDEO_COVER
                ):
                    reusable_ref = f"telegram:{external_chat_id}:{message_id}"
                receipts.append(
                    PublishReceipt(
                        effect_type=(
                            "telegram_channel_message"
                            if step.target == PublishTarget.CHANNEL
                            else "telegram_discussion_message"
                        ),
                        external_chat_id=external_chat_id,
                        external_message_id=str(message_id),
                        detail={
                            "step_kind": step.kind.value,
                            "target": step.target.value,
                            "item_index": item_index,
                            "item_indexes": indexes,
                            "reusable_ref": reusable_ref,
                        },
                    )
                )
            except (PublishTransportPartialError, PublishTransportUncertainError):
                raise
            except Exception as exc:
                if receipts:
                    raise PublishTransportPartialError(
                        str(exc),
                        tuple(receipts),
                    ) from exc
                raise PublishTransportUncertainError(str(exc)) from exc
        return receipts
