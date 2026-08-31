"""独立媒体下载器与发布器。

两个独立功能模块，由队列编排层（src/bot.py 的 _Pipeline）调用。

扩展方式：
- 新增下载源：在 MediaDownloader._download 增加 kind 分支，或子类覆写。
- 新增发布目标/格式：在 MediaPublisher._publish 增加分支，或子类覆写。
- 前后处理（压缩/水印/通知/去重等）：订阅 pre/post 钩子即可，无需改核心。
"""

import asyncio
import hashlib
import logging
import os

from telethon import TelegramClient, custom, functions, helpers
from telethon.tl import types
from telethon.utils import get_input_document, get_input_photo, get_peer_id

from .downloader import CancelToken, DownloadProgress, download_video
from .video import guess_mime, is_photo_path, is_video_path, make_cover, make_thumb, probe_video

logger = logging.getLogger(__name__)

SHARD_RETRIES = 3


class FileTooLargeError(Exception):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(
            f"文件 {size / 1024 / 1024:.1f}MB 超过 {limit // (1024 * 1024)}MB 上限"
        )


class PublishPartialError(Exception):
    """A visible publish request may have succeeded and must not be replayed blindly."""

    def __init__(self):
        super().__init__("partial publish requires manual review")


class MediaDownloader:
    """独立下载器：消息 / 相册 / URL → 本地文件。"""

    def __init__(
        self,
        client: TelegramClient,
        workdir_fn,
        download_timeout: int,
        download_workers: int = 8,
        part_size_kb: int = 512,
    ):
        self.client = client
        self._workdir_fn = workdir_fn
        self.download_timeout = download_timeout
        self.download_workers = max(1, download_workers)
        self.part_size_kb = part_size_kb
        self.shard_retries = SHARD_RETRIES
        self.pre_download_hooks = []   # async (job) -> None
        self.post_download_hooks = []  # async (job, paths) -> None
        self.progress_hooks = []       # async (seq, received, total, item, items) -> None
        self.status_hooks = []         # async (job, status) -> None

    def _workdir(self, seq: int) -> str:
        return self._workdir_fn(seq)

    async def run(self, job):
        for hook in self.pre_download_hooks:
            await hook(job)
        paths = await self._download(job)
        for hook in self.post_download_hooks:
            await hook(job, paths)
        return paths

    async def _download(self, job):
        cached = getattr(job, "cached_path", "") or ""
        if cached:
            logger.info("Job #%s using cached file %s", job.seq, cached)
            return cached
        workdir = self._workdir(job.seq)
        os.makedirs(workdir, exist_ok=True)
        if job.kind == "album":
            paths = []
            total = len(job.album)
            for index, message in enumerate(job.album, start=1):
                logger.info("Job #%s downloading album item %s", job.seq, message.id)
                path = await self._download_media_concurrent(
                    message, workdir, job.seq, index, total
                )
                if not path:
                    raise RuntimeError("未能下载相册媒体文件")
                paths.append(path)
            return paths
        if job.kind == "collection":
            paths = []
            total = len(job.album)
            for index, message in enumerate(job.album, start=1):
                logger.info(
                    "Job #%s downloading collection item %s (%d/%d)",
                    job.seq,
                    message.id,
                    index,
                    total,
                )
                path = await self._download_media_concurrent(
                    message, workdir, job.seq, index, total
                )
                if not path:
                    raise RuntimeError("未能下载合集媒体文件")
                paths.append(path)
            return paths
        if job.kind == "media":
            path = await self._download_media_concurrent(
                job.message, workdir, job.seq, 1, 1
            )
        else:
            cancel_token = CancelToken()
            job.url_cancel_token = cancel_token
            job.url_stage = "download"

            def on_url_progress(progress: DownloadProgress) -> None:
                if progress.status == "postprocessing":
                    job.url_stage = "postprocessing"
                    for hook in self.status_hooks:
                        asyncio.create_task(hook(job, progress.status))
                    return
                job.url_stage = "download"
                for hook in self.progress_hooks:
                    asyncio.create_task(
                        hook(
                            job.seq,
                            progress.downloaded_bytes,
                            progress.total_bytes or 0,
                            progress.item_index,
                            progress.item_total,
                        )
                    )

            try:
                path, _ = await download_video(
                    job.url,
                    workdir,
                    on_progress=on_url_progress,
                    cancel_token=cancel_token,
                )
            finally:
                job.url_cancel_token = None
        if not path:
            raise RuntimeError("未能下载媒体文件")
        return path

    def _media_size(self, media) -> int:
        doc = getattr(media, "document", None)
        if doc:
            return doc.size
        photo = getattr(media, "photo", None)
        if photo and photo.sizes:
            total = 0
            for sz in photo.sizes:
                if isinstance(sz, types.PhotoSizeProgressive):
                    if sz.sizes:
                        total = max(total, sz.sizes[-1])
                else:
                    total = max(total, getattr(sz, "size", 0) or 0)
            if total:
                return total
        raise RuntimeError("不支持的媒体类型")

    def _media_filename(self, media, item: int = 1) -> str:
        doc = getattr(media, "document", None)
        if doc:
            for attr in doc.attributes:
                if isinstance(attr, types.DocumentAttributeFilename):
                    return attr.file_name
            ext = "bin"
            if doc.mime_type:
                ext = doc.mime_type.split("/")[-1]
            return f"media.{ext}"
        return f"photo_{item}.jpg"

    async def _download_media_concurrent(
        self, message, workdir: str, seq: int, item: int, items: int
    ) -> str:
        """并发分片下载（iter_download + 多路 offset/stride），单文件接近带宽上限。

        单分片容错：某一路分片流失败时重建该流重试（SHARD_RETRIES 次），
        不因一路抖动报废整个文件。
        """
        media = message.media
        file_size = self._media_size(media)
        filename = self._media_filename(media, item)
        out = os.path.join(workdir, filename)
        if os.path.exists(out):
            stem, ext = os.path.splitext(filename)
            out = os.path.join(workdir, f"{stem}_{item}{ext}")
        request_size = int(self.part_size_kb * 1024)
        workers = self.download_workers
        stride = workers * request_size
        progress = self._progress(seq, item, items)
        received = 0

        with open(out, "wb") as f:
            f.truncate(file_size)

            async def consume(w: int):
                nonlocal received
                last_exc = None
                for attempt in range(self.shard_retries + 1):
                    try:
                        k = 0
                        it = self.client.iter_download(
                            media,
                            offset=w * request_size,
                            stride=stride,
                            request_size=request_size,
                            file_size=file_size,
                        )
                        async for chunk in it:
                            f.seek(w * request_size + k * stride)
                            f.write(chunk)
                            k += 1
                            received += len(chunk)
                            await progress(received, file_size)
                        return
                    except Exception as exc:
                        last_exc = exc
                        logger.warning(
                            "Job #%s shard #%s failed (%s), retry %d/%d",
                            seq,
                            w,
                            exc.__class__.__name__,
                            attempt + 1,
                            self.shard_retries,
                        )
                        await asyncio.sleep(1)
                raise last_exc

            results = await asyncio.gather(
                *(consume(w) for w in range(workers)), return_exceptions=True
            )
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]

        logger.info("Job #%s downloaded concurrently %d bytes", seq, file_size)
        return out

    def _progress(self, seq: int, item: int, items: int):
        last = {"pct": -1}

        async def progress(received: int, total: int) -> None:
            pct = int(received * 100 / total) if total else 0
            if pct == last["pct"]:
                return
            last["pct"] = pct
            for hook in self.progress_hooks:
                await hook(seq, received, total, item, items)

        return progress


