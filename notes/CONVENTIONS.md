# 编写约定与引用规范

本文档定义 `notes/` 下所有文件的写作约定。新增文档前请先读本文。

---

## 1. 链接约定

### 1.1 源码引用

所有源码链接均为**相对于当前文件所在目录**的相对路径。

| 文档位置 | 引用 `kernel/.../walt.c` 的写法 |
|---|---|
| `notes/foo.md`（本文件这一层）| `../kernel/kernel/sched/walt/walt.c` |
| `notes/00-overview/foo.md` | `../../kernel/kernel/sched/walt/walt.c` |
| `notes/02-walt/foo.md` | `../../kernel/kernel/sched/walt/walt.c` |

> **易错**：`notes/` 下的顶层文档（`README.md`、`CONVENTIONS.md`）只需要
> **两个点**（`../kernel/`），子目录里的文档才需要三个点。凭手感写
> `../../kernel/` 会让链接跳出仓库。改完链接务必跑一次 §1.4 的检查脚本。

统一格式：**函数名 + 链接**，行号放在链接锚点里：

```markdown
`walt_update_task_ravg` [walt.c:2288](../kernel/kernel/sched/walt/walt.c#L2288)
```

（上面这个例子是**顶层文档**的写法。子目录里的文档要多一层
`../`，见 §1.1 的表。）

> **为什么一定要带函数名**：行号会随源码变更（哪怕是本仓库换一个 kernel tag）而失效，
> 函数名不会。重新定位时用 `grep -n "^函数名(" file.c` 即可。

#### 锚点落在**函数名所在行** [易错]

行号必须指向**包含函数名的那一行**，不是定义的首行。当签名跨行时两者不同：

```c
static inline void                                        /* 306 —— 不要锚这里 */
fixup_cumulative_runnable_avg(struct rq *rq,              /* 307 —— 锚这里 */
			      struct task_struct *p, ...)
```

判断标准一句话：`grep -n "^函数名(" file.c` 打出来的行号，就是锚点该用的行号。

> **为什么**：这是重定位时唯一能机械复现的规则。锚在返回类型行（`static void`）
> 上，读者 grep 到的行号和文档里的对不上，会以为文档过期了。
> 全树已核查过一遍，见 §1.4 的 `check-anchors.py`。

### 1.2 文档间引用

同样使用相对路径，**且不要凭手感加 `../`**：

| 文档位置 | 引用 `02-walt/01-window-model.md` 的写法 |
|---|---|
| `notes/foo.md`（本文件这一层）| `[01-window-model.md](02-walt/01-window-model.md)` |
| `notes/02-walt/foo.md` | `[01-window-model.md](01-window-model.md)` |
| `notes/03-comparison/foo.md` | `[01-window-model.md](../02-walt/01-window-model.md)` |

> 顶层文档**不需要** `../`——它一层就够到 `02-walt/`。
> 多写一层会跳出仓库（`notes/` 的上一层是仓库根，不是 `notes/` 内的目录）。

### 1.3 行号失配的处理

如果发现文档行号与源码不符，**不要静默改掉行号**，在文末「遗留问题」中记录一行，
说明是哪个 commit/tag 之后偏移的。让读者知道文档与源码的对应关系何时断裂。

### 1.4 检查脚本

每次新增/改动文档后跑这两个：

```bash
bash notes/scripts/check-links.sh        # 链接目标是否存在
python3 notes/scripts/check-anchors.py   # 行号锚点是否指向预期行
```

两者分工不同，都要跑：

**`check-links.sh`** —— 管**目标存不存在**：

- 解析 `notes/` 下所有 `.md` 的相对链接，逐个验证目标是否存在
- **自动跳过**围栏代码块与行内代码 span（其中的 `[x](y)` 是格式示例，非真链接）
- 指向**尚未编写**文档（`01-baseline/` `02-walt/` `03-comparison/`）的
  前向引用单独归类、**不算失败**——按 §1 的约定，它们是待办标记
- 退出码非 0 表示存在真正的断裂链接

**`check-anchors.py`** —— 管**行号对不对**，检查四项：

1. 显示文本与锚点数字一致（`[walt.c:2288](...#L2288)`）
2. 锚点未越界（不超过目标文件行数）
3. 锚点未落在**空行**上——落在空行读者点进去什么也看不到；
   区间引用（`#L88-L98`）的**末行**同样检查，多算一行会被抓出来
