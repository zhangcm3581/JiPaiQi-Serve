#!/usr/bin/env bash
# Ubuntu/systemd updater. All functions are parsed before Git can replace this file.
set -Eeuo pipefail
umask 077

JPQ_APP_DIR=${JPQ_APP_DIR:-/opt/jpq}
JPQ_SERVICE=${JPQ_SERVICE:-jpq.service}
JPQ_DB_PATH=${JPQ_DB_PATH:-/var/lib/jpq/jpq.sqlite3}
JPQ_BACKUP_DIR=${JPQ_BACKUP_DIR:-/var/backups/jpq-updates}
JPQ_LOCK_FILE=${JPQ_LOCK_FILE:-/run/lock/jpq-update.lock}
JPQ_HEALTH_URL=${JPQ_HEALTH_URL:-http://127.0.0.1:8768/health}
JPQ_HEALTH_ATTEMPTS=${JPQ_HEALTH_ATTEMPTS:-30}

jpq_backup=''
jpq_before=''
jpq_restore_service=0
jpq_code_changed=0
jpq_env_moved=0

log() { printf '[jpq-update] %s\n' "$*"; }
fail() { log "$*" >&2; exit 1; }

health_check() {
    local attempt
    for ((attempt=1; attempt<=JPQ_HEALTH_ATTEMPTS; attempt++)); do
        if systemctl is-active --quiet "$JPQ_SERVICE" &&
            curl --noproxy '*' --fail --silent --connect-timeout 1 --max-time 2 "$JPQ_HEALTH_URL" |
                python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status") == "ok" else 1)' 2>/dev/null; then
            return 0
        fi
        if ((attempt < JPQ_HEALTH_ATTEMPTS)); then sleep 1; fi
    done
    return 1
}

rollback() {
    local restored=1
    umask 022
    log '更新失败，恢复更新前的代码和虚拟环境。'
    systemctl stop "$JPQ_SERVICE" || restored=0
    if ((jpq_code_changed)); then
        # --keep refuses to overwrite unexpected local edits; never use reset --hard.
        git -C "$JPQ_APP_DIR" reset --keep "$jpq_before" || restored=0
    fi
    if ((jpq_env_moved)); then
        if [[ -e "$JPQ_APP_DIR/.venv" ]]; then
            mv "$JPQ_APP_DIR/.venv" "$jpq_backup/failed-venv" || restored=0
        fi
        if [[ ! -e "$JPQ_APP_DIR/.venv" ]]; then
            mv "$jpq_backup/venv-before" "$JPQ_APP_DIR/.venv" || restored=0
        else
            restored=0
        fi
    fi
    if ((restored)) && systemctl start "$JPQ_SERVICE" && health_check; then
        log "旧版本已恢复：$jpq_before"
    else
        log "自动恢复未完成，请检查 systemctl status ${JPQ_SERVICE} 与 ${jpq_backup}。" >&2
    fi
    log '当前数据库未被旧备份覆盖；数据库备份保留供手工恢复。'
}

finish() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    if ((status != 0 && jpq_restore_service)); then rollback; fi
    exit "$status"
}

