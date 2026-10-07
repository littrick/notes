# WALT 架构总览

> **源码**：[kernel/kernel/sched/walt/](../../kernel/kernel/sched/walt/)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

---

## 1. 一句话定位

WALT 是 Qualcomm 在 Android GKI 内核上实现的一套**窗口式负载追踪 + 调度决策**框架。
它**不替换**原生调度器，而是通过 Android vendor hook 把决策**旁挂**在原生路径上。

```
                     ┌──────────────────────────────────────┐
   原生 CFS 路径  ──▶ │  vendor hook 调用点（fair.c/core.c）  │
   （未被修改）        └──────────────┬───────────────────────┘
                                     │ trace_android_rvh_*
                                     ▼
                     ┌──────────────────────────────────────┐
                     │   WALT 回调（walt.c / walt_cfs.c …）  │
                     └──────────────┬───────────────────────┘
                                     │ 读写
                                     ▼
                     ┌──────────────────────────────────────┐
                     │  walt_task_struct / walt_rq（内联）   │
                     └──────────────────────────────────────┘
```

---

## 2. 文件职责表

行数为 `wc -l` 实测值（2026-09-17）。

| 文件 | 行数 | 实际职责 |
|---|---:|---|
| [walt.c](../../kernel/kernel/sched/walt/walt.c) | 5174 | **核心**：窗口 / 负载 / 需求计算引擎、簇拓扑、分组、迁移簿记、容量更新 |
| [walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c) | 1557 | **CFS 任务 placement**：`walt_find_best_target`、能量模型、MVP 机制 |
| [core_ctl.c](../../kernel/kernel/sched/walt/core_ctl.c) | 1551 | 基于 WALT 负载的 CPU 上下线守护线程 |
| [trace.h](../../kernel/kernel/sched/walt/trace.h) | 1544 | 50+ 个 `schedwalt` tracepoint 定义 |
| [walt.h](../../kernel/kernel/sched/walt/walt.h) | 1236 | WALT 内部头文件：数据结构、内联助手、常量 |
| [cpufreq_walt.c](../../kernel/kernel/sched/walt/cpufreq_walt.c) | 1164 | `walt` governor：util→freq 映射、boost 阶梯、限速 |
| [sysctl.c](../../kernel/kernel/sched/walt/sysctl.c) | 1147 | 全部 tunable 定义与 handler |
| [walt_lb.c](../../kernel/kernel/sched/walt/walt_lb.c) | 1134 | **负载均衡**：newidle balance、active migration、big task rotation |
| [walt_halt.c](../../kernel/kernel/sched/walt/walt_halt.c) | 613 | CPU "halt"：临时停用 CPU 并迁走任务（非热插拔、非 EAS） |
| [walt_rt.c](../../kernel/kernel/sched/walt/walt_rt.c) | 380 | RT 任务 placement + 长跑 RT 看门狗 |
| [sched_avg.c](../../kernel/kernel/sched/walt/sched_avg.c) | 338 | **注意**：不是负载追踪，见 §4.1 |
| [boost.c](../../kernel/kernel/sched/walt/boost.c) | 300 | 全局调度 boost 状态机 + cgroup 分组标志初始化 |
| [input-boost.c](../../kernel/kernel/sched/walt/input-boost.c) | 300 | 输入事件驱动的 min-freq (PM QoS) + boost |
| [walt_tp.c](../../kernel/kernel/sched/walt/walt_tp.c) | 151 | 动态 tracepoint 开关管理 |
| [fixup.c](../../kernel/kernel/sched/walt/fixup.c) | 92 | 与 vendor hook 缺失/兼容性相关的兜底 |
| [trace.c](../../kernel/kernel/sched/walt/trace.c) | 84 | tracepoint 子系统初始化 |
| [walt_debug.c](../../kernel/kernel/sched/walt/walt_debug.c) | 34 | 调试模块注册（`CONFIG_SCHED_WALT_DEBUG`） |
| [preemptirq_long.c](../../kernel/kernel/sched/walt/preemptirq_long.c) | 177 | 长 preempt/irq 关闭检测（调试模块） |

### 2.1 构建归属

见 [Makefile](../../kernel/kernel/sched/walt/Makefile)：

- `CONFIG_SCHED_WALT` → `sched-walt.o` = `walt.o boost.o sched_avg.o walt_halt.o core_ctl.o trace.o input-boost.o sysctl.o cpufreq_walt.o fixup.o walt_lb.o walt_rt.o walt_cfs.o walt_tp.o`
- `CONFIG_SCHED_WALT_DEBUG` → `sched-walt-debug.o` = `walt_debug.o preemptirq_long.o`