4. 锚点未停在**返回类型行**（见 §1.1 的「锚点落在函数名所在行」）

当前状态（2026-09-17）：

| 检查 | 结果 |
|---|---|
| 链接 | **2134 条，0 断裂，0 条前向引用** |
| 源码锚点 | **1618 条，全部通过** |

26 个 Markdown 文件，全部文档已写完，所有前向引用均已落地。

---

## 2. 每份文档的头部

每份文档必须以如下头部开始：

```markdown
# 标题

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17
```

---

## 3. 确定性标记

源码里读得清楚的和推测的必须区分开，使用以下标记：

| 标记 | 含义 |
|---|---|
| （无标记） | 结论直接从源码得出，可复现 |
| **`[推测]`** | 有依据的推断，但源码未直接证明 |
| **`[待确认]`** | 有疑问，需要进一步实验或读更多代码 |
| **`[反直觉]`** | 与通常认知相反、容易踩坑的点，重点标注 |

`[推测]` / `[待确认]` 的条目同时要在 [04-open-questions.md](03-comparison/04-open-questions.md) 中登记。

---

## 4. 术语约定

同一概念在全文中只用一个词。首次出现时给出定义并标注英文原文。

| 中文 | 英文 / 符号 | 含义 |
|---|---|---|
| 窗口 | window / `sched_ravg_window` | WALT 的基本时间切片，编译期默认 16ms（`HZ_300` 时 16.67ms）；20ms 是运行时调优值，见 §4.1 |
| 窗口滚动 | rollover | 窗口边界到达时把 curr 推入 prev 的动作 |
| 任务需求 | demand | 任务在过去 `RAVG_HIST_SIZE` 个窗口内见过的**最大** `sum` |
| 任务和 | sum | 任务在窗口内的可运行时间（含等待），已做频率归一化 |
| 需求预测 | pred_demand | 由 16 桶直方图预测的下一窗口忙时 |
| 归一化 | scale / frequency normalization | 把实际执行时间换算到「参考频率下等效时间」 |
| 放核 / 放置 | placement | 决定任务在哪个 CPU 上运行 |
| 调频 | frequency scaling | 决定 CPU 跑什么频率 |
| 上行/下行迁移 | upmigrate / downmigrate | 任务向容量更大/更小的簇迁移 |
| 共置 | colocation | 把同一 RTG 的任务尽量放在一起 |
| 相关线程组 | RTG (Related Thread Group) | 一组需要协同调度的线程 |
| 大任务 | big task / misfit | **`!task_fits_max(p, rq->cpu)`**——容量相对放不下的任务；按 `is_compat_thread` 分 `nr_big_tasks`(64 位) / `nr_32bit_big_tasks`(32 位)。注意 `max_task_load()` 全树零调用者，是死代码 |

### 4.1 窗口默认值的坑 [反直觉]

[walt.h:24-28](../kernel/kernel/sched/walt/walt.h#L24-L28) 定义了
`DEFAULT_SCHED_RAVG_WINDOW`：

- `CONFIG_HZ_300` 时 → `3333333 * 5` ≈ **16.67ms**
- 否则 → **16000000 ns (16ms)**

但注释与调优文档中常说的「**20ms 窗口**」是历史/推荐值，通过
`sysctl_sched_ravg_window_nr_ticks` 或 `sched_ravg_window` 在运行时设定。
**读源码时以 `sched_ravg_window` 变量为准，不要假设常量就是实际窗口。**

---

## 5. 代码片段引用原则

- 引用代码时**只引关键行**，并标注所在函数
- 不做整段粘贴；读者有源码，文档的价值在于**解释与串联**
- 涉及跨文件逻辑时，用引用链接把两个文件连起来，而不是复制两份代码

---

## 6. 文档间的职责边界

避免同一份内容写在两个地方，按下列归属：

| 内容 | 唯一归属 |
|---|---|
| 数据结构字段含义 | `00-overview/03-data-structures.md` |
| hook 注册/调用点 | `00-overview/02-integration-model.md` |
| 函数调用时序 | `00-overview/04-data-flow.md` |
| 算法细节 | 对应阶段 2 主题文档 |
| 与 baseline 的对比 | `03-comparison/` |

主题文档中需要提到字段时，**链接**到 `03-data-structures.md`，不重复解释。
