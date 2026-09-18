#!/usr/bin/env bash
# ============================================================
# TGVIO 一键安装 / 管理脚本
# 面向零基础用户：安装、启动、日志、停止、删除、重建、修改配置
# 运行方式：bash install.sh
# ============================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"
ENV_BACKUP="$SCRIPT_DIR/.env.bak"
IMAGE_NAME="tgvio:local"
CONTAINER_NAME="tgvio"

C_RESET='\033[0m'; C_OK='\033[32m'; C_WARN='\033[33m'; C_ERR='\033[31m'; C_INFO='\033[36m'
info() { printf "${C_INFO}%s${C_RESET}\n" "$*"; }
ok()   { printf "${C_OK}%s${C_RESET}\n" "$*"; }
warn() { printf "${C_WARN}%s${C_RESET}\n" "$*"; }
err()  { printf "${C_ERR}%s${C_RESET}\n" "$*"; }
pause(){ printf '\n按回车继续...'; read -r _ || true; }

# ------------------------------------------------------------
# 环境检查
# ------------------------------------------------------------
ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    err "未检测到 Docker。"
    cat <<'EOF'

请先在服务器上安装 Docker，然后重新运行本脚本：

  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker

安装完成后执行：bash install.sh
EOF
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    err "Docker 已安装，但当前无法连接（可能没有启动，或需要管理员权限）。"
    echo "  请先启动 Docker（例如：systemctl start docker），再重试。"
    return 1
  fi
  return 0
}

container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" = "true" ]
}

container_exists() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER_NAME"
}

# ------------------------------------------------------------
# .env 读写
# ------------------------------------------------------------
env_get() {
  [ -f "$ENV_FILE" ] || { printf ''; return; }
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

env_set() {
  local key="$1" value="${2-}" tmp
  value="${value//$'\r'/}"; value="${value//$'\n'/ }"
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
    ENV_K="$key" ENV_V="$value" awk \
      'BEGIN{k=ENVIRON["ENV_K"];v=ENVIRON["ENV_V"];d=0}
       index($0,k"=")==1 {print k"="v; d=1; next}
       {print}
       END{if(!d) print k"="v}' "$ENV_FILE" >"$tmp"
  else
    { [ -f "$ENV_FILE" ] && cat "$ENV_FILE"; printf '%s=%s\n' "$key" "$value"; } >"$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

mask_value() {
  local v="${1-}" n=${#1}
  if [ "$n" -le 6 ]; then printf '******'; else printf '%s****%s' "${v:0:3}" "${v: -3}"; fi
}

backup_env() {
  [ -f "$ENV_FILE" ] || return 0
  cp -f "$ENV_FILE" "$ENV_BACKUP" && chmod 600 "$ENV_BACKUP" 2>/dev/null || true
}

# ------------------------------------------------------------
# 输入校验：$1 键名 $2 类型 $3 值 $4 额外参数
# ------------------------------------------------------------
validate_field() {
  local key="$1" type="$2" v="$3" extra="${4-}"
  case "$type" in
    nonempty)      [ -n "$v" ] || { err "「$key」不能为空。"; return 1; } ;;
    int_pos)       [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -gt 0 ] || { err "「$key」必须是正整数。"; return 1; } ;;
    int_range)     { [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge "${extra%%:*}" ] && [ "$v" -le "${extra##*:}" ]; } \
                     || { err "「$key」必须是 ${extra%%:*} 到 ${extra##*:} 之间的整数。"; return 1; } ;;
    bool)          [ "$v" = "true" ] || [ "$v" = "false" ] || { err "只能填 true（开）或 false（关）。"; return 1; } ;;
    choice)        [[ ",$extra," == *",$v,"* ]] || { err "只能填：$extra"; return 1; } ;;
    api_hash)      [[ "$v" =~ ^[0-9a-fA-F]{32}$ ]] || { err "API_HASH 应为 32 位十六进制字符。"; return 1; } ;;
    bot_token)     [[ "$v" =~ ^[0-9]+:[A-Za-z0-9_-]{10,}$ ]] || { err "机器人令牌格式不对，应形如 123456:ABC..."; return 1; } ;;
    dest_channel)  { [[ "$v" =~ ^@[A-Za-z0-9_]{5,}$ ]] || [[ "$v" =~ ^-100[0-9]{6,}$ ]]; } \
                     || { err "目标频道请填 @频道名 或 -100 开头的数字 ID。"; return 1; } ;;
    ids)           [[ "$v" =~ ^[0-9]+(,[0-9]+)*$ ]] || { err "请填数字用户 ID，多个用英文逗号分隔。"; return 1; } ;;
    url_or_empty)  [ -z "$v" ] || [[ "$v" =~ ^https?:// ]] || { err "请留空，或填以 http:// 或 https:// 开头的地址。"; return 1; } ;;
    any)           return 0 ;;
  esac
  return 0
}

