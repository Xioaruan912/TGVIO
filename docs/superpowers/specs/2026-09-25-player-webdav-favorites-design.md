# Player WebDAV 收藏与配置恢复设计

日期：2026-09-25
状态：待用户审阅
范围：TGVIO Player 收藏上传、WebDAV 设置页和新 VPS 恢复

## 1. 目标

让 Player 收藏在所有登录设备间共享；收藏时自动把实际视频副本放入用户配置的 WebDAV 收藏目录；取消收藏时同步删除该副本。Player 用户级设置和收藏清单写入 WebDAV，使新 VPS 在首次提供恢复连接信息与密钥后，可以从同一 WebDAV 恢复，不依赖旧 VPS 的 `player.sqlite3`。

用户确认的行为：

- 收藏目录保存真实视频副本和恢复清单。
- 收藏是 Player 实例级共享状态，不按登录 session 分区。
- 取消收藏会删除 WebDAV 中对应收藏副本。
- 设置页可修改 WebDAV 地址、路径、用户名和密码。
- 新 VPS 首次提供恢复凭据或密钥，之后从同一 WebDAV 恢复。
- 发布约束：本地不构建；源码推送至 VPS，在 VPS 构建和部署；最后用 BrowserAct 验收 `https://csdn.im`。

## 2. 当前实现边界

- 收藏在 `player.sqlite3` 的 `favorites` 表中，主键含 `token_digest`，因此当前按登录 session 分开。
- HTTP API 已有收藏增删与分页接口；前端播放页和收藏页已连接这些接口。
- Player 当前使用只读 WebDAV adapter 读取原始归档，另有受限删除 adapter；没有 PUT/MKCOL 上传能力。
- 现有 WebDAV URL、用户名、密码与归档根目录来自 Player 环境变量。
- 前端一般播放偏好目前保存在浏览器 `localStorage`。本设计的 WebDAV 配置备份范围是 Player 级存储/收藏设置和恢复清单；设备专属播放偏好继续留在浏览器，不自动跨设备同步。

实现时，新的可写收藏目标配置与原始归档读取配置分开。更改收藏目标不能改变原始归档 URL/root，也不能影响已有播放路径。

## 3. 默认远端布局

默认配置由可编辑设置初始化，不把目标地址散落在上传逻辑中：

```text
WebDAV endpoint: https://file.722225.xyz
Player root:     115/Pron/99_TGPLAYER
Favorites:       99_收藏
```

远端布局：

```text
115/Pron/99_TGPLAYER/
  player-config.enc
  favorites-manifest.enc
  99_收藏/
    <media_id>.<source_extension>
```

视频使用稳定媒体 ID 命名，避免重试重复、文件名冲突和来源文件名泄露。清单保存 schema 版本、修订号、媒体 ID、目标文件相对路径、必要播放元数据和同步状态。恢复密文使用认证加密；恢复密钥不写入 WebDAV。

WebDAV endpoint、Player root、收藏子目录和凭据都从 Player 持久设置读取。初始化时使用上述默认值。根目录/子目录按安全的相对路径规范化，拒绝绝对路径、`..`、反斜杠和控制字符。

## 4. 数据与服务设计

### 4.1 Player 级收藏

新增 Player 全局收藏存储，不再把用户收藏与 `token_digest` 绑定。迁移时把现有所有 session 的收藏做并集迁入全局收藏，避免丢失；保留必要的旧表读取兼容，确认迁移成功后再移除旧路径。

收藏实体至少保存：媒体 ID、远端相对路径、收藏时间、远端同步状态和最近错误分类。通过稳定 ID 保证上传/删除重试幂等。

### 4.2 WebDAV 写入 adapter 与同步任务

新增独立的 WebDAV 写入接口，不扩充 `ReadOnlyWebDavAdapter` 的权限。服务负责：

1. 从 Player catalog 解析媒体的安全来源路径。
2. 以有界内存流从归档读取，再流式 PUT 到收藏目录，不把整段视频读入内存或写入临时本地文件。
3. 校验传输完成状态与预期大小；只有确认视频副本可用后，才把该媒体标为已同步。
4. 保存加密的配置快照与版本化收藏清单。
5. 对目录创建、上传、删除和清单写入提供幂等重试。

