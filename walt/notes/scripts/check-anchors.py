#!/usr/bin/env python3
"""校验 notes/ 下所有 Markdown 里指向内核源码的 #L 行号锚点。

配合 check-links.sh 使用：check-links.sh 管「链接目标是否存在」，
本脚本管「锚点行号是否指向预期的那一行」。

检查项（对应 CONVENTIONS.md §1.1）：

1. **显示与锚点一致** —— `[walt.c:2288](...#L2288)` 里数字必须相同。
2. **不越界** —— 锚点行号不得超过目标文件总行数。
3. **不落在空行** —— 链接锚在空行上，读者点进去看不到任何东西。
   区间引用（`#L88-L98`）的**末行**同样检查：取到闭区间外的空行
   说明区间多算了一行。
4. **停在返回类型行** —— 本项目约定锚点落在**函数名所在行**
   （见 CONVENTIONS.md §1.1，重定位靠 `grep -n "^函数名("`）。
   若锚点行是一个不含 `(` 的裸声明行、且下一行才是 `名字(`，
   说明锚点早了一行。

用法：
    python3 notes/scripts/check-anchors.py          # 在当前仓库根运行
    python3 notes/scripts/check-anchors.py -v       # 额外打印通过的统计

退出码非 0 表示存在问题。
"""

import os
import re
import sys
import glob

# 显示文本 + 锚点。显示文本可以是 `walt.c:2288`、`include/.../walt.h:157`
# 或干脆只是 `:2288`，所以这里不约束冒号前的部分。
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*#L\d+(?:-L\d+)?)\)")
ANCHOR_RE = re.compile(r"#L(\d+)(?:-L(\d+))?$")

# 裸声明行：只有类型/修饰符，没有 '(' —— 典型如 `static inline void`
BARE_DECL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ \t\*]*$")
# 函数名行：可选空白 + 标识符 + '('
NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def strip_code_spans(text):
    """抹掉行内代码 span 与围栏代码块，避免把格式示例当真链接。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def check(root, verbose=False):
    files = sorted(glob.glob(os.path.join(root, "notes", "**", "*.md"), recursive=True))
    line_cache = {}

    def lines_of(path):
        if path not in line_cache:
            with open(path, encoding="utf-8", errors="replace") as fh:
                line_cache[path] = fh.read().split("\n")
        return line_cache[path]

    problems = []
    n_anchors = 0
    n_skipped = 0

    for md in files:
        with open(md, encoding="utf-8", errors="replace") as fh:
            src = strip_code_spans(fh.read())
        rel = os.path.relpath(md, root)

        for m in LINK_RE.finditer(src):
            display, target = m.group(1), m.group(2)
            target_path, frag = target.split("#", 1)
            if not target_path or "://" in target_path:
                n_skipped += 1
                continue

            abs_target = os.path.normpath(
                os.path.join(os.path.dirname(md), target_path)
            )
            if not os.path.exists(abs_target):
                # 目标不存在由 check-links.sh 负责报告，这里跳过
                n_skipped += 1
                continue

            n_anchors += 1
            L = lines_of(abs_target)
            start_s, end_s = ANCHOR_RE.search("#" + frag).groups()

            # 1. 显示与锚点一致
            shown = re.search(r":(\d+)(?:-(\d+))?", display)
            if shown:
                if shown.group(1) and shown.group(1) != start_s:
                    problems.append(
                        (rel, target, f"显示行 {shown.group(1)} != 锚点行 {start_s}")
                    )
                if shown.group(2) and end_s and shown.group(2) != end_s:
                    problems.append(
                        (rel, target, f"显示末行 {shown.group(2)} != 锚点末行 {end_s}")
                    )

            for idx_s in (start_s, end_s):
                if idx_s is None:
                    continue
                idx = int(idx_s)
                # 2. 越界
                if idx < 1 or idx > len(L):
                    problems.append(
                        (rel, target, f"第 {idx} 行越界（文件共 {len(L)} 行）")
                    )
                    continue
                # 3. 空行
                if L[idx - 1].strip() == "":
                    problems.append((rel, target, f"第 {idx} 行是空行"))
                    continue
                # 4. 停在返回类型行（只在区间起点上判断）
                if idx_s is start_s and idx + 1 <= len(L):
                    if BARE_DECL_RE.match(L[idx - 1]) and NAME_RE.match(L[idx]):
                        problems.append(
                            (
                                rel,
                                target,
                                f"第 {idx} 行是返回类型行，函数名在 "
                                f"第 {idx + 1} 行（{NAME_RE.match(L[idx]).group(1)}）",
                            )
                        )

    print(f"源码锚点：{n_anchors}   跳过（非源码/目标缺失/代码示例）：{n_skipped}")
    if problems:
        print(f"发现问题：{len(problems)}\n")
        for rel, target, why in problems:
            print(f"  {rel}: {target}\n      {why}")
        return 1

    print("全部通过：显示一致、未越界、未落在空行、未停在返回类型行。")
    if verbose:
        print(f"（扫描了 {len(files)} 个 Markdown 文件）")
    return 0


if __name__ == "__main__":
    root = os.getcwd()
    if not os.path.isdir(os.path.join(root, "notes")):
        print("请在仓库根目录运行（应能看到 notes/ 子目录）", file=sys.stderr)
        sys.exit(2)
    sys.exit(check(root, verbose="-v" in sys.argv))