ask_field() {
  local key="$1" label="$2" type="$3" extra="${4-}" current input
  current="$(env_get "$key")"
  local shown="$current"
  case "$type" in
    secret|secret_optional|api_hash|bot_token) shown="$(mask_value "$current")" ;;
  esac
  [ -z "$shown" ] && shown="（空）"
  printf '  %s\n  当前：%s\n  请输入新值（直接回车=不修改，输入 - =清空）：' "$label" "$shown"
  read -r input || true
  [ -z "$input" ] && return 2
  if [ "$input" = "-" ]; then
    case "$type" in
      nonempty|secret|api_hash|bot_token|dest_channel|ids) err "这一项是必填，不能清空。"; return 1 ;;
    esac
    env_set "$key" ""; ok "已清空：$key"; return 0
  fi
  if [ "$type" = "bool" ]; then
    case "$input" in
      1|true|yes|on|开) input="true" ;;
      0|false|no|off|关) input="false" ;;
      *) err "请输入 1（开）或 0（关）。"; return 1 ;;
    esac
  fi
  validate_field "$key" "$type" "$input" "$extra" || return 1
  env_set "$key" "$input"
  ok "已保存：$key"
  return 0
}

# ------------------------------------------------------------
# 配置分组
# ------------------------------------------------------------
GROUP_REQUIRED=(
  "API_ID|API_ID（数字）|int_pos"
  "API_HASH|API_HASH（32 位）|api_hash"
  "BOT_TOKEN|机器人令牌|bot_token"
  "DEST_CHANNEL|目标频道|dest_channel"
  "ALLOWED_USERS|允许使用的用户 ID|ids"
)
GROUP_LOOK=(
  "COVER_MODE|生成封面（1 开 / 0 关）|bool"
  "COVER_WIDTH|封面宽度|int_range|128:4096"
  "FORWARD_CAPTION|保留原文字（1 开 / 0 关）|bool"
  "CHANNEL_AT|频道展示名（可空）|any"
  "GROUP_AT|群组展示名（可空）|any"
)
GROUP_SWITCH=(
  "TGVIO_RUN_BOT|启动机器人|bool"
  "TGVIO_PUBLISH_ENABLED|自动发布到频道|bool"
  "TGVIO_URL_ENABLED|允许链接下载|bool"
  "TGVIO_COLLECTIONS_ENABLED|合集功能|bool"
  "TGVIO_COLLECTION_PREVIEW_ENABLED|合集发布预览|bool"
  "TGVIO_COLLECTION_EDITING_ENABLED|草稿整理|bool"
  "TGVIO_PREVIEW_ENABLED|效果预览|bool"
  "TGVIO_ALERTS_ENABLED|失败提醒|bool"
)
GROUP_ARCHIVE=(
  "TGVIO_ARCHIVE_ENABLED|启用归档|bool"
  "TGVIO_ARCHIVE_WEBDAV_URL|WebDAV 地址|url_or_empty"
  "TGVIO_ARCHIVE_WEBDAV_USER|WebDAV 账号|any"
  "TGVIO_ARCHIVE_WEBDAV_PASSWORD|WebDAV 密码|secret_optional"
  "TGVIO_ARCHIVE_REMOTE_ROOT|远端目录|any"
  "TGVIO_ARCHIVE_POLICY|归档策略|choice|required,best_effort"
  "TGVIO_ARCHIVE_LAYOUT|目录格式|choice|v1,v2"
)
GROUP_ADV=(
  "TGVIO_WORKER_CONCURRENCY|同时处理任务数|int_range|1:16"
  "TGVIO_TELEGRAM_DOWNLOAD_WORKERS|下载并发|int_range|1:32"
  "TGVIO_TELEGRAM_UPLOAD_WORKERS|上传并发|int_range|1:32"
  "TGVIO_BATCH_MAX_ITEMS|单批最多媒体数|int_range|1:500"
  "TGVIO_CACHE_RETENTION_HOURS|缓存保留小时|int_range|1:2160"
  "TGVIO_DISK_RESERVE_MB|磁盘保留空间(MB)|int_range|0:1000000"
  "TGVIO_AUTO_RETRY_MAX_ATTEMPTS|自动重试次数|int_range|0:10"
  "TGVIO_YTDLP_COOKIES_FILE|链接下载 Cookie 文件路径（可空）|any"
  "TGVIO_SOURCE_SESSION|来源 session 文件路径（个人账号）|any"
  "TGVIO_SOURCE_CHATS|来源白名单（@名或-100ID，逗号分隔）|any"
  "TGVIO_SOURCE_TRIGGER|回复触发词（默认 #tgvio）|any"
  "TGVIO_SOURCE_TRIGGER_DELETE|抓取后删除触发消息|bool"
  "TGVIO_SOURCE_DOWNLOAD_WORKERS|来源下载并发|int_range|1:16"
  "TGVIO_LOG_LEVEL|日志级别|choice|DEBUG,INFO,WARNING,ERROR,CRITICAL"
)

