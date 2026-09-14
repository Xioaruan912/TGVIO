from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403


class IntakeStatusMixin:
    async def _owner_quiet(self, owner_id: int) -> bool:
        try:
            preference = await self._repository.get_user_preference(int(owner_id))
            return bool(preference.quiet_mode)
        except Exception:
            return False

    async def _track_status(self, chat_id: int, message_id: int, job_id: str) -> None:
        """Continuously edit the acceptance message with durable pipeline progress."""

        last_text = ""
        previous_item: int | None = None
        previous_current = 0
        previous_time = time.monotonic()
        accepted_order = await self._accepted_order(job_id)
        quiet_owner: bool | None = None
        try:
            while True:
                job = await self._intake.repository.get(job_id) if hasattr(self._intake, "repository") else None
                if job is None:
                    # IntakeService intentionally hides persistence details, so
                    # the runtime uses the processor's repository when exposed.
                    repository = getattr(self._processor, "repository", None)
                    if repository is None:
                        repository = getattr(self._processor, "_repository", None)
                    if repository is None:
                        return
                    job = await repository.get(job_id)
                else:
                    repository = self._intake.repository
                if job is None:
                    return
                if quiet_owner is None:
                    quiet_owner = await self._owner_quiet(int(job.owner_id))
                progress = await repository.get_job_progress(job_id)
                archive = await repository.get_archive_package_for_job(job_id)
                control = await repository.get_job_control(job_id)
                held = bool(control.hold_requested)

                now = time.monotonic()
                speed_bps: float | None = None
                if progress is not None and progress.phase == "downloading":
                    if previous_item == progress.item_index and progress.current >= previous_current:
                        elapsed = now - previous_time
                        delta = progress.current - previous_current
                        if elapsed > 0 and delta > 0:
                            speed_bps = delta / elapsed
                    previous_item = progress.item_index
                    previous_current = progress.current
                    previous_time = now
                else:
                    previous_item = None
                    previous_current = 0
                    previous_time = now

                text = self._render_live_status(
                    job,
                    progress,
                    archive,
                    speed_bps=speed_bps,
                    held=held,
                    accepted_order=accepted_order,
                )
                if text != last_text:
                    final = self._status_is_terminal(job, archive) or job.terminal
                    if (not quiet_owner) or final:
                        if await self._safe_edit(
                            chat_id,
                            message_id,
                            text,
                            buttons=self._status_buttons(job, archive),
                        ):
                            last_text = text
                if self._status_is_terminal(job, archive):
                    return
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.status_tracker.failed",
                "Dynamic job status tracker failed",
                job_id=job_id,
                exception_type=type(exc).__name__,
                exc_info=True,
            )

    def _render_live_status(
        self,
        job: Job,
        progress: JobProgress | None,
        archive: ArchivePackage | None,
        *,
        speed_bps: float | None = None,
        held: bool = False,
        accepted_order: int | None = None,
    ) -> str:
        total_bytes = sum(max(0, int(item.size_bytes or 0)) for item in job.items)
        label = f"任务 #{accepted_order}" if accepted_order is not None else "任务"
        lines = [
            f"✅ 已接收 · **{label}**",
            f"媒体：`{len(job.items)}` · `{self._human_bytes(total_bytes)}`",
        ]

        phase = progress.phase if progress is not None else job.state.value
        if held and not job.terminal:
            lines.append("状态：⏸ **已暂停** · 已保留当前缓存，恢复后从安全边界继续")
        elif phase == "downloading" and progress is not None:
            completed_indexes = {item.index for item in job.items if item.local_path}
            completed_bytes = sum(
                max(0, int(item.size_bytes or 0))
                for item in job.items
                if item.index in completed_indexes
            )
            current_bytes = 0 if progress.item_index in completed_indexes else progress.current
            overall_done = min(total_bytes, completed_bytes + current_bytes) if total_bytes else 0
            overall_pct = int(overall_done * 100 / total_bytes) if total_bytes else 0
            item_pct = int(progress.current * 100 / progress.total) if progress.total else 0
            lines.extend(
                [
                    "状态：⬇️ **正在下载**",
                    f"总进度：`{self._progress_bar(overall_pct)}` `{overall_pct}%` · "
                    f"`{self._human_bytes(overall_done)}/{self._human_bytes(total_bytes)}`",
                    f"当前：`{(progress.item_index or 0) + 1}/{progress.item_total or len(job.items)}` · "
                    f"`{item_pct}%` · `{self._human_bytes(progress.current)}/{self._human_bytes(progress.total)}`",
                ]
            )
            if speed_bps is not None and speed_bps > 0:
                lines.append(f"速度：`{self._human_bytes(int(speed_bps))}/s`")
        elif phase == "analyzing" and progress is not None:
            current = min(progress.total, progress.current + 1) if progress.total else 0
            lines.extend(
                [
                    "状态：🔎 **正在分析**",
                    f"进度：`{current}/{progress.total or len(job.items)}`",
                ]
            )
        elif phase == "publishing" and progress is not None:
            current = min(progress.total, progress.current + 1) if progress.total else 0
            lines.extend(
                [
                    "状态：📤 **正在发布**",
                    f"步骤：`{current}/{progress.total}`",
                ]
            )
        elif job.state == JobState.SUCCEEDED:
            lines.append("状态：✅ **Telegram 发布完成**")
        elif job.state == JobState.FAILED:
            issue = describe_job_failure(job.error_code)
            recovery = job_recovery_state(job)
            recovery_status = str(recovery.get("status", ""))
            if recovery_status == "scheduled":
                lines.extend(
                    [
                        "状态：🔄 **系统正在自动恢复**",
                        f"原因：{issue.title}",
                        f"将自动进行第 `{recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')}` 次安全重试，无需操作。",
                    ]
                )
            elif recovery_status == "quarantined":
                lines.extend(
                    [
                        "状态：🛡️ **已隔离，不会自动重发**",
                        issue.explanation,
                        "后续任务会继续；请有空时核对目标频道中的实际消息。",
                    ]
                )
            elif recovery_status == "manual_review":
                lines.extend(
                    [
                        "状态：🛡️ **安全检查阻止重发**",
                        issue.explanation,
                        "此任务已跳过，后续任务会继续；需要管理员核对发布记录。",
                    ]
                )
            elif recovery_status in {"exhausted", "abandoned"}:
                lines.extend(
                    [
                        f"状态：⏭️ **{issue.title}，已自动跳过**",
                        f"自动处理未能恢复任务（已尝试 `{recovery.get('attempt_count', 0)}` 次）。",
                        "后续任务会继续；如仍需要这份内容，可在详情中手动重试或重新转发。",
                    ]
                )
            elif job_failure_waits_for_recovery(job):
                lines.extend(
                    [
                        "状态：🔄 **系统正在判断恢复方式**",
                        f"原因：{issue.title}",
                        "无需操作，系统会自动重试或安全跳过。",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"状态：❌ **{issue.title}**",
                        issue.explanation,
                        f"下一步：{issue.action}",
                    ]
                )
        elif job.state == JobState.CANCELLED:
            lines.append("状态：⛔ **已取消**")
        elif job.state == JobState.PLANNED:
            if getattr(self._settings, "publish_enabled", False):
                lines.append("状态：🧠 **已规划，等待发布**")
            else:
                lines.append("状态：🧠 **已规划，自动发布关闭**")
        elif job.state in {JobState.DOWNLOADED, JobState.ANALYZED}:
            lines.append("状态：⏳ **准备下一阶段**")
        else:
            lines.append("状态：⏳ **等待处理**")

        if archive is not None:
            stored = sum(1 for obj in archive.objects if obj.state.value == "stored")
            archive_labels = {
                ArchivePackageState.PLANNED: "等待归档",
                ArchivePackageState.STAGING: "准备归档",
                ArchivePackageState.UPLOADING: "归档上传中",
                ArchivePackageState.VERIFYING: "归档校验中",
                ArchivePackageState.COMMITTED: "归档完成",
                ArchivePackageState.FAILED: "归档失败",
                ArchivePackageState.CANCELLED: "归档已取消",
            }
            icon = {
                ArchivePackageState.COMMITTED: "✅",
                ArchivePackageState.FAILED: "❌",
                ArchivePackageState.CANCELLED: "⛔",
            }.get(archive.state, "☁️")
            lines.append(
                f"WebDAV 归档：{icon} `{archive_labels[archive.state]}` · `{stored}/{len(archive.objects)}`"
            )
            if archive.state == ArchivePackageState.FAILED:
                issue = describe_archive_failure(archive.error_code)
                recovery = archive_recovery_state(job)
                recovery_status = str(recovery.get("status", ""))
                if recovery_status == "scheduled":
                    lines.extend(
                        [
                            issue.explanation,
                            f"系统将自动进行第 `{recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')}` 次续传，无需操作。",
                        ]
                    )
                elif recovery_status in {"exhausted", "abandoned"}:
                    lines.extend(
                        [
                            issue.explanation,
                            "自动续传已停止；Telegram 发布不受影响，可稍后从详情手动重传。",
                        ]
                    )
                elif archive_failure_waits_for_recovery(job, archive):
                    lines.extend([issue.explanation, "系统正在自动判断续传方式，无需操作。"])
                else:
                    lines.extend([issue.explanation, f"下一步：{issue.action}"])
        return "\n".join(lines)

    @staticmethod
    def _status_buttons(job: Job, archive: ArchivePackage | None):
        rows = [
            [
                Button.inline(
                    "🔎 查看任务",
                    f"ui:job:{job.id}".encode("utf-8"),
                )
            ]
        ]
        if job.terminal:
            rows[0].append(
                Button.inline("📋 结果", f"ui:result:{job.id}".encode("utf-8"))
            )
        if (
            job.state == JobState.FAILED
            and job.error_code not in {"publish_partial", "publish_uncertain"}
            and not job_failure_waits_for_recovery(job)
            and job_recovery_state(job).get("status") != "manual_review"
        ):
            rows[0].append(
                Button.inline(
                    "🔁 重试任务",
                    f"ui:retry:{job.id}".encode("utf-8"),
                )
            )
        if (
            archive is not None
            and archive.state == ArchivePackageState.FAILED
            and not archive_failure_waits_for_recovery(job, archive)
        ):
            rows.append(
                [
                    Button.inline(
                        "☁️ 重传失败归档",
                        f"ui:archive-retry:{job.id}".encode("utf-8"),
                    )
                ]
            )
        return rows

    def _status_is_terminal(self, job: Job, archive: ArchivePackage | None) -> bool:
        if job_failure_waits_for_recovery(job):
            return False
        if archive_failure_waits_for_recovery(job, archive):
            return False
        archive_terminal = archive is None or archive.state in {
            ArchivePackageState.COMMITTED,
            ArchivePackageState.FAILED,
            ArchivePackageState.CANCELLED,
        }
        if job.terminal:
            return archive_terminal
        return job.state == JobState.PLANNED and not getattr(
            self._settings,
            "publish_enabled",
            False,
        )

    @staticmethod
    def _progress_bar(percent: int) -> str:
        value = max(0, min(100, int(percent)))
        filled = min(10, value // 10)
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _human_bytes(value: int) -> str:
        amount = float(max(0, int(value or 0)))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
            amount /= 1024
        return f"{amount:.1f}TB"
