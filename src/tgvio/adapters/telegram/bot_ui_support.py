from __future__ import annotations

from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import JobState
from tgvio.domain.job_query import JobListFilter
from tgvio.domain.publish import PublishStepKind


COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "打开 TGVIO 首页"),
    ("begin", "开始收集一个合集"),
    ("end", "结束合集并创建任务"),
    ("mode", "设置雪花显示偏好"),
    ("drafts", "查看合集草稿"),
    ("pause", "暂停队列或指定任务"),
    ("resume", "恢复队列或指定任务"),
    ("jobs", "查看最近任务"),
    ("status", "查看运行与任务状态"),
    ("source", "来源账号（个人 session）"),
    ("pick", "选择来源最近媒体并发布"),
    ("help", "查看使用说明"),
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
    }
)
COLLECTION_BUTTONS = frozenset(
    {COLLECTION_BEGIN_BUTTON, COLLECTION_NEW_BUTTON, COLLECTION_END_BUTTON, COLLECTION_PREVIEW_BUTTON}
)


STATE_LABELS = {
    JobState.RECEIVED: "已接收",
    JobState.DOWNLOADING: "下载中",
    JobState.DOWNLOADED: "已下载",
    JobState.ANALYZING: "分析中",
    JobState.ANALYZED: "已分析",
    JobState.PLANNED: "已规划",
    JobState.PUBLISHING: "发布中",
    JobState.SUCCEEDED: "已完成",
    JobState.FAILED: "失败",
    JobState.CANCELLED: "已取消",
}


ARCHIVE_STATE_LABELS = {
    ArchivePackageState.PLANNED: "已规划",
    ArchivePackageState.STAGING: "准备中",
    ArchivePackageState.UPLOADING: "归档中",
    ArchivePackageState.VERIFYING: "校验中",
    ArchivePackageState.COMMITTED: "已完成",
    ArchivePackageState.FAILED: "失败",
    ArchivePackageState.CANCELLED: "已取消",
}


JOB_FILTER_LABELS = {
    JobListFilter.TODAY: "今天",
    JobListFilter.PENDING: "待处理",
    JobListFilter.FAILED: "失败",
    JobListFilter.COMPLETED: "完成",
    JobListFilter.ACTIVE: "进行中",
    JobListFilter.HISTORY: "历史",
    JobListFilter.ALL: "全部",
    JobListFilter.HELD: "暂停",
}

JOB_FILTER_UI_ORDER = (
    JobListFilter.TODAY,
    JobListFilter.PENDING,
    JobListFilter.FAILED,
    JobListFilter.COMPLETED,
    JobListFilter.ACTIVE,
    JobListFilter.HISTORY,
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
