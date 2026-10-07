# 基线 vs WALT：逐子系统对照

> **最后核对**：2026-09-17
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **调频专项** → [cpufreq-diff.md](02-cpufreq-diff.md)
> **未决问题** → [open-questions.md](04-open-questions.md)

本文回答一个问题：**WALT 相对原生 Linux 5.15 调度器，改了什么、留了什么。**

---

## 0. 一句话总结

> **WALT 替换了「负载信号的产生方式」，并在此之上加了一整套
> Android 特有的 QoS 机制；它保留了原生调度器的骨架。**

具体地说：

| 层面 | 状态 |
|---|---|
| 调度框架（`schedule()` / `pick_next_task` / `context_switch`）| **完全保留** |
| `struct sched_class` 与类优先级 | **完全保留** |
| **负载信号的来源** | **替换**：窗口计数器 ← PELT |
| **选核算法** | **替换**：`walt_find_energy_efficient_cpu()` ← `find_energy_efficient_cpu()` |
| **调频 governor** | **替换**：`walt` ← `schedutil` |
| 负载均衡 | **部分替换**（4 个 hook，其中 3 个整体接管）|
| **PELT 本体** | **保留，未被移除**（见 §4）|
| 新增机制 | boost / RTG / MVP / pipeline / core_ctl / walt_halt |

---

## 1. 接管点总表

WALT 通过 **Android vendor hook** 介入原生调度器。
`trace_android_rvh_*` 是**单回调**（`DECLARE_RESTRICTED_HOOK`），
`trace_android_vh_*` 是**多回调**（`DECLARE_HOOK`）。

完整的 50 个 hook 注册见
[02-integration-model.md](../00-overview/02-integration-model.md)。
这里只列**架构级**的接管点：

