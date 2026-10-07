#!/usr/bin/env bash
# 检查 notes/ 下所有 markdown 的相对链接与源码引用是否可解析。
#
# 用法（在仓库根 WALT/ 下执行）：
#   bash notes/scripts/check-links.sh
#
# 退出码：0 = 无断裂；1 = 存在断裂（不含"未编写文档"的前向引用）
#
# 说明：
#   - 跳过 ``` 围栏代码块（其中的链接是格式示例，不是真链接）
#   - "未编写文档"的前向引用单独归类，不算失败
#     （按 CONVENTIONS，指向未写文档的链接是标记待办，不是错误）

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2

# 尚未编写的文档前缀 —— 这些的前向引用不算断裂
PENDING_RE='^(\.\./)?(01-baseline|02-walt|03-comparison)/'

# 同目录裸文件名的前向引用（如 02-walt/ 下写 04-placement.md）。
# 判据是"全仓库都没有这个 basename"，而不是"是裸文件名"——
# 否则同目录下写错文件名（04-placemnt.md）会被误判成待编写，
# 真实的断裂反而漏掉了。
is_pending_bare_name() {
    local base="${1##*/}"
    case "$1" in
        */*) return 1 ;;                       # 带目录的走 PENDING_RE
    esac
    case "$base" in
        *.md) ;;
        *) return 1 ;;
    esac
    ! find notes -name "$base" -not -path '*/scripts/*' -print -quit | grep -q .
}

broken=0
pending=0
checked=0

while IFS= read -r md; do
    dir=$(dirname "$md")

    # 去掉围栏代码块与行内代码（`...`）后再提取链接
    #   —— 这两种位置里的 [x](y) 是格式示例，不是真链接
    links=$(awk '
        /^[[:space:]]*```/ { fence = !fence; next }
        fence { next }
        {
            # 删除行内代码 span
            while (match($0, /`[^`]*`/))
                $0 = substr($0, 1, RSTART - 1) substr($0, RSTART + RLENGTH)
            print
        }
    ' "$md" | grep -oE '\]\([^)]+' | sed 's/^](//')

    [ -z "$links" ] && continue

    while IFS= read -r tgt; do
        case "$tgt" in
            http*|""|"#") continue ;;
        esac
        path="${tgt%%#*}"          # 去掉锚点
        [ -z "$path" ] && continue

        checked=$((checked + 1))

        if [ -e "$dir/$path" ]; then
            continue
        fi

        if printf '%s' "$path" | grep -qE "$PENDING_RE"; then
            pending=$((pending + 1))
            continue
        fi

        if is_pending_bare_name "$path"; then
            pending=$((pending + 1))
            continue
        fi

        echo "BROKEN  $md  ->  $tgt"
        broken=$((broken + 1))
    done <<< "$links"
done < <(find notes -name '*.md' -not -path '*/scripts/*' | sort)

echo "----------------------------------------"
echo "已检查链接：$checked    断裂：$broken    待编写文档的前向引用：$pending"

[ "$broken" -eq 0 ] || exit 1
exit 0