main() {
    if [[ ${1:-} == '--help' || ${1:-} == '-h' ]]; then
        printf '%s\n' '用法：sudo bash /opt/jpq/scripts/update.sh' \
            '跟随当前分支的 upstream 快进更新；保留数据库，失败回退代码和依赖。' \
            '自定义：JPQ_APP_DIR / JPQ_SERVICE / JPQ_DB_PATH / JPQ_BACKUP_DIR / JPQ_HEALTH_URL。'
        return 0
    fi
    [[ $# == 0 ]] || fail '不接受位置参数；请使用 --help 查看说明。'
    [[ $(id -u) == 0 ]] || fail '请使用 sudo 运行更新脚本。'
    local command branch remote upstream target root
    for command in git python3 systemctl curl flock mktemp; do
        command -v "$command" >/dev/null || fail "缺少命令：${command}，请先按部署文档安装依赖。"
    done
    [[ $JPQ_HEALTH_ATTEMPTS =~ ^[1-9][0-9]*$ && ${#JPQ_HEALTH_ATTEMPTS} -le 3 ]] || fail '健康检查次数须为1到999的整数。'
    [[ -d "$JPQ_APP_DIR" ]] || fail "项目目录不存在：$JPQ_APP_DIR"
    JPQ_APP_DIR=$(cd "$JPQ_APP_DIR" && pwd -P)
    root=$(git -C "$JPQ_APP_DIR" rev-parse --show-toplevel) || fail '此目录不是Git仓库；请按部署文档重新用git clone安装。'
    [[ $root == "$JPQ_APP_DIR" ]] || fail 'JPQ_APP_DIR必须指向Git仓库根目录。'
    [[ -x "$JPQ_APP_DIR/.venv/bin/python" ]] || fail '虚拟环境不存在，请先完成首次部署。'

    exec 9>"$JPQ_LOCK_FILE"
    flock -n 9 || fail '另一个更新任务正在运行。'
    [[ -z $(git -C "$JPQ_APP_DIR" status --porcelain) ]] || fail '服务器代码有本地修改或未跟踪文件，请先处理；更新脚本不会覆盖它们。'
    branch=$(git -C "$JPQ_APP_DIR" symbolic-ref --quiet --short HEAD) || fail '当前为detached HEAD，请先切回部署分支。'
    remote=$(git -C "$JPQ_APP_DIR" config --get "branch.$branch.remote") || fail '当前分支未配置远端。'
    [[ $remote != '.' && $remote != -* ]] || fail '部署分支必须跟踪远端仓库。'
    upstream=$(git -C "$JPQ_APP_DIR" rev-parse --symbolic-full-name '@{upstream}') || fail '当前分支未配置upstream。'
    jpq_before=$(git -C "$JPQ_APP_DIR" rev-parse --verify HEAD)

    log "获取远端更新，当前分支：$branch"
    git -C "$JPQ_APP_DIR" fetch --prune "$remote"
    target=$(git -C "$JPQ_APP_DIR" rev-parse --verify "$upstream^{commit}")
    if [[ $jpq_before == "$target" ]]; then
        log "已是最新版本：${target}；服务未重启。"
        return 0
    fi
    git -C "$JPQ_APP_DIR" merge-base --is-ancestor "$jpq_before" "$target" || fail '本地与远端分支已分叉，拒绝自动合并；服务保持原状。'
    git -C "$JPQ_APP_DIR" cat-file -e "$target:requirements.txt" || fail '目标版本缺少requirements.txt。'
    git -C "$JPQ_APP_DIR" cat-file -e "$target:app/main.py" || fail '目标版本缺少app/main.py。'
    systemctl is-active --quiet "$JPQ_SERVICE" || fail '服务当前未运行，请先排查；更新脚本只更新已运行的服务。'

    mkdir -p "$JPQ_BACKUP_DIR"
    jpq_backup=$(mktemp -d "$JPQ_BACKUP_DIR/update-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
    printf '%s\n' "$jpq_before" > "$jpq_backup/commit-before.txt"
    printf '%s\n' "$target" > "$jpq_backup/commit-after.txt"
    cp "$JPQ_APP_DIR/requirements.txt" "$jpq_backup/requirements-before.txt"
    log "备份目录：$jpq_backup"

    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    jpq_restore_service=1
    systemctl stop "$JPQ_SERVICE"
    # Read-only source avoids silently creating a new empty DB if the configured path is wrong.
    "$JPQ_APP_DIR/.venv/bin/python" - "$JPQ_DB_PATH" "$jpq_backup/database.sqlite3" <<'PY'
from pathlib import Path
from contextlib import closing
import sqlite3
import sys

source_uri = Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'
with closing(sqlite3.connect(source_uri, uri=True)) as source:
    with closing(sqlite3.connect(sys.argv[2])) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('数据库备份校验失败')
PY

    mv "$JPQ_APP_DIR/.venv" "$jpq_backup/venv-before"
    jpq_env_moved=1
    # The service runs as jpq, so new code and dependencies must remain readable.
    # The already-created backup directory keeps its private 0700 permissions.
    umask 022
    jpq_code_changed=1
    git -C "$JPQ_APP_DIR" merge --ff-only "$target"
    python3 -m venv "$JPQ_APP_DIR/.venv"
    "$JPQ_APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$JPQ_APP_DIR/requirements.txt"
    "$JPQ_APP_DIR/.venv/bin/python" -m pip check
    "$JPQ_APP_DIR/.venv/bin/python" -m compileall -q "$JPQ_APP_DIR/app"
    systemctl start "$JPQ_SERVICE"
    health_check || fail '新版本未通过健康检查。'
    jpq_restore_service=0
    log "更新成功：$target"
    log "数据库与旧依赖备份保留在：$jpq_backup"
}

main "$@"; exit $?
