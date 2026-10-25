#!/usr/bin/env bash
# 用法：注册 macOS launchd 计划任务（把「我喜欢」补齐到本地曲库）。
#
#   ./scripts/install_task.sh --config ~/.music-sync/config.json --weekly --weekday 7 --time 21:00
#   ./scripts/install_task.sh --config ~/.music-sync/config.json --daily --time 08:30
#   ./scripts/install_task.sh --config ~/.music-sync/config.json --hours 24
#   ./scripts/install_task.sh --remove
#   ./scripts/install_task.sh --run-now
#
# 选项：
#   --config PATH     配置文件（默认 ~/.music-sync/config.json）
#   --task-name NAME  任务名（默认读配置里的 schedule.task_name，即 MusicSync）
#   --weekly          每周执行，配合 --weekday/--time
#   --weekday N       周几：1=周一 … 7=周日（也接受 Monday..Sunday）
#   --time HH:MM      执行时间，24 小时制
#   --daily           每天执行，配合 --time
#   --hours N         每隔 N 小时执行
#   --remove          删除已注册的任务
#   --run-now         立即运行一次已注册的任务
#
# 这个脚本只负责找解释器、校验参数，真正的 plist 生成与 launchctl 调用
# 都在 music_sync/schedule.py 里（逻辑只有一份）。
# 需要 Xcode 命令行工具提供的 python3；没有的话先 `xcode-select --install`。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
    printf '❌ %s\n' "$*" >&2
    exit 1
}

# 找 python3：优先 $PYTHON3，其次 PATH 上的 python3；找到的必须真能跑
# （macOS 上裸的 python3 可能只是 Xcode 的触发存根，跑起来只报错不干活）
find_python() {
    if [ -n "${PYTHON3:-}" ]; then
        if "$PYTHON3" -c 'import sys' >/dev/null 2>&1; then
            printf '%s' "$PYTHON3"
            return 0
        fi
        die "PYTHON3 指向的解释器跑不起来：$PYTHON3"
    fi
    if command -v python3 >/dev/null 2>&1; then
        local candidate
        candidate="$(command -v python3)"
        if "$candidate" -c 'import sys' >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
        die "找到的 python3 无法运行（$candidate）。请先装 Xcode 命令行工具：xcode-select --install"
    fi
    die "没找到 python3。请先安装 Xcode 命令行工具：xcode-select --install"
}

CONFIG=""
TASK_NAME=""
PY_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            [ $# -ge 2 ] || die "--config 后面要跟路径"
            CONFIG="$2"
            shift 2
            ;;
        --task-name)
            [ $# -ge 2 ] || die "--task-name 后面要跟名字"
            TASK_NAME="$2"
            shift 2
            ;;
        --weekly)
            PY_ARGS+=("--weekly")
            shift
            ;;
        --daily)
            PY_ARGS+=("--daily")
            shift
            ;;
        --hours)
            [ $# -ge 2 ] || die "--hours 后面要跟小时数"
            case "$2" in
                ''|*[!0-9]*) die "--hours 需要正整数，收到：$2" ;;
            esac
            PY_ARGS+=("--hours" "$2")
            shift 2
            ;;
        --weekday)
            [ $# -ge 2 ] || die "--weekday 后面要跟周几"
            weekday="$2"
            case "$weekday" in
                1) weekday="Monday" ;;
                2) weekday="Tuesday" ;;
                3) weekday="Wednesday" ;;
                4) weekday="Thursday" ;;
                5) weekday="Friday" ;;
                6) weekday="Saturday" ;;
                7) weekday="Sunday" ;;
                [Mm]onday|[Tt]uesday|[Ww]ednesday|[Tt]hursday|[Ff]riday|[Ss]aturday|[Ss]unday) ;;
                *) die "--weekday 只支持 1-7（1=周一 … 7=周日）或 Monday..Sunday，收到：$weekday" ;;
            esac
            PY_ARGS+=("--weekday" "$weekday")
            shift 2
            ;;
        --time)
            [ $# -ge 2 ] || die "--time 后面要跟 HH:MM"
            case "$2" in
                [0-2][0-9]:[0-5][0-9]) ;;
                *) die "--time 格式要是 HH:MM（如 21:00），收到：$2" ;;
            esac
            PY_ARGS+=("--time" "$2")
            shift 2
            ;;
        --remove)
            PY_ARGS+=("--remove")
            shift
            ;;
        --run-now)
            PY_ARGS+=("--run-now")
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "不认识的参数：$1（用 --help 看用法）"
            ;;
    esac
done

PYTHON="$(find_python)"

# --config 缺省时交给 Python 读默认路径；给了就必须存在
CMD=("$PYTHON" -m music_sync)
if [ -n "$CONFIG" ]; then
    [ -e "$CONFIG" ] || die "配置文件不存在：$CONFIG"
    CONFIG_ABS="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"
    CMD+=(--config "$CONFIG_ABS")
fi

CMD+=(task)
if [ -n "$TASK_NAME" ]; then
    CMD+=(--task-name "$TASK_NAME")
fi
if [ ${#PY_ARGS[@]} -gt 0 ]; then
    CMD+=("${PY_ARGS[@]}")
fi

# 切到项目根，保证 `python -m music_sync` 能找到包
cd "$PROJECT_ROOT"
exec "${CMD[@]}"