> **注意** `preemptirq_long.c` / `walt_debug.c` 属于**调试模块**，默认不编入。
> 统计 WALT hook 数量时要区分这两个文件，见
> [integration-model.md](02-integration-model.md#31-统计口径)。

---

## 3. 三个核心概念

理解 WALT 只需要抓住三个词，其余都是它们的推论：

### 3.1 窗口（window）

时间被切成固定长度 `sched_ravg_window` 的连续切片。所有统计量在**窗口边界**上滚一次：
`curr_*` → `prev_*`，并累计历史。

这带来 WALT 最本质的行为特征：

- **信号是阶梯状的**：窗口内不更新，边界上跳变
- **历史是有限的**：只保留 `RAVG_HIST_SIZE = 5` 个窗口
- **决策发生在边界上**：调频、core_ctl 都由窗口滚动驱动（见 §4.3）

### 3.2 需求（demand）

`demand` = 过去 5 个窗口中见过的**最大** `sum`。注意是 **max 不是平均**——
这是 WALT 相对 PELT 的关键取舍：**宁可高估也不低估**，
保证突发负载时能立刻给足算力。策略由 `WINDOW_STATS_*` 选择
（见 [walt.h:288-292](../../kernel/kernel/sched/walt/walt.h#L288-L292)）。

### 3.3 归一化（frequency normalization）

任务在 2GHz 上跑 1ms 和在 1GHz 上跑 1ms，对系统的意义不同。
WALT 用 `scale_exec_time()` 把实际执行时间换算到「参考频率下的等效时间」，
使得调度决策与当前频率解耦。这是 placement / 调频能共用一套单位的前提。

---

## 4. 三个易错点 [反直觉]

以下三点是读懂 WALT 时最容易走错的路。

### 4.1 负载计算不在 `sched_avg.c`

`sched_avg.c` 名字极具误导性。它的实际内容（函数实测）：

| 函数 | 作用 |
|---|---|
| `sched_get_cluster_util_pct` | 簇利用率百分比 |
| `sched_get_nr_running_avg` | 返回 `struct sched_avg_stats`（nr / nr_misfit / nr_max / nr_scaled） |
| `sched_update_nr_prod` | nr_running 生产计数 |
| `sched_get_cpu_util_pct` | CPU 利用率百分比 |
| `sched_update_hyst_times` | busy-hysteresis 时间更新 |
| `sched_lpm_disallowed_time` | LPM（低功耗模式）禁用时长 |

即：**统计与迟滞**，与 `sum`/`demand` 的计算毫无关系。

真正的负载计算入口是 `walt_update_task_ravg()` [walt.c:2288](../../kernel/kernel/sched/walt/walt.c#L2288)。

### 4.2 placement 不在 `walt.c`

`walt.c` 虽然最大，但**不做任务放置决策**。放置逻辑在
[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)，
入口 `walt_select_task_rq_fair` [walt_cfs.c:1149](../../kernel/kernel/sched/walt/walt_cfs.c#L1149)。

顺带一提，RT 的 placement 又在第三个地方：[walt_rt.c](../../kernel/kernel/sched/walt/walt_rt.c)
的 `walt_select_task_rq_rt`。

### 4.3 调频算法跨两个文件，且**不是** `update_util` 驱动

原生 schedutil 走 `cpufreq_update_util()` 回调（事件驱动）。
WALT governor **不走这条路**——它由**窗口滚动**驱动，
通过 `waltgov_add_callback` / `waltgov_run_callback`
（[walt.h:383-409](../../kernel/kernel/sched/walt/walt.h#L383-L409)）触发。

分工：

| 侧 | 文件 | 关键函数 |
|---|---|---|
| 负载侧（算出需要多少算力） | walt.c | `freq_policy_load` :590、`cpu_util_freq_walt` :680 |
| 映射侧（算力→频率） | cpufreq_walt.c | `walt_map_util_freq` :208、`waltgov_walt_adjust` :305 |

---

## 5. PELT 并未被移除 [反直觉]

`pelt.c` 与 `fair.c` 中的 PELT 路径**完整保留并持续计算**。证据：

- 原生 `schedutil` governor（[cpufreq_schedutil.c](../../kernel/kernel/sched/cpufreq_schedutil.c)）仍在树中
- WALT 只在 `cpu_util_cum()` [walt.h:445](../../kernel/kernel/sched/walt/walt.h#L445) 中
  把 PELT 的 `util_avg` 作为**次要信号**读取：

  ```c
  static inline unsigned long cpu_util_cum(int cpu)
  {
      return READ_ONCE(cpu_rq(cpu)->cfs.avg.util_avg);
  }
  ```

**意义**：系统里同时跑着两套负载估计，WALT 的 `demand` 用于主要决策，
PELT 的 `util_avg` 用于少数辅助判断。这正是做 base / WALT 对照实验的基础——
不需要改代码就能拿到 baseline 数据。

---

## 6. 下一步

- hook 如何接进去 → [integration-model.md](02-integration-model.md)
- 数据结构字段细节 → [data-structures.md](03-data-structures.md)
- 数据怎么流动 → [data-flow.md](04-data-flow.md)
