from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserFacingIssue:
    title: str
    explanation: str
    action: str


_SEE_CHANNEL = "请去频道看一下实际发出了什么，缺的部分重新转发给我。"

_JOB_FAILURES: dict[str, UserFacingIssue] = {
    "download_failed": UserFacingIssue(
        title="没能下载原文件",
        explanation="频道还没有发任何内容。",
        action="通常会自动重试；不行的话点“🔁 重试任务”，或重新转发原消息。",
    ),
    "source_missing": UserFacingIssue(
        title="原消息已被删除",
        explanation="来源里已经找不到这些内容，重试也取不回来；频道没有发任何内容。",
        action="如果还能找到原内容，请重新转发给我。",
    ),
    "telegram_file_timeout": UserFacingIssue(
        title="Telegram 暂时取不到文件",
        explanation="这是 Telegram 那边的临时问题，系统会隔一段时间自动再试。",
        action="不用操作，会自动再试；很久没好再点“🔁 重试任务”。",
    ),
    "disk_low": UserFacingIssue(
        title="服务器空间不够了",
        explanation="下载前就停下了，频道没有发任何内容。",
        action="在“⚙️ 设置 → 🔧 系统状态”里清理缓存，再点“🔁 重试任务”。",
    ),
    "media_analysis_failed": UserFacingIssue(
        title="文件打不开",
        explanation="文件下载好了，但格式检查没通过，还没有发布。",
        action="可以点“🔁 重试任务”；还不行就换一个文件。",
    ),
    "publish_failed": UserFacingIssue(
        title="发到频道时出错了",
        explanation="系统会先确认频道里有没有发出去，确认安全才会再发。",
        action="点“🔁 重试任务”。",
    ),
    "publish_partial": UserFacingIssue(
        title="可能已经发出去一部分",
        explanation="为了不重复发送，系统没有自动重发。",
        action=_SEE_CHANNEL,
    ),
    "publish_uncertain": UserFacingIssue(
        title="不确定有没有发出去",
        explanation="Telegram 没有给出明确结果；为了不重复发送，系统没有自动重发。",
        action=_SEE_CHANNEL,
    ),
}


def describe_job_failure(error_code: str | None) -> UserFacingIssue:
    return _JOB_FAILURES.get(
        error_code or "",
        UserFacingIssue(
            title="任务没有完成",
            explanation="系统已经安全停下，频道里不会出现重复内容。",
            action="可以点“🔁 重试任务”。",
        ),
    )


def describe_archive_failure(error_code: str | None = None) -> UserFacingIssue:
    del error_code
    return UserFacingIssue(
        title="云端备份没完成",
        explanation="频道里的内容不受影响；备份完成前，服务器上的文件会一直保留。",
        action="点“🔁 全部重试”或“☁️ 重新备份”，会从上次的位置接着传。",
    )
