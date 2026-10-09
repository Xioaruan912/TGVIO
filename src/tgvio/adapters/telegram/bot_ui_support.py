from __future__ import annotations

from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import JobState
from tgvio.domain.job_query import JobListFilter
from tgvio.domain.publish import PublishStepKind


# The menu shows only everyday entries; the other commands keep working when typed.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "首页"),
    ("jobs", "我的任务"),
    ("begin", "开始一个合集"),
    ("end", "结束合集并发布"),
    ("help", "怎么用"),
)


NAV_HOME = "🏠 首页"
NAV_JOBS = "📋 我的任务"
NAV_STATUS = "📊 状态"
NAV_ARCHIVE = "☁️ 归档"
NAV_CACHE = "🧹 缓存"
NAV_MORE = "ℹ️ 更多"
NAV_HISTORY = "🗂 发布历史"
NAV_DRAFTS = "📝 我的草稿"
NAV_STYLE = "🎨 发布风格"
NAV_SETTINGS = "⚙️ 设置"
NAV_HELP = "❓ 怎么用"
COLLECTION_BEGIN_BUTTON = "📥 开始合集"
COLLECTION_NEW_BUTTON = "📥 新建合集"
COLLECTION_END_BUTTON = "🛑 结束并发布"
COLLECTION_PREVIEW_BUTTON = "👀 预览与整理"
NAV_BUTTONS = frozenset(
    {
        NAV_HOME,
        NAV_JOBS,
        NAV_STATUS,
        NAV_ARCHIVE,
        NAV_CACHE,
        NAV_MORE,
        NAV_HISTORY,
        NAV_DRAFTS,
        NAV_STYLE,
        NAV_SETTINGS,
        NAV_HELP,
    }
)
COLLECTION_BUTTONS = frozenset(
    {COLLECTION_BEGIN_BUTTON, COLLECTION_NEW_BUTTON, COLLECTION_END_BUTTON, COLLECTION_PREVIEW_BUTTON}
)


STATE_LABELS = {
    JobState.RECEIVED: "排队中",
    JobState.DOWNLOADING: "下载中",
    JobState.DOWNLOADED: "下载完成",
    JobState.ANALYZING: "检查中",
    JobState.ANALYZED: "检查完成",
    JobState.PLANNED: "等待发布",
    JobState.PUBLISHING: "发布中",
    JobState.SUCCEEDED: "已完成",
    JobState.FAILED: "失败",
    JobState.CANCELLED: "已取消",
}


ARCHIVE_STATE_LABELS = {
    ArchivePackageState.PLANNED: "等待备份",
    ArchivePackageState.STAGING: "准备中",
    ArchivePackageState.UPLOADING: "上传中",
    ArchivePackageState.VERIFYING: "核对中",
    ArchivePackageState.COMMITTED: "已完成",
    ArchivePackageState.FAILED: "失败",
    ArchivePackageState.CANCELLED: "已取消",
}


JOB_FILTER_LABELS = {
    JobListFilter.TODAY: "今天",
    JobListFilter.PENDING: "待处理",
    JobListFilter.FAILED: "失败",
    JobListFilter.COMPLETED: "已完成",
    JobListFilter.ACTIVE: "进行中",
    JobListFilter.HISTORY: "更早的",
    JobListFilter.ALL: "全部",
    JobListFilter.HELD: "暂停",
    JobListFilter.RUNNING: "进行中",
}

# The three tabs every task page shows; "has problems" is the failure page.
TASK_TABS: tuple[tuple[str, bytes], ...] = (
    ("⏳ 进行中", b"ui:jobs:running:0"),
    ("⚠️ 有问题", b"ui:failures:0"),
    ("✅ 已完成", b"ui:jobs:completed:0"),
)


STEP_LABELS = {
    PublishStepKind.CHANNEL_COVER_ALBUM: "频道封面相册",
    PublishStepKind.CHANNEL_VIDEO_COVER: "频道视频帧封面",
    PublishStepKind.CHANNEL_MEDIA_GROUP: "频道媒体组",
    PublishStepKind.CHANNEL_DOCUMENT: "频道文件",
    PublishStepKind.DISCUSSION_PHOTO_ALBUM: "评论区图片组",
    PublishStepKind.DISCUSSION_VIDEO_ALBUM: "评论区视频组",
    PublishStepKind.DISCUSSION_MEDIA_GROUP: "评论区合并相册",
    PublishStepKind.DISCUSSION_MEDIA: "评论区媒体",
    PublishStepKind.DISCUSSION_DOCUMENT: "评论区文件",
}
