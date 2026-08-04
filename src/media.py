"""独立媒体下载器与发布器。

两个独立功能模块，由队列编排层（src/bot.py 的 _Pipeline）调用。

扩展方式：
- 新增下载源：在 MediaDownloader._download 增加 kind 分支，或子类覆写。
- 新增发布目标/格式：在 MediaPublisher._publish 增加分支，或子类覆写。
- 前后处理（压缩/水印/通知/去重等）：订阅 pre/post 钩子即可，无需改核心。
"""

import asyncio
import logging
import os

from telethon import TelegramClient, functions
from telethon.tl import types
from telethon.utils import get_input_document, get_input_photo

from .downloader import download_video
from .video import guess_mime, is_photo_path, is_video_path, make_thumb, probe_video

logger = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(
            f"文件 {size / 1024 / 1024:.1f}MB 超过 {limit // (1024 * 1024)}MB 上限"
        )


class MediaDownloader:
    """独立下载器：消息 / 相册 / URL → 本地文件。"""

    def __init__(self, client: TelegramClient, workdir_fn, download_timeout: int):
        self.client = client
        self._workdir_fn = workdir_fn
        self.download_timeout = download_timeout
        self.pre_download_hooks = []   # async (job) -> None
        self.post_download_hooks = []  # async (job, paths) -> None
        self.progress_hooks = []       # async (seq, received, total, item, items) -> None

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
                path = await message.download_media(
                    file=workdir,
                    progress_callback=self._progress(job.seq, index, total),
                )
                if not path:
                    raise RuntimeError("未能下载相册媒体文件")
                paths.append(path)
            return paths
        if job.kind == "media":
            path = await job.message.download_media(
                file=workdir,
                progress_callback=self._progress(job.seq, 1, 1),
            )
        else:
            path, _ = await download_video(job.url, workdir)
        if not path:
            raise RuntimeError("未能下载媒体文件")
        return path

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
    ):
        self.client = client
        self.dest = dest
        self._workdir_fn = workdir_fn
        self.upload_timeout = upload_timeout
        self.max_file_size = max_file_size
        self.forward_caption = forward_caption
        self.pre_publish_hooks = []   # async (job, payload) -> None
        self.post_publish_hooks = []  # async (job, ids) -> None
        self.progress_hooks = []      # async (seq, received, total, item, items) -> None
        self._dest_input = None

    def _workdir(self, seq: int) -> str:
        return self._workdir_fn(seq)

    async def publish(self, job, payload) -> list:
        for hook in self.pre_publish_hooks:
            await hook(job, payload)
        ids = await self._publish(job, payload)
        for hook in self.post_publish_hooks:
            await hook(job, ids)
        return ids

    async def _publish(self, job, payload):
        if isinstance(payload, list):
            return await self._publish_album(job, payload)
        return [await self._publish_media(job, payload)]

    async def _publish_media(self, job, path: str) -> int:
        size = os.path.getsize(path)
        if size > self.max_file_size:
            raise FileTooLargeError(size, self.max_file_size)
        caption = None
        if self.forward_caption and job.kind == "media":
            caption = job.message.message[:1024] or None
        media = await self._upload_media_input(path, job.spoiler, job.seq)
        msg = await self.client.send_file(self.dest, media, caption=caption)
        return msg.id

    async def _publish_album(self, job, paths: list) -> list:
        for path in paths:
            size = os.path.getsize(path)
            if size > self.max_file_size:
                raise FileTooLargeError(size, self.max_file_size)
        dest_input = await self._get_dest_input()
        total = len(paths)
        if self.forward_caption:
            captions = [m.message[:1024] or "" for m in job.album]
        else:
            captions = [""] * total
        single_media = []
        for index, path in enumerate(paths):
            fm = await self._upload_media_input(
                path, job.spoiler, job.seq, item=index + 1, items=total
            )
            result = await self.client(
                functions.messages.UploadMediaRequest(dest_input, fm)
            )
            if isinstance(result, types.MessageMediaPhoto):
                reference = types.InputMediaPhoto(
                    id=get_input_photo(result.photo), spoiler=job.spoiler or None
                )
            elif isinstance(result, types.MessageMediaDocument):
                reference = types.InputMediaDocument(
                    id=get_input_document(result.document),
                    spoiler=job.spoiler or None,
                )
            else:
                raise RuntimeError(f"无法为相册媒体 #{(index + 1)} 构建引用")
            caption = captions[index] if index < len(captions) else ""
            single_media.append(types.InputSingleMedia(reference, message=caption))

        result = await self.client(
            functions.messages.SendMultiMediaRequest(
                dest_input, multi_media=single_media
            )
        )
        ids = []
        for update in getattr(result, "updates", []) or []:
            if isinstance(update, types.UpdateNewChannelMessage):
                ids.append(update.message.id)
            elif isinstance(update, types.UpdateNewMessage):
                ids.append(update.message.id)
        return ids

    async def _upload_media_input(self, path, spoiler, seq, item=1, items=1):
        progress = self._make_upload_progress(seq, item, items)
        uploaded = await self.client.upload_file(path, progress_callback=progress)
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