本地 SQLite 保存同步 outbox/重试状态，但 WebDAV 文件和远端清单是跨 VPS 恢复所需的持久副本。上传失败不把“待同步”伪报成“已保存”；收藏在 UI 中保留并显示待重试/失败状态。

远端状态更新以 revision 和 tombstone 表达未完成操作：上传先登记待上传，再写媒体文件，最后提交已同步状态；取消收藏先写删除意图，再删媒体副本，成功后完成 tombstone。恢复时续做未完成操作，避免崩溃留下的文件被误判为已取消或已完成。

### 4.3 收藏媒体恢复与播放

启动恢复时读取清单，将收藏 ID 与副本路径导入 Player catalog 查询层。正常情况下优先复用原始归档位置；原始位置失效或 catalog 中不存在时，使用 `99_收藏` 副本作为播放来源。收藏副本必须作为 Player 自己维护的位置登记，不能伪装成原归档 package，也不能被普通 catalog sync 清除。

## 5. 设置页与 API

设置入口保留现有位置。设置主界面增加“收藏与 WebDAV”项，进入独立页面（移动端与桌面端都可达），包含：

- WebDAV endpoint URL。
- Player 根目录与收藏子目录。
- 用户名与密码输入；密码只写不读，GET 仅返回 `credentials_configured`。
- “测试连接”与保存操作；连接测试验证认证、目标目录创建/写入/删除权限，临时探测文件仅写在配置目录并在结束后清理。
- 当前收藏同步状态、最后成功同步时间、失败任务重试入口。

设置读写 API 只允许已认证 Player session；更新接口校验同源与字段范围。URL 必须 HTTPS，拒绝 localhost、私网、链路本地、保留地址和不安全重定向；连接时再次校验解析后的目标地址，避免用户可控 URL 变成 SSRF 通道。

UI 不把 WebDAV 密码或恢复密钥存入 `localStorage`，不预填密码，不在 toast 或错误详情中显示服务端异常、远端路径或凭据。

## 6. 配置加密、首次恢复与重部署

### 6.1 在线配置

设置页更新的 WebDAV 凭据在服务端加密后写入 Player 本地数据库，并在远端 `player-config.enc` 中以同一恢复协议加密。使用 AES-256-GCM，密钥由独立的 `TGVIO_PLAYER_RECOVERY_KEY` 经 HKDF-SHA256 派生；nonce 每次随机生成并在密文封装中保存。该随机恢复密钥只放在 VPS 的权限受限 Player 环境配置/部署密钥库中，不由 WebDAV 设置页读写，也不保存到 WebDAV。

使用维护中的 `cryptography` 库提供 AES-GCM/HKDF，不手写密码学；将其列入 Player 运行依赖锁文件。首个版本部署时若 VPS 环境中还没有 recovery key，由 VPS 发布流程生成随机 256-bit key 并写入权限 `0600` 的 Player env，不把值回显。之后同 VPS 发布保留此 key；新 VPS 运维从受限密钥库提供同一 key。

### 6.2 新 VPS bootstrap

新 VPS 部署时，先在 VPS 的 Player 环境配置/部署密钥库中设置与备份匹配的 `TGVIO_PLAYER_RECOVERY_KEY`，并设置该 VPS 的 Player 访问口令（访问口令不写入 WebDAV 快照）。新 VPS 第一次启动后，登录 Player，再使用一次性恢复表单输入：

- WebDAV endpoint 与 Player root（可使用上述默认值，也可填当前已更改的位置）。
- WebDAV 用户名与密码，用于读取远端文件。

恢复密钥从 VPS 环境读取，不通过表单返回或显示。

恢复成功后服务加载可编辑 WebDAV 设置、玩家级收藏和收藏副本目录。普通同 VPS 重部署也从远端检查 revision 并修复本地状态；启动过程不能用空数据库覆盖远端新版本。

如果用户丢失 WebDAV 连接凭据或恢复密钥，密文无法读取/解密，页面必须显示可操作的恢复失败原因；不尝试绕过加密或重置成空收藏。同 VPS 部署保留该环境密钥；新 VPS 必须从部署密钥库/受限 VPS 配置提供相同密钥。该密钥不能仅靠读取 WebDAV 自动找回。

### 6.3 配置目标变更