edit_group() {
  local title="$1"; shift
  local specs=("$@")
  while true; do
    clear 2>/dev/null || true
    echo "========== $title =========="
    local i=1 spec key label type extra cur shown
    for spec in "${specs[@]}"; do
      IFS='|' read -r key label type extra <<<"$spec"
      cur="$(env_get "$key")"; shown="$cur"
      case "$type" in secret|secret_optional|api_hash|bot_token) shown="$(mask_value "$cur")";; esac
      [ -z "$shown" ] && shown="（空）"
      printf '  %2d) %-26s 当前：%s\n' "$i" "$label" "$shown"
      i=$((i + 1))
    done
    printf '   0) 返回上一级\n\n请选择要修改的编号：'
    local choice; read -r choice || true
    case "$choice" in
      0|"") return 0 ;;
      *[!0-9]*) err "请输入编号。"; sleep 1; continue ;;
    esac
    if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#specs[@]}" ]; then err "没有这个编号。"; sleep 1; continue; fi
    spec="${specs[$((choice - 1))]}"
    IFS='|' read -r key label type extra <<<"$spec"
    echo
    ask_field "$key" "$label" "$type" "${extra-}"
    printf '\n'
    pause
  done
}

view_all() {
  echo "========== 当前配置（密钥打码）=========="
  local line key value
  while IFS= read -r line; do
    case "$line" in
      ""|\#*) printf '%s\n' "$line"; continue ;;
    esac
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in
      API_HASH|BOT_TOKEN|*PASSWORD*|*TOKEN*) value="$(mask_value "$value")" ;;
    esac
    printf '%s=%s\n' "$key" "$value"
  done <"$ENV_FILE"
}

restore_default() {
  printf '将用 .env.example 覆盖当前配置（会保留已填的必填项）。确认？(y/N) '
  local a; read -r a || true
  case "$a" in y|Y) : ;; *) return ;; esac
  local keep=() k
  for k in API_ID API_HASH BOT_TOKEN DEST_CHANNEL ALLOWED_USERS TGVIO_RUN_BOT TGVIO_PUBLISH_ENABLED; do
    keep+=("$k=$(env_get "$k")")
  done
  cp -f "$ENV_EXAMPLE" "$ENV_FILE"
  local kv
  for kv in "${keep[@]}"; do env_set "${kv%%=*}" "${kv#*=}"; done
  ok "已恢复默认（必填项保留）"
}

backup_menu() {
  while true; do
    clear 2>/dev/null || true
    echo "========== 备份 / 恢复 =========="
    echo "  1) 备份当前配置（覆盖 .env.bak）"
    echo "  2) 从 .env.bak 恢复"
    echo "  3) 恢复默认配置（保留必填项）"
    echo "  0) 返回"
    printf '请选择：'; local c; read -r c || true
    case "$c" in
      1) cp -f "$ENV_FILE" "$ENV_BACKUP" && chmod 600 "$ENV_BACKUP" 2>/dev/null; ok "已备份到 .env.bak"; pause ;;
      2) if [ -f "$ENV_BACKUP" ]; then cp -f "$ENV_BACKUP" "$ENV_FILE"; ok "已从备份恢复"; else err "没有找到 .env.bak"; fi; pause ;;
      3) restore_default; pause ;;
      0) return ;;
      *) err "没有这个编号。"; sleep 1 ;;
    esac
  done
}