class MediaPublisher:
    """独立发布器：本地文件 → 频道消息，返回发布的消息 id 列表。"""

    def __init__(
        self,
        client: TelegramClient,
        dest,
        workdir_fn,
        upload_timeout: int,
        max_file_size: int,
        forward_caption: bool,
        upload_workers: int = 16,
        part_size_kb: int = 512,
        cover_mode: bool = False,
        cover_width: int = 1280,
        max_cover_images: int = 10,
        group_counter_file: str = "",
        channel_at: str = "",
        group_at: str = "",
    ):
        self.client = client
        self.dest = dest
        self._workdir_fn = workdir_fn
        self.upload_timeout = upload_timeout
        self.max_file_size = max_file_size
        self.forward_caption = forward_caption
        self.upload_workers = max(1, upload_workers)
        self.part_size_kb = part_size_kb
        self.cover_mode = cover_mode
        self.cover_width = cover_width
        self.max_cover_images = max(1, max_cover_images)
        self.pre_publish_hooks = []   # async (job, payload) -> None
        self.post_publish_hooks = []  # async (job, ids) -> None
        self.checkpoint_hooks = []    # async (job, canonical_refs) -> None
        self.progress_hooks = []      # async (seq, received, total, item, items) -> None
        self.dedup_manager = None
        self._dest_input = None
        self._group_input = None
        self._group_id = None
        self.group_counter_file = group_counter_file
        self._group_max_id = self._load_group_max()
        self._thread_root = None
        self._caption_footer = " ".join(
            x for x in (channel_at, group_at) if x
        ).strip()

    def _with_footer(self, text: str) -> str:
        """caption 末尾自动追加 @频道 @群组（footer），保证不超 1024 字符。"""
        text = text or ""
        if not self._caption_footer:
            return text[:1024]
        limit = 1024 - len(self._caption_footer) - 1
        if limit <= 0:
            return self._caption_footer[:1024]
        if text:
            return f"{text[:limit]}\n{self._caption_footer}"
        return self._caption_footer

    def _workdir(self, seq: int) -> str:
        return self._workdir_fn(seq)

    async def publish(self, job, payload) -> list:
        job._publish_send_attempts = 0
        if not hasattr(job, "_published_refs"):
            job._published_refs = []
        for hook in self.pre_publish_hooks:
            await hook(job, payload)
        ids = await self._publish(job, payload)
        for hook in self.post_publish_hooks:
            await hook(job, ids)
        return ids

    @staticmethod
    def _peer_id(peer) -> int:
        try:
            return int(get_peer_id(peer))
        except Exception:
            try:
                return int(peer)
            except (TypeError, ValueError):
                return 0

    @staticmethod
    def _begin_send(job) -> None:
        job._publish_send_attempts = int(getattr(job, "_publish_send_attempts", 0)) + 1

    async def _checkpoint(self, job, peer, message_ids, role: str) -> None:
        peer_id = self._peer_id(peer)
        known = list(getattr(job, "_published_refs", []))
        fresh = []
        for message_id in message_ids:
            ref = (peer_id, int(message_id), str(role))
            if ref not in known:
                known.append(ref)
                fresh.append(ref)
        job._published_refs = known
        if fresh:
            for hook in self.checkpoint_hooks:
                await hook(job, fresh)

    @staticmethod
    def _raise_partial_if_started(job, attempts_before: int, exc: BaseException) -> None:
        if int(getattr(job, "_publish_send_attempts", 0)) > attempts_before:
            raise PublishPartialError() from exc

    async def _publish(self, job, payload):
        if job.kind == "collection":
            return await self._publish_collection(job, payload)
        if isinstance(payload, list):
            return await self._publish_album(job, payload)
        return await self._publish_media(job, payload)

    async def _publish_media(self, job, path) -> list:
        size = os.path.getsize(path)
        if size > self.max_file_size:
            raise FileTooLargeError(size, self.max_file_size)
        caption = None
        if self.forward_caption and job.kind == "media":
            caption = job.message.message[:1024] or None
        caption = self._with_footer(caption) or None
        media = await self._media_input(job, path, job.spoiler, job.seq)
        if self.cover_mode and is_video_path(path):
            attempts_before = int(getattr(job, "_publish_send_attempts", 0))
            try:
                return await self._publish_cover_video(job, path, media, caption)
            except Exception as exc:
                self._raise_partial_if_started(job, attempts_before, exc)
                logger.warning(
                    "Cover publish failed (%s), fallback direct publish", exc
                )
        dest_input = await self._get_dest_input()
        self._begin_send(job)
        msg = await self.client.send_file(self.dest, media, caption=caption)
        await self._record_dedup_message(job, path, msg)
        await self._checkpoint(
            job, getattr(msg, "peer_id", None) or dest_input, [msg.id], "destination"
        )
        return [msg.id]

    async def _publish_cover_video(self, job, video_path, media, caption) -> list:
        workdir = self._workdir(job.seq)
        os.makedirs(workdir, exist_ok=True)
        cover = await make_cover(video_path, workdir, self.cover_width)
        cover_media = await self._upload_media_input(cover, False, job.seq)
        dest_input = await self._get_dest_input()
        self._begin_send(job)
        cover_msg = await self.client.send_file(self.dest, cover_media, caption=caption)
        await self._checkpoint(
            job, getattr(cover_msg, "peer_id", None) or dest_input, [cover_msg.id], "cover"
        )
        group_peer, comment_id = await self._post_comment(job, media, cover_msg)
        await self._record_dedup_ref(job, video_path, group_peer, comment_id)
        await self._checkpoint(job, group_peer, [comment_id], "comment")
        return [(dest_input, cover_msg.id), (group_peer, comment_id)]

    async def _get_discussion_group(self):
        if self._group_input is not None:
            return self._group_input
        dest_input = await self._get_dest_input()
        full = await self.client(
            functions.channels.GetFullChannelRequest(channel=dest_input)
        )
        lid = full.full_chat.linked_chat_id
        if not lid:
            raise RuntimeError("频道未关联讨论群组，无法发布评论")
        chat = await self.client.get_entity(types.PeerChannel(channel_id=lid))
        self._group_input = await self.client.get_input_entity(chat)
        self._group_id = lid
        return self._group_input

    async def _scan_group(self, lo: int, hi: int) -> list:
        if self._group_id is None:
            await self._get_discussion_group()
        found = []
        for start in range(lo, hi, 100):
            ids = list(range(start, min(start + 100, hi)))
            if not ids:
                break
            res = await self.client(
                functions.channels.GetMessagesRequest(
                    channel=self._group_id,
                    id=[types.InputMessageID(id=i) for i in ids],
                )
            )
            found.extend(
                m for m in res.messages if not isinstance(m, types.MessageEmpty)
            )
        return found

    def _load_group_max(self):
        if not self.group_counter_file:
            return None
        try:
            with open(self.group_counter_file) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _persist_group_max(self) -> None:
        if not self.group_counter_file or self._group_max_id is None:
            return
        try:
            os.makedirs(os.path.dirname(self.group_counter_file), exist_ok=True)
            with open(self.group_counter_file, "w") as f:
                f.write(str(self._group_max_id))
        except Exception as ex:
            logger.warning("无法持久化群组计数: %s", ex)

    def _note_group_id(self, mid) -> None:
        if mid is not None and (
            self._group_max_id is None or mid > self._group_max_id
        ):
            self._group_max_id = mid
            self._persist_group_max()

    async def _find_thread_root(self, cover_id: int):
        """定位群组线程根（频道帖镜像）消息 id，找不到返回 None。

        群组消息被清空后消息 id 计数器不回退，因此用「最高已见 id」做
        下界扫描窗口（持久化到 group_counter_file，跨重启保留）。
        """
        await self._get_discussion_group()
        if self._thread_root and self._thread_root[0] == cover_id:
            return self._thread_root[1]
        if self._group_max_id is None:
            msgs = await self._scan_group(1, 501)
            if msgs:
                self._note_group_id(max(m.id for m in msgs))
        await asyncio.sleep(1.0)
        base = self._group_max_id or 1
        root = None
        for attempt in range(6):
            lo = max(1, base - 10)
            hi = base + 400 * (attempt + 1)
            msgs = await self._scan_group(lo, hi)
            for m in msgs:
                self._note_group_id(m.id)
                cp = getattr(getattr(m, "fwd_from", None), "channel_post", None)
                if cp == cover_id:
                    root = m.id
                    break
            if root is not None:
                break
            await asyncio.sleep(1.5)
        if root is None:
            logger.warning("Cover #%s 未找到讨论组线程根，评论将回退为群组直发", cover_id)
        self._thread_root = (cover_id, root)
        return root

    async def _post_comment(self, job, media, channel_msg):
        group = await self._get_discussion_group()
        mid = getattr(channel_msg, "id", channel_msg)
        root = await self._find_thread_root(mid)
        reply_to = None
        if root is not None:
            reply_to = types.InputReplyToMessage(reply_to_msg_id=root)
        self._begin_send(job)
        result = await self.client(
            functions.messages.SendMediaRequest(
                peer=group,
                media=media,
                message="",
                reply_to=reply_to,
                random_id=helpers.generate_random_long(),
            )
        )
        comment_id = None
        for update in getattr(result, "updates", []) or []:
            if isinstance(update, types.UpdateNewMessage):
                comment_id = update.message.id
            elif isinstance(update, types.UpdateNewChannelMessage):
                comment_id = update.message.id
        if comment_id is None:
            raise RuntimeError("评论消息发送后未取到 id")
        self._note_group_id(comment_id)
        await self._checkpoint(job, group, [comment_id], "comment")
        return group, comment_id

    async def _post_album_comment(
        self, job, paths, root_msg, spoiler, seq, caption="", item_offset=0, total_items=None
    ) -> tuple:
        group = await self._get_discussion_group()
        root_id = getattr(root_msg, "id", root_msg)
        thread_root = await self._find_thread_root(root_id)
        reply_to = None
        if thread_root is not None:
            reply_to = types.InputReplyToMessage(reply_to_msg_id=thread_root)
        single_media = []
        total = total_items or len(paths)
        for index, path in enumerate(paths):
            fm = await self._media_input(
                job, path, spoiler, seq, item=item_offset + index + 1, items=total
            )
            result = await self.client(
                functions.messages.UploadMediaRequest(group, fm)
            )
            if isinstance(result, types.MessageMediaPhoto):
                reference = types.InputMediaPhoto(
                    id=get_input_photo(result.photo), spoiler=spoiler or None
                )
            elif isinstance(result, types.MessageMediaDocument):
                reference = types.InputMediaDocument(
                    id=get_input_document(result.document),
                    spoiler=spoiler or None,
                )
            else:
                raise RuntimeError(f"无法为评论相册媒体 #{(index + 1)} 构建引用")
            msg_text = caption if index == 0 else ""
            single_media.append(types.InputSingleMedia(reference, message=msg_text))
        self._begin_send(job)
        result = await self.client(
            functions.messages.SendMultiMediaRequest(
                group, multi_media=single_media, reply_to=reply_to
            )
        )
        ids = []
        sent_messages = []
        for update in getattr(result, "updates", []) or []:
            if isinstance(update, types.UpdateNewChannelMessage):
                ids.append(update.message.id)
                sent_messages.append(update.message)
            elif isinstance(update, types.UpdateNewMessage):
                ids.append(update.message.id)
                sent_messages.append(update.message)
        if not ids:
            raise RuntimeError("评论相册发送后未取到 id")
        for cid in ids:
            self._note_group_id(cid)
        for path, message in zip(paths, sent_messages):
            await self._record_dedup_message(job, path, message)
        await self._checkpoint(job, group, ids, "comment")
        return group, ids

    def _album_captions(self, job, paths) -> list:
        if not self.forward_caption:
            return [""] * len(paths)
        album_msgs = job.album or []
        caps = []
        for index, _path in enumerate(paths):
            m = album_msgs[index] if index < len(album_msgs) else None
            caps.append((m.message[:1024] or "") if m is not None else "")
        return caps

    async def _publish_album(self, job, paths: list) -> list:
        for path in paths:
            size = os.path.getsize(path)
            if size > self.max_file_size:
                raise FileTooLargeError(size, self.max_file_size)
        dest_input = await self._get_dest_input()
        captions = self._album_captions(job, paths)
        photo_idx = [i for i, p in enumerate(paths) if is_photo_path(p)]
        video_idx = [i for i, p in enumerate(paths) if not is_photo_path(p)]

        if self.cover_mode and len(photo_idx) > self.max_cover_images:
            dropped = len(photo_idx) - self.max_cover_images
            photo_idx = photo_idx[: self.max_cover_images]
            logger.info(
                "封面相册超过 %s 张，丢弃多余 %s 张图片",
                self.max_cover_images,
                dropped,
            )

        if not self.cover_mode or not video_idx:
            limited = [paths[i] for i in photo_idx] if self.cover_mode else paths
            return await self._send_album_media(job, limited, dest_input, job.spoiler)

        refs = []
        if photo_idx:
            photo_paths = [paths[i] for i in photo_idx]
            photo_caps = [captions[i] for i in photo_idx]
            cover_ids = await self._send_album_media(
                job, photo_paths, dest_input, None, forced_captions=photo_caps,
                role="cover",
            )
            refs.extend((dest_input, mid) for mid in cover_ids)
            root_msg = cover_ids[0]
        else:
            workdir = self._workdir(job.seq)
            os.makedirs(workdir, exist_ok=True)
            first = paths[video_idx[0]]
            cover = await make_cover(first, workdir, self.cover_width)
            cover_media = await self._upload_media_input(cover, False, job.seq)
            self._begin_send(job)
            cover_msg = await self.client.send_file(
                self.dest,
                cover_media,
                caption=self._with_footer(captions[video_idx[0]]) or None,
            )
            await self._checkpoint(
                job, getattr(cover_msg, "peer_id", None) or dest_input,
                [cover_msg.id], "cover",
            )
            refs.append((dest_input, cover_msg.id))
            root_msg = cover_msg.id

        video_paths = [paths[i] for i in video_idx]
        first_chunk = True
        for start in range(0, len(video_paths), 10):
            chunk = video_paths[start : start + 10]
            caption = ""
            if first_chunk and len(chunk) > 1:
                caption = f"合集共 {len(video_paths)} 个视频"
            attempts_before = int(getattr(job, "_publish_send_attempts", 0))
            try:
                if len(chunk) == 1:
                    media = await self._media_input(
                        job, chunk[0], job.spoiler, job.seq,
                        item=start + 1, items=len(video_paths),
                    )
                    group_peer, comment_id = await self._post_comment(job, media, root_msg)
                    await self._record_dedup_ref(job, chunk[0], group_peer, comment_id)
                    await self._checkpoint(job, group_peer, [comment_id], "comment")
                    refs.append((group_peer, comment_id))
                else:
                    group_peer, cids = await self._post_album_comment(
                        job, chunk, root_msg, job.spoiler, job.seq, caption=caption,
                        item_offset=start, total_items=len(video_paths),
                    )
                    await self._checkpoint(job, group_peer, cids, "comment")
                    refs.extend((group_peer, cid) for cid in cids)
            except Exception as exc:
                self._raise_partial_if_started(job, attempts_before, exc)
                logger.warning(
                    "Album comment publish failed (%s), fallback direct", exc
                )
                for index, _p in enumerate(chunk):
                    media = await self._media_input(
                        job, _p, job.spoiler, job.seq,
                        item=start + index + 1, items=len(video_paths),
                    )
                    self._begin_send(job)
                    msg = await self.client.send_file(self.dest, media)
                    await self._record_dedup_message(job, _p, msg)
                    await self._checkpoint(
                        job, getattr(msg, "peer_id", None) or dest_input,
                        [msg.id], "fallback",
                    )
                    refs.append((dest_input, msg.id))
            first_chunk = False
        return refs

    @staticmethod
    def _join_collection_texts(job) -> str:
        """会话期间的文字评论：每次发送的内容按行拆分（自动补换行），整合为一条文本。

        作为封面 caption 与封面一起发送（非封面模式不使用）。
        """
        texts = getattr(job, "texts", None) or []
        if not texts:
            return ""
        lines = []
        for t in texts:
            for line in t.splitlines():
                line = line.strip()
                if line:
                    lines.append(line)
        return "\n".join(lines)[:1024]

    @staticmethod
    def _merge_caption(comment: str, original: str) -> str:
        if not comment:
            return original
        if original:
            return comment + "\n" + original
        return comment

    async def _publish_collection(self, job, paths: list) -> list:
        """合集发布：整个会话整合为「1 个封面 + 1 个评论区」。

        - 图片（按序取前 MAX_COVER_IMAGES 张）→ 频道封面相册，超出按序丢弃
        - 全部视频 → 按 10 条一组媒体组，进同一个讨论组评论线程
        - 纯图片合集 → 只发封面相册；纯视频合集 → 首视频截帧做封面
        - 会话期间收集的文字评论（job.texts）按行整合为封面 caption
        """
        for path in paths:
            size = os.path.getsize(path)
            if size > self.max_file_size:
                raise FileTooLargeError(size, self.max_file_size)
        dest_input = await self._get_dest_input()
        captions = self._album_captions(job, paths)
        comment = self._join_collection_texts(job)
        photo_idx = [i for i, p in enumerate(paths) if is_photo_path(p)]
        video_idx = [i for i, p in enumerate(paths) if not is_photo_path(p)]

        if not self.cover_mode:
            if comment:
                logger.info("非封面模式忽略 %d 条会话评论", len(getattr(job, "texts", []) or []))
            return await self._publish_ordered(job, paths, dest_input, captions)

        refs = []
        root_msg = None
        if photo_idx:
            dropped = len(photo_idx) - self.max_cover_images
            if dropped > 0:
                logger.info(
                    "合集图片超过 %s 张，按序丢弃 %s 张",
                    self.max_cover_images,
                    dropped,
                )
            photo_idx = photo_idx[: self.max_cover_images]
            photo_paths = [paths[i] for i in photo_idx]
            photo_caps = [captions[i] for i in photo_idx]
            if comment:
                photo_caps[0] = self._merge_caption(comment, photo_caps[0])
            cover_ids = await self._send_album_media(
                job, photo_paths, dest_input, None, forced_captions=photo_caps,
                role="cover",
            )
            refs.extend((dest_input, mid) for mid in cover_ids)
            root_msg = cover_ids[0]
        elif video_idx:
            workdir = self._workdir(job.seq)
            os.makedirs(workdir, exist_ok=True)
            first = paths[video_idx[0]]
            cover = await make_cover(first, workdir, self.cover_width)
            cover_media = await self._upload_media_input(cover, False, job.seq)
            cover_caption = self._merge_caption(comment, captions[video_idx[0]])
            self._begin_send(job)
            cover_msg = await self.client.send_file(
                self.dest,
                cover_media,
                caption=self._with_footer(cover_caption) or None,
            )
            await self._checkpoint(
                job, getattr(cover_msg, "peer_id", None) or dest_input,
                [cover_msg.id], "cover",
            )
            refs.append((dest_input, cover_msg.id))
            root_msg = cover_msg.id

        if video_idx:
            video_paths = [paths[i] for i in video_idx]
            first_chunk = True
            for start in range(0, len(video_paths), 10):
                chunk = video_paths[start : start + 10]
                caption = ""
                if first_chunk and len(chunk) > 1:
                    caption = f"合集共 {len(video_paths)} 个视频"
                attempts_before = int(getattr(job, "_publish_send_attempts", 0))
                try:
                    if len(chunk) == 1:
                        media = await self._media_input(
                            job, chunk[0], job.spoiler, job.seq,
                            item=start + 1, items=len(video_paths),
                        )
                        group_peer, comment_id = await self._post_comment(job, media, root_msg)
                        await self._record_dedup_ref(job, chunk[0], group_peer, comment_id)
                        await self._checkpoint(job, group_peer, [comment_id], "comment")
                        refs.append((group_peer, comment_id))
                    else:
                        group_peer, cids = await self._post_album_comment(
                            job, chunk, root_msg, job.spoiler, job.seq, caption=caption,
                            item_offset=start, total_items=len(video_paths),
                        )
                        await self._checkpoint(job, group_peer, cids, "comment")
                        refs.extend((group_peer, cid) for cid in cids)
                except Exception as exc:
                    self._raise_partial_if_started(job, attempts_before, exc)
                    logger.warning(
                        "Collection comment publish failed (%s), fallback direct", exc
                    )
                    for index, _p in enumerate(chunk):
                        media = await self._media_input(
                            job, _p, job.spoiler, job.seq,
                            item=start + index + 1, items=len(video_paths),
                        )
                        self._begin_send(job)
                        msg = await self.client.send_file(self.dest, media)
                        await self._record_dedup_message(job, _p, msg)
                        await self._checkpoint(
                            job, getattr(msg, "peer_id", None) or dest_input,
                            [msg.id], "fallback",
                        )
                        refs.append((dest_input, msg.id))
                first_chunk = False
        return refs

    async def _publish_ordered(self, job, paths: list, dest_input, captions: list) -> list:
        """非封面模式合集发布：按到达顺序发到频道（连续图片 10 张一组相册，视频单发）。"""
        refs = []
        total = len(paths)
        i = 0
        while i < total:
            if is_video_path(paths[i]):
                media = await self._media_input(
                    job, paths[i], job.spoiler, job.seq, item=i + 1, items=total
                )
                self._begin_send(job)
                msg = await self.client.send_file(
                    self.dest, media, caption=self._with_footer(captions[i]) or None
                )
                await self._record_dedup_message(job, paths[i], msg)
                await self._checkpoint(
                    job, getattr(msg, "peer_id", None) or dest_input,
                    [msg.id], "destination",
                )
                refs.append(msg.id)
                i += 1
            else:
                chunk_paths = []
                chunk_caps = []
                while i < total and not is_video_path(paths[i]) and len(chunk_paths) < 10:
                    chunk_paths.append(paths[i])
                    chunk_caps.append(captions[i])
                    i += 1
                ids = await self._send_album_media(
                    job, chunk_paths, dest_input, job.spoiler,
                    forced_captions=chunk_caps,
                )
                refs.extend(ids)
        return refs

    async def _send_album_media(
        self,
        job,
        paths,
        dest_input,
        spoiler,
        forced_captions=None,
        role="destination",
    ) -> list:
        total = len(paths)
        if forced_captions is not None:
            captions = list(forced_captions)
        elif self.forward_caption:
            captions = [m.message[:1024] or "" for m in (job.album or [])]
        else:
            captions = [""] * total
        while len(captions) < total:
            captions.append("")
        ids = []
        for start in range(0, total, 10):
            chunk = paths[start : start + 10]
            single_media = []
            for index, path in enumerate(chunk):
                item_index = start + index
                fm = await self._media_input(
                    job, path, spoiler, job.seq, item=item_index + 1, items=total
                )
                result = await self.client(
                    functions.messages.UploadMediaRequest(dest_input, fm)
                )
                if isinstance(result, types.MessageMediaPhoto):
                    reference = types.InputMediaPhoto(
                        id=get_input_photo(result.photo), spoiler=spoiler or None
                    )
                elif isinstance(result, types.MessageMediaDocument):
                    reference = types.InputMediaDocument(
                        id=get_input_document(result.document),
                        spoiler=spoiler or None,
                    )
                else:
                    raise RuntimeError(f"无法为相册媒体 #{(item_index + 1)} 构建引用")
                caption = captions[item_index] if item_index < len(captions) else ""
                single_media.append(
                    types.InputSingleMedia(reference, message=self._with_footer(caption))
                )
            self._begin_send(job)
            result = await self.client(
                functions.messages.SendMultiMediaRequest(
                    dest_input, multi_media=single_media
                )
            )
            chunk_ids = []
            chunk_messages = []
            for update in getattr(result, "updates", []) or []:
                if isinstance(update, types.UpdateNewChannelMessage):
                    chunk_ids.append(update.message.id)
                    chunk_messages.append(update.message)
                elif isinstance(update, types.UpdateNewMessage):
                    chunk_ids.append(update.message.id)
                    chunk_messages.append(update.message)
            if not chunk_ids:
                raise PublishPartialError()
            ids.extend(chunk_ids)
            for path, message in zip(chunk, chunk_messages):
                await self._record_dedup_message(job, path, message)
            await self._checkpoint(job, dest_input, chunk_ids, role)
        return ids

    async def _media_input(self, job, path, spoiler, seq, item=1, items=1):
        manager = self.dedup_manager
        content = getattr(job, "_content_hashes", {}).get(os.path.realpath(path))
        if manager is not None and content is not None:
            try:
                media, entry = await manager.reuse_input_media(
                    self.client, content, spoiler=bool(spoiler)
                )
                if media is not None and entry is not None:
                    pending = getattr(job, "_dedup_reused", {})
                    pending[os.path.realpath(path)] = (entry, content)
                    job._dedup_reused = pending
                    logger.info("Job #%s D1 media reuse hit (%d bytes)", seq, content.size_bytes)
                    return media
            except Exception as exc:
                logger.warning("Job #%s D1 reuse fallback: %s", seq, exc.__class__.__name__)
        return await self._upload_media_input(path, spoiler, seq, item=item, items=items)

    async def _record_dedup_message(self, job, path, message) -> None:
        manager = self.dedup_manager
        content = getattr(job, "_content_hashes", {}).get(os.path.realpath(path))
        if manager is None or content is None:
            return
        try:
            reused = getattr(job, "_dedup_reused", {}).pop(os.path.realpath(path), None)
            await manager.record_sent_message(content, message)
            if reused is not None:
                await manager.mark_hit(reused[0], content)
        except Exception as exc:
            logger.warning("Job #%s D1 index update skipped: %s", job.seq, exc.__class__.__name__)

    async def _record_dedup_ref(self, job, path, peer, message_id: int) -> None:
        manager = self.dedup_manager
        content = getattr(job, "_content_hashes", {}).get(os.path.realpath(path))
        if manager is None or content is None:
            return
        try:
            message = await self.client.get_messages(peer, ids=int(message_id))
            await self._record_dedup_message(job, path, message)
        except Exception as exc:
            logger.warning("Job #%s D1 ref refresh skipped: %s", job.seq, exc.__class__.__name__)

    async def _upload_media_input(self, path, spoiler, seq, item=1, items=1):
        uploaded = await self._upload_concurrent(path, seq, item, items)
        if is_photo_path(path):
            return types.InputMediaUploadedPhoto(file=uploaded, spoiler=spoiler or None)

        os.makedirs(self._workdir(seq), exist_ok=True)
        attributes = [
            types.DocumentAttributeFilename(file_name=os.path.basename(path))
        ]
        mime = guess_mime(path)
        thumb_input = None
        nosound = None
        if is_video_path(path):
            duration, width, height = 0, 1, 1
            try:
                duration, width, height = await probe_video(path)
            except Exception as exc:
                logger.warning("Video probe failed for job #%s: %s", seq, exc)
            try:
                thumb = await make_thumb(path, self._workdir(seq))
                if thumb:
                    thumb_input = await self.client.upload_file(thumb)
            except Exception as exc:
                logger.warning("Video thumb failed for job #%s: %s", seq, exc)
            attributes.insert(
                0,
                types.DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                ),
            )
            nosound = True
        return types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime,
            attributes=attributes,
            thumb=thumb_input,
            spoiler=spoiler or None,
            nosound_video=nosound,
        )

    async def _upload_concurrent(self, path, seq, item, items):
        """并发分片上传（saveFilePart 按索引无序并发），单文件接近带宽上限。"""
        file_size = os.path.getsize(path)
        part_size = int(self.part_size_kb * 1024)
        is_big = file_size > 10 * 1024 * 1024
        part_count = (file_size + part_size - 1) // part_size or 1
        file_id = helpers.generate_random_long()
        file_name = os.path.basename(path)
        progress = self._make_upload_progress(seq, item, items)

        md5 = None
        if not is_big:
            md5 = hashlib.md5()
            with open(path, "rb") as f:
                while True:
                    part = f.read(part_size)
                    if not part:
                        break
                    md5.update(part)

        sem = asyncio.Semaphore(self.upload_workers)
        received = 0

        async def send_part(index: int) -> None:
            nonlocal received
            async with sem:
                with open(path, "rb") as f:
                    f.seek(index * part_size)
                    part = f.read(part_size)
                if is_big:
                    request = functions.upload.SaveBigFilePartRequest(
                        file_id, index, part_count, part
                    )
                else:
                    request = functions.upload.SaveFilePartRequest(
                        file_id, index, part
                    )
                ok = await self.client(request)
                if not ok:
                    raise RuntimeError(f"上传分片 {index} 失败")
                received += len(part)
                await progress(received, file_size)

        await asyncio.gather(*(send_part(i) for i in range(part_count)))

        logger.info("Job #%s uploaded concurrently %d bytes", seq, file_size)
        if is_big:
            return types.InputFileBig(file_id, part_count, file_name)
        return custom.InputSizedFile(
            file_id, part_count, file_name, md5=md5, size=file_size
        )

    def _make_upload_progress(self, seq, item, items):
        last = {"pct": -1}

        async def progress(received, total):
            pct = int(received * 100 / total) if total else 0
            if pct == last["pct"]:
                return
            last["pct"] = pct
            for hook in self.progress_hooks:
                await hook(seq, received, total, item, items)

        return progress

    async def _get_dest_input(self):
        if self._dest_input is None:
            self._dest_input = await self.client.get_input_entity(self.dest)
        return self._dest_input
