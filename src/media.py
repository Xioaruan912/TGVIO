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
                path = await self._download_media_concurrent(
                    message, workdir, job.seq, index, total
                )
                if not path:
                    raise RuntimeError("未能下载相册媒体文件")
                paths.append(path)
            return paths
        if job.kind == "media":
            path = await self._download_media_concurrent(
                job.message, workdir, job.seq, 1, 1
            )
        else:
            path, _ = await download_video(job.url, workdir)
        if not path:
            raise RuntimeError("未能下载媒体文件")
        return path

    def _media_size(self, media) -> int:
        doc = getattr(media, "document", None)
        if doc:
            return doc.size
        photo = getattr(media, "photo", None)
        if photo and photo.sizes:
            return photo.sizes[-1].size
        raise RuntimeError("不支持的媒体类型")

    def _media_filename(self, media) -> str:
        doc = getattr(media, "document", None)
        if doc:
            for attr in doc.attributes:
                if isinstance(attr, types.DocumentAttributeFilename):
                    return attr.file_name
            ext = "bin"
            if doc.mime_type:
                ext = doc.mime_type.split("/")[-1]
            return f"media.{ext}"
        return "photo.jpg"

    async def _download_media_concurrent(
        self, message, workdir: str, seq: int, item: int, items: int
    ) -> str:
        """并发分片下载（iter_download + 多路 offset/stride），单文件接近带宽上限。"""
        media = message.media
        file_size = self._media_size(media)
        filename = self._media_filename(media)
        out = os.path.join(workdir, filename)
        request_size = int(self.part_size_kb * 1024)
        workers = self.download_workers
        stride = workers * request_size
        progress = self._progress(seq, item, items)
        received = 0

        with open(out, "wb") as f:
            f.truncate(file_size)

            async def consume(w: int) -> None:
                nonlocal received
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

            await asyncio.gather(*(consume(w) for w in range(workers)))

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
    ):
        self.client = client
        self.dest = dest
        self._workdir_fn = workdir_fn
        self.upload_timeout = upload_timeout
        self.max_file_size = max_file_size
        self.forward_caption = forward_caption
        self.upload_workers = max(1, upload_workers)
        self.part_size_kb = part_size_kb
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