# ------------------------------------------------------------
# 镜像与容器
# ------------------------------------------------------------
build_image() {
  info "正在构建镜像（第一次通常需要几分钟，请耐心等待）..."
  if docker build --target runtime -t "$IMAGE_NAME" "$SCRIPT_DIR"; then
    ok "镜像构建完成。"; return 0
  fi
  err "镜像构建失败，请检查网络后重试。"; return 1
}

validate_env() {
  [ -f "$ENV_FILE" ] || return 0
  info "正在检查配置是否正确..."
  local out
  out="$(docker run --rm --env-file "$ENV_FILE" "$IMAGE_NAME" \
        python -c "from tgvio.config import Settings; Settings.from_env(); print('CONFIG_OK')" 2>&1)"
  if printf '%s' "$out" | grep -q CONFIG_OK; then
    ok "配置检查通过。"; return 0
  fi
  err "配置检查未通过："
  printf '%s\n' "$out" | tail -n 2
  case "$out" in
    *"missing required environment variable: BOT_TOKEN"*) echo "→ 请填写机器人的令牌（BOT_TOKEN）" ;;
    *"missing required environment variable: API_ID"*)    echo "→ 请填写 API_ID" ;;
    *"missing required environment variable: API_HASH"*)  echo "→ 请填写 API_HASH" ;;
    *"missing required environment variable: DEST_CHANNEL"*) echo "→ 请填写目标频道" ;;
    *"missing required environment variable: ALLOWED_USERS"*) echo "→ 请填写你的数字用户 ID" ;;
    *"invalid ALLOWED_USERS"*) echo "→ 用户 ID 必须是数字，多个用英文逗号分隔" ;;
    *"API_ID must be positive"*) echo "→ API_ID 必须是正整数" ;;
    *"invalid API_HASH"*) echo "→ API_HASH 格式不正确" ;;
  esac
  return 1
}

start_container() {
  mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/downloads" "$SCRIPT_DIR/session" "$SCRIPT_DIR/logs"
  if container_exists; then
    info "检测到已有容器，正在重新创建..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  info "正在启动机器人..."
  if ! docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      --env-file "$ENV_FILE" \
      -v "$SCRIPT_DIR/data:/app/data" \
      -v "$SCRIPT_DIR/downloads:/app/downloads" \
      -v "$SCRIPT_DIR/session:/app/session" \
      -v "$SCRIPT_DIR/logs:/app/logs" \
      "$IMAGE_NAME" >/dev/null; then
    err "启动失败，请用菜单「2) 查看日志」查看原因。"; return 1
  fi
  sleep 3
  if container_running; then
    ok "机器人已启动。请在 Telegram 里给机器人发送 /start 开始使用。"
  else
    err "容器未能正常运行，请用菜单「2) 查看日志」查看原因。"
  fi
}

restart_if_running() {
  if container_running; then
    printf '是否现在重启机器人让改动生效？(y/N) '
    local a; read -r a || true
    case "$a" in
      y|Y) docker restart "$CONTAINER_NAME" >/dev/null && ok "已重启" ;;
    esac
  fi
}

# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
do_install() {
  ensure_docker || { pause; return; }
  if [ ! -f "$ENV_FILE" ]; then
    cp -f "$ENV_EXAMPLE" "$ENV_FILE" && chmod 600 "$ENV_FILE"
    info "已生成配置文件：.env"
  fi
  echo
  echo "请填写下面 5 项信息（可参考 README 的「开始前要准备什么」）："
  local spec key label type extra
  for spec in "${GROUP_REQUIRED[@]}"; do
    IFS='|' read -r key label type extra <<<"$spec"
    while [ -z "$(env_get "$key")" ]; do
      ask_field "$key" "$label" "$type" "${extra-}"
      [ -z "$(env_get "$key")" ] && warn "这一项是必填，请重新输入。"
    done
  done
  env_set TGVIO_ENV production
  env_set TGVIO_RUN_BOT true
  env_set TGVIO_PUBLISH_ENABLED true
  ok "配置已保存到 .env"
  build_image || { pause; return; }
  if ! validate_env; then
    warn "请到菜单「7) 修改配置」修正后，再选择「5) 重建 / 更新」。"
    pause; return
  fi
  start_container
  pause
}

do_logs() {
  ensure_docker || { pause; return; }
  if ! container_exists; then err "还没有安装，请先选择「1) 安装并启动」。"; pause; return; fi
  info "正在显示日志（按 Ctrl+C 退出）..."
  docker logs -f --tail 200 "$CONTAINER_NAME"
}

