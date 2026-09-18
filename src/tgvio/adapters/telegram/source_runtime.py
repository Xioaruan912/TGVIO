"""In-Bot source setup: personal-account login, whitelist and reader lifecycle.

Everything the operator needs is driven from the TGVIO chat so no VPS shell is
required. The personal session, its whitelist and the reader are owned here;
the intake runtime only receives the ready reader through a callback.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from tgvio.config import Settings
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.adapters.telegram.user_source import UserSourceReader

_WHITELIST_FLAG = "source_chats"
_MAX_CHATS = 50

ReaderReadyHook = Callable[[object, UserSourceReader, int], None]
ReaderStoppedHook = Callable[[], None]
TriggerMediaHook = Callable[[int, list], Awaitable[None]]
NoticeHook = Callable[[str], Awaitable[None]]


class SourceLoginError(RuntimeError):
    pass


class SourceCoordinator:
    def __init__(
        self,
        settings: Settings,
        repository,
        flags: RuntimeFlags,
        session_path: Path,
        *,
        on_reader_ready: ReaderReadyHook | None = None,
        on_reader_stopped: ReaderStoppedHook | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._flags = flags
        self._session_path = Path(session_path)
        self._on_reader_ready = on_reader_ready
        self._on_reader_stopped = on_reader_stopped
        self._client: TelegramClient | None = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._user_id: int | None = None
        self._awaiting: str | None = None  # none | phone | code | password
        self._reader: UserSourceReader | None = None
        self._trigger_seen: dict[int, int] = {}
        self._poll_task: asyncio.Task | None = None
        self._on_trigger_media: TriggerMediaHook | None = None
        self._on_notice: NoticeHook | None = None
        self._poll_seconds = max(5, int(getattr(settings, "source_poll_seconds", 15)))
        self._log = logging.getLogger("tgvio.telegram.source")

    # ---------------------------------------------------------------- status
    @property
    def awaiting(self) -> str | None:
        return self._awaiting

    @property
    def user_id(self) -> int | None:
        return self._user_id

    @property
    def active(self) -> bool:
        return self._reader is not None and self._client is not None

    @property
    def client(self) -> TelegramClient | None:
        return self._client

    def set_awaiting(self, phase: str | None) -> None:
        self._awaiting = phase

    def set_hooks(
        self,
        *,
        on_reader_ready: ReaderReadyHook | None = None,
        on_reader_stopped: ReaderStoppedHook | None = None,
        on_trigger_media: TriggerMediaHook | None = None,
        on_notice: NoticeHook | None = None,
    ) -> None:
        if on_reader_ready is not None:
            self._on_reader_ready = on_reader_ready
        if on_reader_stopped is not None:
            self._on_reader_stopped = on_reader_stopped
        if on_trigger_media is not None:
            self._on_trigger_media = on_trigger_media
        if on_notice is not None:
            self._on_notice = on_notice

    async def handle_trigger(
        self,
        chat_id: int,
        message_id: int,
        reply_to_msg_id: int | None,
    ) -> None:
        """Single entry point for a trigger message, from updates or polling."""

        chat_id = int(chat_id)
        message_id = int(message_id)
        if message_id <= self._trigger_seen.get(chat_id, 0):
            return
        self._trigger_seen[chat_id] = message_id
        reader = self._reader
        if reader is None:
            return
        has_reply = reply_to_msg_id is not None
        self._log.info("source.trigger.received reply=%s", has_reply)
        try:
            media = await reader.capture_at(chat_id, reply_to_msg_id) if has_reply else []
            if not media and not has_reply and self.effective_latest():
                media = await reader.capture_latest(chat_id)
            if media and self._on_trigger_media is not None and self._user_id:
                await self._on_trigger_media(int(self._user_id), media)
            elif has_reply:
                await self._notice("⚠️ 未能读取被回复的消息：可能已被删除或不是媒体。")
            else:
                await self._notice("⚠️ 没找到最近的媒体：请**长按目标消息 → 回复**，再发送触发词。")
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            self._log.warning("source.trigger.failed type=%s", type(exc).__name__)
            await self._notice("⚠️ 抓取失败，请稍后重试。")
        finally:
            if self.effective_delete_trigger():
                try:
                    await self._client.delete_messages(chat_id, [message_id])
                except Exception:  # noqa: BLE001
                    pass

    async def check_now(self) -> tuple[int, int]:
        """Manual "check now": search and process any pending trigger messages."""

        reader = self._reader
        if reader is None:
            return (0, 0)
        found = await reader.find_recent_triggers(limit=20)
        processed = 0
        for chat_id, message_id, reply_to in found:
            if message_id <= self._trigger_seen.get(chat_id, 0):
                continue
            await self.handle_trigger(chat_id, message_id, reply_to)
            processed += 1
        return (len(found), processed)

    async def _notice(self, text: str) -> None:
        if self._on_notice is None:
            return
        try:
            await self._on_notice(text)
        except Exception:  # noqa: BLE001
            pass

    def _start_trigger_poll(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._trigger_poll_loop(), name="tgvio-source-trigger-poll"
        )

    async def _stop_trigger_poll(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _trigger_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            reader = self._reader
            if reader is None or self._client is None:
                continue
            try:
                found = await reader.poll_triggers(self._trigger_seen)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("source.trigger.poll_failed type=%s", type(exc).__name__)
                continue
            for chat_id, message_id, reply_to in found:
                await self.handle_trigger(chat_id, message_id, reply_to)

    def effective_trigger(self) -> str:
        value = (self._flags.get("source_trigger") or self._settings.source_trigger or "").strip()
        return value or "#tgvio"

    def effective_delete_trigger(self) -> bool:
        raw = self._flags.get("source_delete_trigger")
        if raw is None:
            return bool(self._settings.source_delete_trigger)
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def effective_latest(self) -> bool:
        raw = self._flags.get("source_latest")
        if raw is None:
            return bool(getattr(self._settings, "source_latest", True))
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    async def toggle_latest(self) -> bool:
        value = not self.effective_latest()
        await self._flags.set(self._repository, "source_latest", "true" if value else "false")
        return value

    def effective_chats(self) -> tuple[str, ...]:
        raw = self._flags.get(_WHITELIST_FLAG)
        if not raw:
            return tuple(self._settings.source_chats)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return tuple(self._settings.source_chats)
        if not isinstance(parsed, list):
            return tuple(self._settings.source_chats)
        return tuple(str(entry).strip() for entry in parsed if str(entry).strip())

    def status_line(self) -> str:
        if self.active:
            chats = len(self._reader.allowed_ids) if self._reader is not None else 0
            configured = len(self.effective_chats())
            return f"已登录 · user_id `{self._user_id}` · 白名单 `{chats}/{configured}` 生效"
        if self._awaiting == "phone":
            return "等待你发送手机号"
        if self._awaiting == "code":
            return "等待验证码"
        if self._awaiting == "password":
            return "等待两步验证密码"
        if self._session_path.exists() or Path(f"{self._session_path}.session").exists():
            return "已有 session，但尚未连接"
        return "未登录"

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> bool:
        """Attach an already authorized session file, if present."""

        session_file = Path(f"{self._session_path}.session")
        if not session_file.is_file():
            self._log.info("source.reader.unconfigured")
            return False
        client = TelegramClient(
            str(self._session_path),
            self._settings.api_id,
            self._settings.api_hash,
            request_retries=8,
            connection_retries=8,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self._log.warning("source.session.unauthorized")
                return False
        except Exception as exc:  # noqa: BLE001 - never block bot startup
            self._log.warning("source.session.connect_failed type=%s", type(exc).__name__)
            return False
        self._client = client
        await self._start_updates()
        await self._ensure_reader()
        return True

    async def _start_updates(self) -> None:
        """Make sure the authorized client is actually receiving updates."""

        client = self._client
        if client is None:
            return
        try:
            await client.catch_up()
        except Exception as exc:  # noqa: BLE001 - best effort sync of missed updates
            self._log.warning("source.updates.catch_up_failed type=%s", type(exc).__name__)
        self._log.info("source.updates.ready")

    async def stop(self) -> None:
        await self._stop_trigger_poll()
        self._reader = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_reader(self) -> None:
        client = self._client
        if client is None:
            return
        me = await client.get_me()
        self._user_id = int(getattr(me, "id", 0) or 0)
        reader = UserSourceReader(
            client,
            trigger=self.effective_trigger(),
            allowed_chats=self.effective_chats(),
        )
        await reader.prepare()
        self._trigger_seen = await reader.seed_trigger_cursor()
        report = getattr(reader, "seed_report", {}) or {}
        self._log.info(
            "source.poll.seed chats=%s triggers=%s",
            report.get("chats", 0),
            report.get("triggers", 0),
        )
        self._reader = reader
        self._awaiting = None
        self._start_trigger_poll()
        if self._on_reader_ready is not None:
            self._on_reader_ready(client, reader, self._user_id)

    # ---------------------------------------------------------------- login
    async def request_code(self, phone: str) -> str:
        phone = (phone or "").strip()
        if not phone:
            raise SourceLoginError("手机号不能为空")
        await self._discard_client()
        client = TelegramClient(
            str(self._session_path),
            self._settings.api_id,
            self._settings.api_hash,
        )
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            await self._discard_client(client)
            raise SourceLoginError("手机号格式不正确") from exc
        except FloodWaitError as exc:
            await self._discard_client(client)
            raise SourceLoginError(f"请求过于频繁，请等待 {int(exc.seconds)} 秒") from exc
        self._client = client
        self._phone = phone
        self._phone_code_hash = str(getattr(sent, "phone_code_hash", "") or "")
        self._awaiting = "code"
        return "验证码已发送：请查看你账号的 Telegram 消息或短信，然后把验证码发给我。"

    async def submit_code(self, code: str) -> str:
        if self._client is None or not self._phone:
            raise SourceLoginError("请先发送手机号")
        value = (code or "").strip().replace(" ", "")
        if not value:
            raise SourceLoginError("验证码不能为空")
        try:
            await self._client.sign_in(
                self._phone,
                value,
                phone_code_hash=self._phone_code_hash,
            )
        except SessionPasswordNeededError:
            self._awaiting = "password"
            return "该账号开启了两步验证，请把两步验证密码发给我。"
        except PhoneCodeInvalidError as exc:
            raise SourceLoginError("验证码不正确，请重新输入") from exc
        except PhoneCodeExpiredError as exc:
            self._awaiting = "phone"
            raise SourceLoginError("验证码已过期，请重新发送手机号") from exc
        except FloodWaitError as exc:
            raise SourceLoginError(f"尝试过于频繁，请等待 {int(exc.seconds)} 秒") from exc
        return await self._finish_login()

    async def submit_password(self, password: str) -> str:
        if self._client is None:
            raise SourceLoginError("登录流程已失效，请重新发送手机号")
        if not (password or "").strip():
            raise SourceLoginError("密码不能为空")
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise SourceLoginError("两步验证密码不正确") from exc
        return await self._finish_login()

    async def _finish_login(self) -> str:
        session_file = Path(f"{self._session_path}.session")
        try:
            session_file.chmod(0o600)
        except OSError:
            pass
        # The client connected while unauthorized; reconnect so Telethon starts
        # the update loop and syncs state on an authorized session. Without this
        # the reader could never see the owner's outgoing trigger messages.
        await self._reconnect_after_login()
        await self._ensure_reader()
        chats = self.effective_chats()
        if not chats:
            return "登录成功，但还【没有来源白名单】。请点「➕ 添加来源」把我允许读取的聊天加进来。"
        if self._reader is None or not self._reader.allowed_ids:
            return "登录成功，但白名单里的来源都无法解析；请检查名称或 ID 后重试。"
        return f"登录成功，已启用 {len(self._reader.allowed_ids)} 个来源。现在可以回复目标消息发 {self.effective_trigger()} 抓取。"

    async def logout(self) -> str:
        await self._stop_trigger_poll()
        self._trigger_seen = {}
        client = self._client
        self._reader = None
        self._awaiting = None
        if client is not None:
            try:
                if await client.is_user_authorized():
                    await client.log_out()
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._user_id = None
        self._phone = None
        self._phone_code_hash = None
        for path in (
            Path(f"{self._session_path}.session"),
            Path(f"{self._session_path}.session-journal"),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._on_reader_stopped is not None:
            self._on_reader_stopped()
        return "已退出登录并删除 session。"

    async def _reconnect_after_login(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("source.session.reconnect_failed type=%s", type(exc).__name__)
            return
        await self._start_updates()

    async def _discard_client(self, client: TelegramClient | None = None) -> None:
        target = client if client is not None else self._client
        self._client = None
        if target is not None:
            try:
                await target.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- whitelist
    async def add_chat(self, value: str) -> str:
        entry = (value or "").strip()
        if not entry:
            raise SourceLoginError("请输入 @频道名 或 -100 开头的 ID")
        current = list(self.effective_chats())
        if entry in current:
            return f"`{entry}` 已在白名单里。"
        if len(current) >= _MAX_CHATS:
            raise SourceLoginError(f"白名单最多 {_MAX_CHATS} 条")
        client = self._client
        if client is None:
            raise SourceLoginError("请先登录来源账号")
        try:
            entity = await client.get_entity(entry)
            from telethon import utils

            resolved = int(utils.get_peer_id(entity))
        except Exception as exc:  # noqa: BLE001
            raise SourceLoginError(f"无法识别 `{entry}`；确认账号已加入该聊天") from exc
        current.append(entry)
        await self._flags.set(self._repository, _WHITELIST_FLAG, json.dumps(current))
        await self._ensure_reader()
        if self._reader is None or resolved not in self._reader.allowed_ids:
            raise SourceLoginError("已保存，但本次解析失败；请重试")
        return f"已添加来源 `{entry}`。"

    async def remove_chat(self, index: int) -> str:
        current = list(self.effective_chats())
        if not 0 <= index < len(current):
            raise SourceLoginError("来源不存在")
        removed = current.pop(index)
        await self._flags.set(self._repository, _WHITELIST_FLAG, json.dumps(current))
        await self._ensure_reader()
        return f"已移除来源 `{removed}`。"

    def whitelist(self) -> list[str]:
        return list(self.effective_chats())