| 子系统 | 原生函数 | 接管方式 | hook 位置 |
|---|---|---|---|
| **选核** | `select_task_rq_fair()` | **整体替换**（`*target_cpu` 被改写，原生逻辑全跳过）| [fair.c:7229](../../kernel/kernel/sched/fair.c#L7229) |
| **调频负载侧** | `effective_cpu_util()` | **不在链上**：WALT 用 `cpu_util_freq_walt()` 另起一条 | 无（`core.c:7328` 的短路点**未注册**）`[待确认]` |
| **newidle 平衡** | `sched_balance_newidle()` | **`done=1` 整体接管** | [fair.c:11163](../../kernel/kernel/sched/fair.c#L11163) |
| **找 busiest queue** | `find_busiest_queue()` | **`done=1`**（但 `load_balance()` 主体照常跑）| [fair.c:9925](../../kernel/kernel/sched/fair.c#L9925) |
| **NOHZ kick** | `nohz_balancer_kick()` | **`done=1`** | [fair.c:10761](../../kernel/kernel/sched/fair.c#L10761) |
| **迁移否决** | `can_migrate_task()` | **只做否决**（不改写逻辑）| [fair.c:8125](../../kernel/kernel/sched/fair.c#L8125) |
| **调度 tick** | `scheduler_tick()` | 旁路（`android_vh_scheduler_tick`）| walt.c:4997 + walt_rt.c:85 |
| **任务唤醒** | `try_to_wake_up()` | 旁路（更新窗口统计）| — |

### 三种接管手法的区别 [重要]

| 手法 | 含义 | 例子 |
|---|---|---|
| **短路 return** | 改写输出变量并使原生提前返回 | `select_task_rq_fair()` 改写 `*target_cpu` |
| **`done=1`** | 置标志位，原生检查后放弃自己的逻辑 | `sched_balance_newidle()` / `find_busiest_queue()` |
| **旁路（hook 不返回值）** | 只观察或维护 WALT 自己的状态，不影响原生 | `android_vh_scheduler_tick` |

> **不要拿 `effective_cpu_util()` 当「短路 return」的例子**。
> 它的短路点 `android_rvh_effective_cpu_util`
> （[core.c:7328-7330](../../kernel/kernel/sched/core.c#L7328-L7330)）
> 在本树中**从未被注册**，因此从不触发。详见 §8 陷阱 1。

> **`find_busiest_queue()` 的 `done=1` 语义特殊**（[反直觉]）：
> 它的意思是「**busiest 给你了，别再遍历**」，
> 而**不是**「整个 `load_balance()` 别跑了」。
> `load_balance()` 的主体（`detach_tasks` / `attach_tasks`）**照常执行**。
> 这与 `sched_balance_newidle()` 的 `done` 完全不同——后者是真的整个跳过。

---

## 2. 逐子系统对照

### 2.1 调度框架

| | 基线 | WALT | 差异 |
|---|---|---|---|
| `struct sched_class` | 5 个类，链接段排序 | **同** | 无 |
| `for_each_class()` | stop→dl→rt→fair→idle | **同** | 无 |
| `__schedule()` | 主循环 | **同** | 无 |
| `pick_next_task_fair()` | MVP 无关 | **`replace_next_task_fair` hook 插入 MVP 选择** | **有** |
| `context_switch()` / `finish_task_switch()` | — | **同** | 无 |

→ 详见 [01-sched-framework.md](../01-baseline/01-sched-framework.md)

**唯一的框架级插入是 MVP 任务选择**——
`walt_cfs_replace_next_task_fair()`（注册于 [walt_cfs.c:1556](../../kernel/kernel/sched/walt/walt_cfs.c#L1556)），
见 [09-rt-mvp.md](../02-walt/09-rt-mvp.md)。

### 2.2 负载追踪：PELT → 窗口模型

**这是 WALT 的根基，也是所有其它差异的源头。**

| 维度 | PELT | WALT 窗口 |
|---|---|---|
| 信号形式 | 几何衰减的指数平均 | **固定长度窗口的累计量** |
| 半衰期 | 32.8 ms（`y^32 = 0.5`）| 无（每窗口重置）|
| 窗口长 | 1024ns 记账单元 / 1.05ms 周期 | **`sched_ravg_window` ~16-20ms** |
| 核心量 | `util_avg` / `load_avg` / `runnable_avg` | `sum` / `demand` / `demand_scaled` |
| 新任务 | `util_est` | `nt_*` 计数器 |
| 预测 | 无 | **16 桶直方图 `pred_demand`** |
| 归一化 | `update_rq_clock_pelt()` | `scale_exec_time()` |
| 数据结构 | `struct sched_avg`（在 `sched_entity` / `cfs_rq` 内）| `struct walt_task_struct` / `walt_rq`（**vendor data 槽**）|

→ 详见 [01-window-model.md](../02-walt/01-window-model.md) / [02-demand-prediction.md](../02-walt/02-demand-prediction.md) / [02-pelt.md](../01-baseline/02-pelt.md)

> **两者不是二选一**——PELT 仍然在跑（见 §4）。

### 2.3 调频

| | schedutil | `walt` governor |
|---|---|---|
| 触发 | 事件驱动（7 个调用点）| **窗口滚动**（每 ~16-20ms）|
| util 来源 | PELT | 窗口计数器 |
| boost 源 | iowait（1 个）| **6 个**（RTG/PL/NWD/BTR/ED/HISPEED）|
| 基准频率 | `freq_invariant ? fmax : cur` | **恒 `fmax`** |
| 高负载分支 | 无 | **有**（1.0625 余量）|
| 频率档吸附 | 无 | **有** |
| 限速 | 单一 | **升/降分离** |
| `avg_cap` 动态基线 | 无 | **有** |
| uclamp 位置 | `effective_cpu_util()` 内 | **governor 侧** |

→ 逐函数差异见 [cpufreq-diff.md](02-cpufreq-diff.md)

### 2.4 选核（placement）

| | EAS | WALT |
|---|---|---|
| 入口 | `find_energy_efficient_cpu()` | `walt_find_energy_efficient_cpu()` |
| hook | 内建 | `android_rvh_select_task_rq_fair` |
| `sd_flag` | 用于区分 wake/fork | **忽略** |
| 候选生成 | 每 perf domain 一个 | **两阶段**：粗筛 + 能量精算 |
| 总是算能量 | **是** | **否**（有 `energy_eval_needed` 开关）|
| 迁移阈值 | **1/16 = 6.25%** | **1/32 ≈ 3.1%**（注释错误地写 6%）|
| fastpath | 无 | **4 条** |
| MVP / RTG / pipeline | 无 | **有**（否决 + 独占）|

→ 详见 [04-placement.md](../02-walt/04-placement.md) / [04-placement-eas.md](../01-baseline/04-placement-eas.md)

**注意**：WALT 的 hook 位置在 `select_task_rq_fair()` 的**最开头**
（[fair.c:7229](../../kernel/kernel/sched/fair.c#L7229)），
**早于任何分支**，包括 `WF_TTWU` 判定。所以 WALT 接管后，
原生的 `find_energy_efficient_cpu()` / `select_idle_sibling()` /
`wake_affine()` **一次都不会被调用**。

### 2.5 负载均衡

| | 基线 | WALT |
|---|---|---|
| `load_balance()` 主体 | — | **保留**（不重写）|
| newidle | `sched_balance_newidle()` | **整体接管**（`done=1`）|
| find_busiest_queue | 决策矩阵 | **接管**（`done=1`，但 LB 主体照常）|
| NOHZ kick | `nohz_balancer_kick()` | **接管**（`done=1`）|
| can_migrate_task | — | **只做否决** |
| 迁移成本常数 | `sysctl_sched_migration_cost` | **WALT 自己复制了一份语义**（不复用变量）|
| 大任务轮转 | 无 | **有**（`walt_lb_check_for_rotation`）|

→ 详见 [05-load-balance.md](../02-walt/05-load-balance.md) / [../01-baseline/load-balance.md](../01-baseline/05-load-balance.md)

> **WALT 不重写 `load_balance()`**——它只在**选择阶段**介入
> （找 busiest group / queue），真正的迁移执行仍走原生的
> `detach_tasks()` / `attach_tasks()`。

### 2.6 功耗侧

| 机制 | 基线 | WALT |
|---|---|---|
| CPU 热插拔 | `cpu_up()` / `cpu_down()` | **不热插拔**（`core_ctl` 已改为 halt/pause）|
| 停核 | — | **`walt_halt`**：置 mask + 排空 rq（`halt_drain_rqs` kthread 里的 `stop_one_cpu`）|
| idle 注入 | — | **无**（`grep -rni idle_inject` 零命中）|
| 决策上下文 | — | `do_core_ctl()` **在 hardirq 上下文**同步跑 |

→ 详见 [07-power-side.md](../02-walt/07-power-side.md)

> **halt ≠ offline**：halted 的 CPU 仍然是 `active` 的，
> 证据是 `walt_find_and_choose_cluster_packing_cpu()`
> （[walt.h:1023](../../kernel/kernel/sched/walt/walt.h#L1023)）用
> `cpu_active_mask & ~cpu_halt_mask`。

### 2.7 RT

| | 基线 | WALT |
|---|---|---|
| RT 负载计数 | RT 有自己的 `rt_rq->avg` | **RT 走与 CFS 完全相同的 `walt_update_task_ravg` 路径** |
| RT 拉取 | `sched_balance_rt` hook | `walt_balance_rt`（[walt_lb.c:723](../../kernel/kernel/sched/walt/walt_lb.c#L723)），判据是 `wts->last_wake_ts` 与 250µs 阈值 |
| RT hook 使用 | — | WALT 只注册 2 个；`android_rvh_sched_balance_rt` / `android_vh_sched_stat_runtime_rt` / `android_rvh_update_rt_rq_load_avg` **存在但 WALT 未注册** |

→ 详见 [09-rt-mvp.md](../02-walt/09-rt-mvp.md)

**WALT 没有 RT 专属的负载计数器**——RT 任务的 `sum` / `demand` /
`demand_scaled` / 16 桶预测位置，都和 CFS 用同一套代码算出。
源码注释 [walt.c:1978-1983](../../kernel/kernel/sched/walt/walt.c#L1978-L1983)
甚至点名了 RT：`"a real-time task runs without preemption for several windows at a stretch"`。

---

## 3. WALT 新增的机制（基线完全没有）

| 机制 | 作用 | 文档 |
|---|---|---|
| **窗口模型** | 替代 PELT 的负载信号 | [01-window-model.md](../02-walt/01-window-model.md) |
| **需求预测** | 16 桶直方图 + 泄漏积分器，提前加频 | [02-demand-prediction.md](../02-walt/02-demand-prediction.md) |
| **`do_pl_notif()`** | **唯一**的亚窗口响应（400MHz 紧急加频）| [demand-prediction.md §6.3](../02-walt/02-demand-prediction.md) |
| **boost 框架** | refcount 状态机，6 种 boost 类型 | [08-boost.md](../02-walt/08-boost.md) |
| **input boost** | 触摸提升，走 `freq_qos` 硬约束 | [08-boost.md](../02-walt/08-boost.md) |
| **RTG / 共置** | 相关线程组，`grp_time` 分流记账 | [06-groups-and-clusters.md](../02-walt/06-groups-and-clusters.md) |
| **heavy / pipeline** | 重任务重排，分配 `pipeline_cpu` | [06-groups-and-clusters.md](../02-walt/06-groups-and-clusters.md) |
| **MVP** | Most-Valuable-Task，四级优先级独占 CPU | [09-rt-mvp.md](../02-walt/09-rt-mvp.md) |
| **core_ctl** | 动态核数（通过 halt）| [07-power-side.md](../02-walt/07-power-side.md) |
| **walt_halt** | halt/pause 的作动层 | [07-power-side.md](../02-walt/07-power-side.md) |
| **大任务轮转** | `big task rotation`，互换而非迁移 | [05-load-balance.md](../02-walt/05-load-balance.md) |
| **early detection** | 检测重任务提前加频 | [08-boost.md](../02-walt/08-boost.md) |

---

## 4. 保留不变的部分 [重要]

**WALT 不是「用窗口模型替掉 PELT」，而是「在 PELT 之上另起一套」。**

| 保留的东西 | 说明 |
|---|---|
| **PELT 本体** | `kernel/sched/pelt.c` 完整保留并持续运行 |
| `cfs_rq->avg` / `se->avg` | 仍在更新（`update_load_avg()` 等）|
| `cpu_load()` / `cpu_util()` 的 PELT 路径 | 仍被原生代码使用 |
| `load_balance()` 主体 | 未重写 |
| `sched_class` 框架 | 未改 |
| CFS 红黑树 / vruntime | 未改 |

**为什么保留 PELT**：① 原生 LB / EAS / 其它子系统仍需 `util_avg`；
② `walt_compute_energy()` 之外仍有大量代码读 PELT 值；
③ 完全移除的改动面太大、回归风险高。`[推测]`

> **实际后果**：系统里**同时存在两套负载估计**，
> 数值可能不一致。调试时务必分清读的是哪一套：
> `wrq->walt_stats.*` / `wts->demand*` 是 WALT，
> `rq->cfs.avg.*` / `se->avg.*` 是 PELT。

---

## 5. 数值对照速查

| 量 | PELT / 基线 | WALT |
|---|---|---|
| 窗口 / 半衰期 | 半衰期 32.8ms | 窗口 ~16-20ms（固定）|
| `LOAD_AVG_MAX` | 47742 | — |
| 迁移阈值（选核）| **1/16 = 6.25%** | **1/32 ≈ 3.1%** |
| 调频余量 | ×1.25 (`map_util_perf`) | ×1.25 (`fmax + fmax>>2`) |
| 高负载余量 | — | **×1.0625**（`>> 4`）|
| 预测桶数 | — | **16**（`NUM_BUSY_BUCKETS`）|
| 桶索引 | — | `bidx = normalized_rt >> 6`，最小钳到 15 |
| 历史窗口数 | — | `RAVG_HIST_SIZE = 5` |
| `busy_factor` | 16 | 16（保留）|
| `imbalance_pct` | 117（MC）/ 110（SMT）| 同（保留）|
| LB 单次迁移上限 | `sysctl_sched_nr_migrate = 32` | 同（保留）|
| RT 拉取阈值 | — | **250 µs** |
| MVP slice / limit | — | **3ms / 12ms** |
| `DIRE_STRAITS_PREV_NR_LIMIT` | — | **10** |
| `NL_RATIO`（NWD）| — | **75** |
| `DEFAULT_HISPEED_LOAD` | — | **90** |

---

## 6. 性能/功耗权衡

| 维度 | WALT 的取舍 |
|---|---|
| **响应速度** | **慢于 schedutil**——调频最快也要等一个窗口。唯一例外 `do_pl_notif()` |
| **抖动** | **更平滑**——窗口平均天然滤波，且 WALT 加了频率档吸附 |
| **新任务** | **更快**——`nt_*` 计数器避免了 PELT 的缓慢爬升（类似 `util_est` 但更激进）|
| **突发负载** | **更激进**——NWD / early detection 提前拉满 |
| **省电** | 双刃：高负载分支降余量省电，但 boost 机制（尤其 FT boost 默认开启）会抬高频率 |
| **可观测性** | **更丰富**——大量 tracepoint + `/proc/sys/walt/*` |

> **`sched_boost` 开机默认 = 1（FULL_THROTTLE_BOOST）**
> （[walt.c:5131](../../kernel/kernel/sched/walt/walt.c#L5131)），
> 且 FT boost 下 `task_sched_boost()` 对**所有**任务返回 true，
> cgroup 过滤完全失效。**这意味着「默认配置」本身就是性能优先的。**
> 见 [open-questions.md Q-07](04-open-questions.md)。

---

## 7. 调试视角：该看哪一套

| 想看什么 | 基线 | WALT |
|---|---|---|
| CPU 有多忙 | `/proc/schedstat`、`cpu_util()` | `wrq->walt_stats.*`、`trace_sched_cpu_util` |
| 任务需求 | `se->avg.util_avg` | `wts->demand` / `demand_scaled` |
| 预测需求 | 无 | `wts->pred_demand_scaled`、`trace_sched_update_task_ravg` |
| 频率决策 | `trace_cpu_frequency` | `trace_waltgov_util_update` / `trace_waltgov_next_freq` |
| 选核决策 | `trace_sched_task_util` | **同名 tracepoint，但内容不同** |
| boost 状态 | 无 | `/proc/sys/walt/sched_boost` |
| 核数 | `cpu_online_mask` | `cpu_halt_mask` + `core_ctl` sysfs |

→ 完整的可观测性清单见 [10-observability.md](../02-walt/10-observability.md)

---

## 8. 读代码时的三条陷阱

1. **不要假设 hook 调用点 = hook 已生效。**
   `effective_cpu_util()` 里有一个漂亮的短路点
   （[core.c:7328-7330](../../kernel/kernel/sched/core.c#L7328-L7330)），
   但在**本树中没有任何代码注册它**，所以第 2-8 步**都是活代码**，
   WALT 也**不经过**这里。
   判断一个 `android_rvh_*` 是否真的接管，唯一可靠的方法是搜
   `register_trace_android_rvh_<名字>`；搜不到就是没接管。
   （`android_vh_*` 是 `DECLARE_HOOK` 多注册者模型，判断方式不同。）

2. **`select_task_rq_fair()` 的全部原生逻辑也是死代码**。
   WALT 的 hook 在函数最开头，早于 `WF_TTWU` 判定。

3. **`find_busiest_queue()` 的 `done=1` 不等于整个 LB 被接管**。
   `load_balance()` 主体照常跑。

---

## 9. 相关文档

- 架构总览 → [01-architecture.md](../00-overview/01-architecture.md)
- 数据结构 → [03-data-structures.md](../00-overview/03-data-structures.md)
- 数据流 → [04-data-flow.md](../00-overview/04-data-flow.md)
- hook 集成 → [02-integration-model.md](../00-overview/02-integration-model.md)
- 调频逐函数 → [cpufreq-diff.md](02-cpufreq-diff.md)
- 术语释义 → [glossary.md](03-glossary.md)
- 未决问题 → [open-questions.md](04-open-questions.md)
