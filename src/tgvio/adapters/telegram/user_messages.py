from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserFacingIssue:
    title: str
    explanation: str
    action: str


_JOB_FAILURES: dict[str, UserFacingIssue] = {
    "download_failed": UserFacingIssue(
        title="暂时无法读取原媒体",
        explanation="任务停在下载阶段，尚未向目标频道发布任何内容。",
        action="请点“重试任务”；如果多次失败，请重新转发原消息。",
    ),
    "disk_low": UserFacingIssue(
        title="服务器可用空间不足",
        explanation="系统在下载前主动停止了任务，没有向目标频道发布内容。",
        action="清理已完成缓存或增加磁盘空间后，再点“重试任务”。",
    ),
    "media_analysis_failed": UserFacingIssue(
        title="媒体文件无法识别",
        explanation="文件已经下载，但格式检查没有通过，尚未开始发布。",
        action="可先点“重试任务”；仍失败时请换一个文件或重新导出媒体。",
    ),
    "publish_failed": UserFacingIssue(
        title="Telegram 发布没有完成",
        explanation="系统会先检查是否已有频道消息，只有确认安全时才允许重试。",
        action="打开任务详情并点“重试任务”，系统会自动执行安全检查。",
    ),
    "publish_partial": UserFacingIssue(
        title="部分内容可能已经发布",
        explanation="系统检测到已确认的 Telegram 消息，已阻止自动重发以免重复。",
        action="请先核对目标频道中的实际消息，再进行人工处理。",
    ),
    "publish_uncertain": UserFacingIssue(
        title="发布结果暂时无法确认",
        explanation="Telegram 的返回结果不明确，系统已阻止自动重发以免重复。",
        action="请先核对目标频道中的实际消息，再进行人工处理。",
    ),
}


def describe_job_failure(error_code: str | None) -> UserFacingIssue:
    return _JOB_FAILURES.get(
        error_code or "",
        UserFacingIssue(
            title="任务没有完成",
            explanation="系统已安全停止任务；请打开任务详情查看当前阶段。",
            action="可以从任务详情尝试安全重试；若按钮不可用，请联系管理员。",
        ),
    )


def describe_archive_failure(error_code: str | None = None) -> UserFacingIssue:
    del error_code
    return UserFacingIssue(
        title="WebDAV 归档未完成",
        explanation="Telegram 发布不受影响；未确认归档前，本地缓存会继续保留。",
        action="网络稳定后点“重传归档”，系统会从远端已确认的文件继续。",
    )
