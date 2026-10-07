# WALT 学习笔记 · 总索引

本目录是对 Qualcomm WALT（Window Assisted Load Tracking）调度器的源码级解剖笔记。

| 项 | 值 |
|---|---|
| 内核版本 | **5.15.211** (Qualcomm, sm8550 / lineage-21) |
| 源码根 | [kernel/](../kernel/) |
| WALT 源码 | [kernel/kernel/sched/walt/](../kernel/kernel/sched/walt/) |
| 调度器源码 | [kernel/kernel/sched/](../kernel/kernel/sched/) |
| 最后核对日期 | 2026-09-17 |

> **阅读约定**：所有源码链接均相对于**当前文件所在目录**。引用格式为
> `函数名 [file.c:123](相对路径#L123)` —— 行号会随源码变更失效，**函数名是稳定的定位依据**。
> 详见 [CONVENTIONS.md](CONVENTIONS.md)。

---

## 一、先读这三篇

如果你只有 30 分钟，按这个顺序读：

1. **[架构总览](00-overview/01-architecture.md)** —— 12 个源文件各自到底负责什么，以及三个最容易踩的认知陷阱
2. **[集成模型](00-overview/02-integration-model.md)** —— WALT 如何在不修改原生调度器的前提下接进去
3. **[数据流动总图](00-overview/04-data-flow.md)** —— 从调度事件到 placement / 调频 / core_ctl 三个消费方的全链路

---

## 二、目录结构

### 阶段 0 · 基础设施（总览与索引基础）

| 文档 | 内容 |
|---|---|
| [01-architecture.md](00-overview/01-architecture.md) | 模块职责总览、12 文件职责表、易错点 |
| [02-integration-model.md](00-overview/02-integration-model.md) | 50 个 vendor hook 的注册点与调用点、`android_vendor_data1` 挂载机制 |
| [03-data-structures.md](00-overview/03-data-structures.md) | `walt_task_struct` / `walt_rq` / `walt_sched_cluster` 等字段级解析 |
| [04-data-flow.md](00-overview/04-data-flow.md) | 数据流动总图与时序 |
| [scripts/check-links.sh](scripts/check-links.sh) | 链接完整性检查脚本（见 [CONVENTIONS §1.4](CONVENTIONS.md)）|
| [scripts/check-anchors.py](scripts/check-anchors.py) | 行号锚点校验：越界 / 空行 / 停在返回类型行 |

### 阶段 1 · 原生 Linux 调度器（baseline）

| 文档 | 内容 |
|---|---|
| [01-sched-framework.md](01-baseline/01-sched-framework.md) | 调度类框架、`__schedule` 主流程、`struct rq` / `cfs_rq` |
| [02-pelt.md](01-baseline/02-pelt.md) | PELT 几何衰减、`update_load_avg`、util_est |
| [03-schedutil.md](01-baseline/03-schedutil.md) | `sugov_get_util`、`map_util_freq`、iowait boost |
| [04-placement-eas.md](01-baseline/04-placement-eas.md) | `select_task_rq_fair`、EAS `find_energy_efficient_cpu` |
| [05-load-balance.md](01-baseline/05-load-balance.md) | `load_balance`、sched domain、active balance |

### 阶段 2 · WALT

| 文档 | 内容 |
|---|---|
| [01-window-model.md](02-walt/01-window-model.md) | 窗口模型：滚动、`walt_update_task_ravg` 六事件、CPU 忙时、频率归一化、迁移簿记 |
| [02-demand-prediction.md](02-walt/02-demand-prediction.md) | 16 桶直方图与 `pred_demand` 预测算法 |
| [03-cpufreq.md](02-walt/03-cpufreq.md) | `walt` governor：负载侧 + 映射侧跨两文件 |
| [04-placement.md](02-walt/04-placement.md) | `walt_select_task_rq_fair`、能量模型、候选选择 |
| [05-load-balance.md](02-walt/05-load-balance.md) | `walt_newidle_balance`、big task rotation、周期 LB |
| [06-groups-and-clusters.md](02-walt/06-groups-and-clusters.md) | RTG、colocation、preferred cluster、pipeline/heavy |
| [07-power-side.md](02-walt/07-power-side.md) | `core_ctl` 上下线、`walt_halt` 与 hotplug 的区别 |
| [08-boost.md](02-walt/08-boost.md) | 全局 boost 状态机、per-task boost、input boost |
| [09-rt-mvp.md](02-walt/09-rt-mvp.md) | RT placement、MVP 抢占队列 |
| [10-observability.md](02-walt/10-observability.md) | tunables 全集、tracepoint 全集、观测脚本 |
| [11-freq-pipeline.md](02-walt/11-freq-pipeline.md) | **从窗口到频率的九阶段流水线**：逐步追踪每一步的数据变换与单位，附完整数值算例 |