do_stop() {
  ensure_docker || { pause; return; }
  if container_running; then
    docker stop "$CONTAINER_NAME" >/dev/null && ok "已停止（数据保留）。"
  else
    warn "机器人当前没有在运行。"
  fi
  pause
}

do_delete() {
  ensure_docker || { pause; return; }
  if ! container_exists && ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    warn "没有找到已安装的机器人和镜像。"; pause; return
  fi
  printf '确认要删除机器人（容器与镜像）吗？(y/N) '
  local a; read -r a || true
  case "$a" in y|Y) : ;; *) return ;; esac
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true
  ok "已删除容器与镜像。"
  printf '是否同时删除数据（data / session / downloads / logs）？此操作不可恢复！(y/N) '
  local b; read -r b || true
  case "$b" in
    y|Y)
      rm -rf "$SCRIPT_DIR/data" "$SCRIPT_DIR/downloads" "$SCRIPT_DIR/session" "$SCRIPT_DIR/logs"
      ok "数据已删除。"
      ;;
    *) info "数据已保留。" ;;
  esac
  pause
}

do_rebuild() {
  ensure_docker || { pause; return; }
  if command -v git >/dev/null 2>&1 && [ -d "$SCRIPT_DIR/.git" ]; then
    info "正在获取最新代码..."
    git pull --ff-only 2>/dev/null || warn "自动更新失败，将使用当前代码继续。"
  fi
  build_image || { pause; return; }
  validate_env || { warn "请到菜单「7) 修改配置」修正后重试。"; pause; return; }
  start_container
  pause
}

do_status() {
  ensure_docker || { pause; return; }
  if container_running; then
    ok "机器人正在运行。"
    docker ps --filter "name=$CONTAINER_NAME" --format '容器：{{.Names}}  状态：{{.Status}}'
  elif container_exists; then
    warn "容器存在，但没有运行。可在主菜单选择「1) 安装并启动」。"
  else
    warn "还没有安装。"
  fi
  pause
}

edit_config_menu() {
  if [ ! -f "$ENV_FILE" ]; then err ".env 不存在，请先选择「1) 安装并启动」。"; pause; return; fi
  backup_env
  while true; do
    clear 2>/dev/null || true
    echo "================ 修改配置 ================"
    echo "配置文件：$ENV_FILE（已自动备份到 .env.bak）"
    echo
    echo "  1) 必填凭证（令牌 / API / 频道 / 用户 ID）"
    echo "  2) 发布外观"
    echo "  3) 功能开关"
    echo "  4) 归档（WebDAV 备份）"
    echo "  5) 性能与高级"
    echo "  6) 查看全部配置（密钥打码）"
    echo "  7) 备份 / 恢复"
    echo "  0) 返回主菜单"
    echo
    printf '请选择：'; local c; read -r c || true
    case "$c" in
      1) edit_group "必填凭证" "${GROUP_REQUIRED[@]}" ;;
      2) edit_group "发布外观" "${GROUP_LOOK[@]}" ;;
      3) edit_group "功能开关" "${GROUP_SWITCH[@]}" ;;
      4) edit_group "归档（WebDAV 备份）" "${GROUP_ARCHIVE[@]}" ;;
      5) edit_group "性能与高级" "${GROUP_ADV[@]}" ;;
      6) clear 2>/dev/null || true; view_all; pause ;;
      7) backup_menu ;;
      0) restart_if_running; return ;;
      *) err "没有这个编号。"; sleep 1 ;;
    esac
  done
}

main_menu() {
  while true; do
    clear 2>/dev/null || true
    echo "=================================================="
    echo "        TGVIO 视频转发机器人 · 管理菜单"
    echo "=================================================="
    echo "  1) 安装并启动"
    echo "  2) 查看日志"
    echo "  3) 停止"
    echo "  4) 删除"
    echo "  5) 重建 / 更新"
    echo "  6) 查看状态"
    echo "  7) 修改配置"
    echo "  0) 退出"
    echo "=================================================="
    printf '请选择：'; local c; read -r c || true
    case "$c" in
      1) do_install ;;
      2) do_logs ;;
      3) do_stop ;;
      4) do_delete ;;
      5) do_rebuild ;;
      6) do_status ;;
      7) edit_config_menu ;;
      0) exit 0 ;;
      *) err "没有这个编号，请重新输入。"; sleep 1 ;;
    esac
  done
}

main_menu