保存新的 WebDAV endpoint/root/favorites path 前，先测试新目标权限。迁移已有收藏时先复制并校验到新目录，再提交新目标配置和清单；任何步骤失败时继续使用旧配置。目标切换成功后不自动删除旧位置副本，避免配置变更导致数据丢失。页面显示旧目标仍有备份副本，后续清理作为单独的显式操作设计。

## 7. 失败处理与诊断

- 上传网络错误、HTTP 429/5xx、认证失败、配额不足和路径拒绝分别归类，保留媒体 ID/稳定指纹、HTTP 状态、操作阶段和尝试次数；严禁记录 URL 中的 userinfo、Authorization、密码或恢复密钥。
- 对 404 原始媒体依现有规则检查其是否已有收藏副本；没有副本时把任务标为需处理，不声称上传成功。
- 取消收藏后 UI 可立即从收藏列表移除，但远端清理失败时保留 tombstone 并继续重试，直到副本删除成功；恢复流程不得把删除中的媒体重新加回收藏。
- 清单写入支持修订号、校验和和原子切换/版本化文件，避免中断时以半截 JSON 覆盖最后可用版本。
- 日志只面向诊断：记录媒体 ID/指纹、HTTP 状态和 outcome，不输出账户凭据及敏感配置。

## 8. 数据库迁移与模块边界

采用不可变的新 migration，不改写已有 migration。至少包含：

- Player 全局收藏及旧 session 收藏并集合并。
- WebDAV 目标设置（凭据加密字段不以明文列存储）。
- Favorite 上传/删除 outbox、重试状态与 manifest revision。
- 收藏副本的 catalog 位置登记，确保它不被常规源归档同步误删。

新增 `WebDavWriteClient`/写入 adapter 与 `FavoriteBackupService`，并由应用服务编排收藏、同步、manifest 持久化和恢复。HTTP server 仅负责认证、参数校验、调用服务与最小 DTO；前端通过 API 调用设置与收藏状态。

## 9. 验收标准

### 自动化

- 多 session 添加/取消收藏时都读写同一 Player 全局清单；旧数据迁移后无收藏丢失。
- Fake WebDAV 验证流式上传、重复请求幂等、成功清单提交、断网/429/5xx 重试、删除 tombstone、路径校验与凭据不泄露。
- 清空本地 Player DB 后，从加密远端设置与清单恢复；收藏副本可播放，原归档媒体缺失时仍可播放。
- 更改远端 URL/path 成功迁移；目标测试或复制失败时仍保留旧配置和旧文件。
- Browser/API DTO 和测试日志均不返回或包含密码、Authorization 或恢复密钥。

### 线上

按项目运维约束：本地不构建；上传源码至 VPS，在 VPS 构建与 Player-only 部署；确认 health、运行镜像和 Bot 容器 ID；核对 HTTPS 实际提供的 JS bundle。

用 BrowserAct 对 `https://csdn.im` 验收：在“设置 → 收藏与 WebDAV”修改/验证配置，收藏一条视频并确认它真实存在于 WebDAV 且能播放；另一登录 session 确认看到同一收藏；取消收藏后确认副本与清单均已删除；验证同步失败提示/恢复流程。远端诊断日志按媒体 ID、HTTP 状态和 operation outcome 对应，不以仅有 API 200 作为上传成功证据。

## 10. 非目标

- 不改变原归档 WebDAV 的只读读取配置或其目录布局。
- 不自动跨设备同步浏览器 `localStorage` 中的设备级播放偏好。
- 不把 Bot 数据库、Telegram 凭据或其它服务环境变量备份到 Player WebDAV。
- Player HTTP 访问口令由各 VPS 的部署 secret 管理，不包含在 WebDAV 快照中；新 VPS 首次部署需要设置访问口令和 recovery key。
- 不在配置变化后静默删除旧 WebDAV 目标中的收藏副本。

## 11. 规格自检

- “实际视频副本 + 清单”“跨设备共享”“取消收藏删除副本”“设置页可改 WebDAV 凭据”“新 VPS 首次输入恢复凭据/密钥”均已纳入。
- 明确了默认端点/路径、加密边界、旧目标迁移失败回退、删除重试和原归档失效时的播放来源。
- Player 级设置与浏览器设备级播放偏好已分开，避免“Player 配置”含义不清。
- 未写入生产凭据；未实现代码、执行构建或更改线上服务。