### 阶段 3 · 对照与收敛

| 文档 | 内容 |
|---|---|
| [01-base-vs-walt.md](03-comparison/01-base-vs-walt.md) | 逐子系统对照（框架/PELT/调频/选核/LB/功耗/RT）+ 接管手法分类 |
| [02-cpufreq-diff.md](03-comparison/02-cpufreq-diff.md) | `cpufreq_schedutil.c` vs `cpufreq_walt.c` 函数级差异 |
| [03-glossary.md](03-comparison/03-glossary.md) | 术语表：RTG / colocation / MVP / pipeline / heavy / halt / rotation / pred demand / top task 等，附「俗称 ≠ 真名」对照 |
| [04-open-questions.md](03-comparison/04-open-questions.md) | 遗留问题与待确认行为 |

---

## 三、进度追踪

- [x] 阶段 0 文档骨架与索引 —— [README.md](README.md)、[CONVENTIONS.md](CONVENTIONS.md)、[01-architecture.md](00-overview/01-architecture.md)
- [x] 阶段 0 WALT 集成模型 —— [02-integration-model.md](00-overview/02-integration-model.md)
- [x] 阶段 0 核心数据结构 —— [03-data-structures.md](00-overview/03-data-structures.md)
- [x] 阶段 0 数据流动总图 —— [04-data-flow.md](00-overview/04-data-flow.md)
- [x] 阶段 1 全部（5/5）—— [sched-framework](01-baseline/01-sched-framework.md)、[pelt](01-baseline/02-pelt.md)、[schedutil](01-baseline/03-schedutil.md)、[placement-eas](01-baseline/04-placement-eas.md)、[load-balance](01-baseline/05-load-balance.md)
- [x] 阶段 2 全部（11/11）—— [window-model](02-walt/01-window-model.md)、[demand-prediction](02-walt/02-demand-prediction.md)、[cpufreq](02-walt/03-cpufreq.md)、[placement](02-walt/04-placement.md)、[load-balance](02-walt/05-load-balance.md)、[groups-and-clusters](02-walt/06-groups-and-clusters.md)、[power-side](02-walt/07-power-side.md)、[boost](02-walt/08-boost.md)、[rt-mvp](02-walt/09-rt-mvp.md)、[observability](02-walt/10-observability.md)、[freq-pipeline](02-walt/11-freq-pipeline.md)
- [x] 阶段 3 全部（4/4）—— [base-vs-walt](03-comparison/01-base-vs-walt.md)、[cpufreq-diff](03-comparison/02-cpufreq-diff.md)、[glossary](03-comparison/03-glossary.md)、[open-questions](03-comparison/04-open-questions.md)

**全部 24 篇文档已完成**（2026-09-17）。

| 检查 | 命令 | 结果 |
|---|---|---|
| 链接完整性 | `bash notes/scripts/check-links.sh` | 2134 条，0 断裂，0 前向引用 |
| 行号锚点 | `python3 notes/scripts/check-anchors.py` | 1618 条，全部通过 |

> **2026-09-17 修订（一）**：更正了一处曾散见于 5 篇文档的错误结论——
> 「WALT 短路了 `effective_cpu_util()`」。经全树搜索，
> `android_rvh_effective_cpu_util` **没有任何注册者**，
> 该短路点从不触发。WALT 是**绕开**而非改写这条链。
> 详见 [Q-08](03-comparison/04-open-questions.md)。

> **2026-09-17 修订（二）**：新增 `check-anchors.py` 后全树复查锚点，
> 修正 13 处「锚点停在返回类型行」的引用——函数签名跨行时，
> 锚点必须落在**函数名所在行**而非 `static void` 那一行。
> 该约定原先未成文，现已补入 [CONVENTIONS §1.1](CONVENTIONS.md)。
> 其中最典型的是 `fixup_cumulative_runnable_avg`（`walt.c:307`）
> 与 `walt_select_task_rq_fair`（`walt_cfs.c:1149`）。

---

## 四、WALT 一句话概括

WALT 把「任务在过去若干个固定长度窗口内的可运行时间」作为一切决策的信号源，
替代（严格说是**旁路**）了原生 PELT 的指数衰减平均：

- **placement**（放哪个 CPU）→ `walt_cfs.c`
- **frequency**（跑多快）→ `cpufreq_walt.c` + `walt.c`
- **CPU 电源**（开几个核）→ `core_ctl.c`

三者都消费同一份由 `walt_update_task_ravg()` 在窗口边界上维护的数据。
