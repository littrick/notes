# 术语表

> **源码**：[walt.h](../../kernel/include/linux/sched/walt.h)、[walt.c](../../kernel/kernel/sched/walt/walt.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本表收录 WALT 笔记中反复出现的术语。每条给出
**符号 / 定义位置 / 含义 / 关键常量**四要素。

> **行号会漂移，符号名不会**。重定位时用表里的符号名
> `grep -n '符号名' 文件`。

---

## 0. 先看这个：容易搞错的名字

WALT 的命名有几处「俗称 ≠ 真名」。**下表左边是口头常说的，
右边才是代码里真实存在的符号**——写文档或搜索时务必用右边。

| 常被写成 | 真实符号 | 说明 |
|---|---|---|
| `pred_demand` | **`pred_demand_scaled`** | 没有未 scaled 的 `pred_demand` 字段 |
| `nt_cpu_time` / `nt_cpu_active_time` | **不存在** | `nt_` 前缀只有 `nt_curr_runnable_sum` / `nt_prev_runnable_sum` |
| `migr_scale` | **不存在** | 全树零命中 |
| `walt_get_top_task()` | **不存在** | top task 由 `wrq->top_tasks[]` + `top_tasks_bitmap` 维护 |
| `wts->top` | **不存在** | 无此字段 |
| `max_task_load()` | 存在但是**死代码** | [walt.h:728](../../kernel/kernel/sched/walt/walt.h#L728)，全树零调用者 |
| `walt_update_task_ravg_stats()` | **`fixup_cumulative_runnable_avg()`** | [walt.c:307](../../kernel/kernel/sched/walt/walt.c#L307) |
| `core_ctl_attrs[]` | **`default_attrs[]`** | [core_ctl.c:453](../../kernel/kernel/sched/walt/core_ctl.c#L453) |

> ftrace 事件目录是 **`events/schedwalt/`**（`TRACE_SYSTEM = schedwalt`），
> **不是** `events/sched/`。

---

## 1. 全局概念

### WALT（Window Assisted Load Tracking）

| 项 | 值 |
|---|---|
| 含义 | 用「过去 N 个**固定长度窗口**内的累计可运行时间」替代 PELT 的指数衰减平均，作为调度决策的负载信号 |
| 窗口长度 | `sched_ravg_window`，默认 `DEFAULT_SCHED_RAVG_WINDOW = 16000000`（16 ms） |
| 历史窗口数 | `RAVG_HIST_SIZE = 5`（[walt.h:41](../../kernel/include/linux/sched/walt.h#L41)） |
| 入口 | `walt_update_task_ravg()`（[walt.c:4775](../../kernel/kernel/sched/walt/walt.c#L4775)，**六种事件**）|

**重要**：WALT 是**旁路**（bypass）而非替换——PELT 本体完整保留并继续运行。

---

### 窗口（window）

固定长度的时间片。所有 `curr_*` 计数器在窗口边界
**滚动**为 `prev_*`，`curr_*` 清零。

| 项 | 值 |
|---|---|
| 滚动函数 | `walt_window_rollover()` / `update_window_start()` |
| 触发 | 每个 CPU 的 tick（`walt_cfs_tick()`）|
| 后果 | **统计量只在滚动后才稳定**——窗口中途读到的值不完整 |

→ [01-window-model.md](../02-walt/01-window-model.md)

---

### demand / demand_scaled

| 符号 | 类型 | 含义 |
|---|---|---|
| `wts->demand` | `u32` | 历史 `RAVG_HIST_SIZE`(5) 个窗口 `sum` 的**最大值**（WALT 时间单位）|
| `wts->demand_scaled` | `u16` | 同一 demand **折算到 0–1024 满刻度** |

- 折算函数 `scale_time_to_util()`，调用点 [walt.c:2029](../../kernel/kernel/sched/walt/walt.c#L2029)
- 时间归一化 `scale_exec_time()` [walt.c:1566](../../kernel/kernel/sched/walt/walt.c#L1566)
  ——按 `wrq->task_exec_scale >> SCHED_CAPACITY_SHIFT` 缩放，
  共置（colocated）时另乘 `load_boost`
- `demand_scaled` 的增量是喂给 per-rq 聚合
  `cumulative_runnable_avg_scaled` 的输入（[walt.c:347-353](../../kernel/kernel/sched/walt/walt.c#L347-L353)）

> **为什么两个都要有**：`demand` 是精确的窗口原始量（用于比较阈值），
> `demand_scaled` 是跨容量/频率可比的口径（用于调频与聚合）。
> 两者排序**可能不一致**——见 [open-questions.md](04-open-questions.md)。

---

### pred demand（需求预测）

| 项 | 值 |
|---|---|
| **真实字段** | **`wts->pred_demand_scaled`**（[walt.h:109](../../kernel/include/linux/sched/walt.h#L109)）|
| 含义 | 用 **16 桶直方图 + 泄漏积分器**外推的**下一个窗口**忙时（1024 满刻度）|
| 桶数 | `NUM_BUSY_BUCKETS = 16`，`NUM_BUSY_BUCKETS_SHIFT = 4` |
| 桶数组 | `wts->busy_buckets[16]` + `wts->bucket_bitmask`（u16）|
| 桶索引 | `busy_to_bucket()` [walt.c:1220](../../kernel/kernel/sched/walt/walt.c#L1220) |
| 桶索引公式 | `bidx = normalized_rt >> (SCHED_CAPACITY_SHIFT - NUM_BUSY_BUCKETS_SHIFT)` = `>> 6`，再 `min(bidx, 15)`，且 `bidx==0` 时钳到 1（合并最低两桶）|
| 直方图步长 | `INC_STEP 8` / `DEC_STEP 2` / `CONSISTENT_THRES 16` / `INC_STEP_BIG 16` |
| 重算函数 | `update_task_pred_demand()` [walt.c:1321](../../kernel/kernel/sched/walt/walt.c#L1321) |
| 聚合字段 | `pred_demands_sum_scaled`（`struct walt_sched_stats`）|

**不变量**：`pred_demand_scaled` 永不**低于**本窗口实际 runtime_scaled
（[walt.c:308](../../kernel/kernel/sched/walt/walt.c#L308)：`if (wts->pred_demand_scaled >= curr_window_scaled)`）。

→ [02-demand-prediction.md](../02-walt/02-demand-prediction.md)

---

### top task / topapp

| 项 | 值 |
|---|---|
| 维护 | per-CPU 表 `wrq->top_tasks[]` + `top_tasks_bitmap`（[walt.h:121-123](../../kernel/kernel/sched/walt/walt.h#L121-L123)）|
| 更新 | `update_top_tasks()` [walt.c:1372](../../kernel/kernel/sched/walt/walt.c#L1372) |
| 滚动 | `rollover_top_tasks()` [walt.c:1471](../../kernel/kernel/sched/walt/walt.c#L1471)，`prev_top` → `curr_top` |
| 取值 | `top_task_load()` [walt.c:557](../../kernel/kernel/sched/walt/walt.c#L557) |
| 用途 | `freq_policy_load()` 中作为**下限**：若 `tt_load > load` 则取 `tt_load`，reason 记 `CPUFREQ_REASON_TT_LOAD`（[walt.c:614-618](../../kernel/kernel/sched/walt/walt.c#L614-L618)）|
| 负载粒度 | `NUM_LOAD_INDICES = 1000`；`sched_load_granule = DEFAULT_SCHED_RAVG_WINDOW / 1000` ≈ 16000 ns |

> **top task 不是一个任务身上的标志位**，而是每个 CPU 维护的一段
> 负载直方图里的最大者。

---

### misfit / big task

| 项 | 值 |
|---|---|
| 判据 | `!task_fits_max(p, rq->cpu)`——**容量相对**的放不下 |
| 计数 | `wrq->walt_stats.nr_big_tasks` |
| 消费方 | `core_ctl`、big task rotation |

→ 参见 [base-vs-walt.md](01-base-vs-walt.md)

---

## 2. 分组与放置

### RTG（Related Thread Group，相关线程组）

| 项 | 值 |
|---|---|
| 结构 | `struct walt_related_thread_group`（[walt.h:46-56](../../kernel/include/linux/sched/walt.h#L46-L56)）|
| 归属指针 | `wts->grp` + `wts->grp_list`（[walt.h:124-125](../../kernel/include/linux/sched/walt.h#L124-L125)）|
| 加入 | `add_task_to_group()` [walt.c:3075](../../kernel/kernel/sched/walt/walt.c#L3075) |
| 成员判定 | `task_in_related_thread_group()`（`wts->grp != NULL`），[walt.h:769](../../kernel/kernel/sched/walt/walt.h#L769) |
| 组池 | `related_thread_groups[MAX_NUM_CGROUP_COLOC_ID]`（[walt.c:2870](../../kernel/kernel/sched/walt/walt.c#L2870)）|
| 上限 | `MAX_NUM_CGROUP_COLOC_ID = 20`（index 0 保留 = 非分组）|

**含义**：把一组任务当作**一个调度单元**，用它们的**合并需求**
决定放置（典型用途：让同一 App 的线程聚到同一个簇）。

---

### colocation（共置）

| 项 | 值 |
|---|---|
| 组 ID | `DEFAULT_CGROUP_COLOC_ID = 1`（[walt.h:746](../../kernel/kernel/sched/walt/walt.h#L746)）|
| 归入该组 | `grp_id = wtg->colocate ? DEFAULT_CGROUP_COLOC_ID : 0`（[walt.c:3308](../../kernel/kernel/sched/walt/walt.c#L3308)）|
| 上迁阈值 | `sched_group_upmigrate` = **20000000**（默认）|
| 下迁阈值 | `sched_group_downmigrate` = **19000000**（默认）|
| 阈值重算 | `walt_update_group_thresholds()` [walt.c:2460](../../kernel/kernel/sched/walt/walt.c#L2460)，由 `sysctl_sched_group_upmigrate_pct`(100) / `_downmigrate_pct`(95) 驱动 |
| 决策 | `update_best_cluster()` [walt.c:2876](../../kernel/kernel/sched/walt/walt.c#L2876)、`_set_preferred_cluster()` [walt.c:2917](../../kernel/kernel/sched/walt/walt.c#L2917) |

**行为**：合并需求 ≥ `upmigrate` → 置 `skip_min`；
要掉到 `downmigrate` **以下**（且满足迟滞）才取消。

---

### skip_min

| 项 | 值 |
|---|---|
| 字段 | `struct walt_related_thread_group::skip_min` |
| 含义 | 「这个 RTG 的合并需求已经足够高，**放置时跳过小核**」 |
| 置位 | `update_best_cluster()` [walt.c:2889-2894](../../kernel/kernel/sched/walt/walt.c#L2889-L2894) |
| 读取 | `walt_task_skip_min()`（[walt.h:891](../../kernel/kernel/sched/walt/walt.h#L891)）|
| 迟滞 | `sysctl_sched_coloc_downmigrate_ns`、`sysctl_sched_hyst_min_coloc_ns` |

**注意**：处于 boost 时 `skip_min` 被强制清零
（[walt.c:2884](../../kernel/kernel/sched/walt/walt.c#L2884)），
因为「boost 本身就会把任务推到大核」。

---

### MVP（Most Valuable Task）

| 项 | 值 |
|---|---|
| 含义 | 在 rq 上按优先级**独占** CPU 的任务；高优先级 MVP 可抢占低优先级 MVP |
| 队列 | `rq->mvp_tasks` + `num_mvp_tasks`（[walt.h:132-133](../../kernel/kernel/sched/walt/walt.h#L132-L133)）|
| 优先级判定 | `walt_get_mvp_task_prio()` [walt_cfs.c:1227](../../kernel/kernel/sched/walt/walt_cfs.c#L1227) |
| 优先级判据 | `is_mvp(wts)` = `wts->mvp_prio != WALT_NOT_MVP`（[walt.h:978](../../kernel/kernel/sched/walt/walt.h#L978)）|

**四级优先级**（[walt.h:971-976](../../kernel/kernel/sched/walt/walt.h#L971-L976)，**数值越大优先级越高**）：

| 常量 | 值 | 来源 |
|---|---|---|
| `WALT_RTG_MVP` | 0 | 属于某 RTG |
| `WALT_BINDER_MVP` | 1 | Binder 事务 |
| `WALT_TASK_BOOST_MVP` | 2 | `TASK_BOOST_STRICT_MAX` |
| `WALT_LL_PIPE_MVP` | 3 | pipeline 低延迟任务（最高）|
| `WALT_NOT_MVP` | −1 | 非 MVP |

**时间片**：`WALT_MVP_SLICE = 3000000`（3 ms），
`WALT_MVP_LIMIT = 4 * WALT_MVP_SLICE = 12000000`（12 ms）。
**Binder MVP 只给一个 slice，其它 MVP 给满 limit**
（`walt_cfs_mvp_task_limit()` [walt_cfs.c:1245](../../kernel/kernel/sched/walt/walt_cfs.c#L1245)）。

→ [09-rt-mvp.md](../02-walt/09-rt-mvp.md)

---

### pipeline task / heavy task（低延迟流水线 / 重任务）

两者共用 `WALT_LOW_LATENCY_*` 标志位体系：

| 常量 | 值 | 位置 |
|---|---|---|
| `WALT_LOW_LATENCY_PIPELINE` | `BIT(2)` | [walt.h:57](../../kernel/kernel/sched/walt/walt.h#L57) |
| `WALT_LOW_LATENCY_HEAVY` | `BIT(3)` | [walt.h:58](../../kernel/kernel/sched/walt/walt.h#L58) |
| `WALT_LOW_LATENCY_MASK` | 二者之或 | [walt.h:60](../../kernel/kernel/sched/walt/walt.h#L60) |

**pipeline**：

| 项 | 值 |
|---|---|
| 谓词 | `walt_pipeline_low_latency_task()` [walt.h:481](../../kernel/kernel/sched/walt/walt.h#L481) |
| 加入 / 移除 | `add_pipeline()` [walt.c:3591](../../kernel/kernel/sched/walt/walt.c#L3591) / `remove_pipeline()` [walt.c:3621](../../kernel/kernel/sched/walt/walt.c#L3621) |
| 支撑数组 | `pipeline_wts[WALT_NR_CPUS]` / `pipeline_nr`（[walt.c:3585](../../kernel/kernel/sched/walt/walt.c#L3585)）|
| 专属 CPU | `wts->pipeline_cpu`（[walt.h:143](../../kernel/include/linux/sched/walt.h#L143)）|
| 触发 | procfs 写入 [sysctl.c:338-357](../../kernel/kernel/sched/walt/sysctl.c#L338-L357) |
| 与 MVP 关系 | pipeline 任务 → `WALT_LL_PIPE_MVP`（最高 MVP 优先级），[walt_cfs.c:1230](../../kernel/kernel/sched/walt/walt_cfs.c#L1230) |

**heavy**：

| 项 | 值 |
|---|---|
| 旋钮 | `sysctl_sched_heavy_nr`（[sysctl.c:85](../../kernel/kernel/sched/walt/sysctl.c#L85)，**无初值 → 默认 0**）|
| 选取 | `find_heaviest_topapp()` [walt.c:3669](../../kernel/kernel/sched/walt/walt.c#L3669) |
| 重排 | `rearrange_heavy()` [walt.c:3809](../../kernel/kernel/sched/walt/walt.c#L3809) |
| 判据 | 在 colocation RTG 内、且最近有运行（`mark_start` 在 `2 * sched_ravg_window` 内）、按 `demand_scaled` 取前 `sysctl_sched_heavy_nr` 名 |
| 效果 | 打上 `WALT_LOW_LATENCY_HEAVY`，各分配一个专属大核，避免互相挤占 |
| 节流 | 重排间隔不小于 100 ms（[walt.c:3683](../../kernel/kernel/sched/walt/walt.c#L3683)）|

> **默认关闭**：`sysctl_sched_heavy_nr` 默认 0，即 heavy 机制默认不生效。

→ [06-groups-and-clusters.md](../02-walt/06-groups-and-clusters.md)

---

### fastpath（选核快速通道）

`enum fastpaths`（[walt_cfs.c:325-331](../../kernel/kernel/sched/walt/walt_cfs.c#L325-L331)）。

| 枚举值 | 触发条件 | 触发点 |
|---|---|---|
| `NONE` | 未命中任何快路 | — |
| `SYNC_WAKEUP` | `sysctl_sched_sync_hint_enable` 且 `sync` 且 `bias_to_this_cpu()` | [walt_cfs.c:1016](../../kernel/kernel/sched/walt/walt_cfs.c#L1016) |
| `PREV_CPU_FASTPATH` | prev_cpu 在起始簇内（或 asym-cap 兄弟核）、active、idle、allowed、未 halt | [walt_cfs.c:432](../../kernel/kernel/sched/walt/walt_cfs.c#L432) |
| `CLUSTER_PACKING_FASTPATH` | `walt_find_and_choose_cluster_packing_cpu()` 返回打包 CPU | [walt_cfs.c:419](../../kernel/kernel/sched/walt/walt_cfs.c#L419) |
| `PIPELINE_FASTPATH` | pipeline 任务且 `pipeline_cpu` 有效、active、未 halt | [walt_cfs.c:986](../../kernel/kernel/sched/walt/walt_cfs.c#L986) |

**SYNC_WAKEUP 的例外**：若同一 RTG 内**已有任务在目标 CPU 上**，
同步快路会被**主动作废**（[walt_cfs.c:1009](../../kernel/kernel/sched/walt/walt_cfs.c#L1009)）——
这是为了保住共置语义，宁可放弃快路也要把同组任务聚到一起。

→ [04-placement.md](../02-walt/04-placement.md)

---

### energy_eval_needed

| 项 | 值 |
|---|---|
| 位置 | `walt_get_indicies()` [walt_cfs.c:221](../../kernel/kernel/sched/walt/walt_cfs.c#L221) |
| 置 false | :235 / :240 / :251 / :322（boost、FULL_THROTTLE、uclamp、RTG `skip_min`、iowait-RTG 等）|
| 为 false 时 | **不算能量**，改用 `capacity_spare_of()` 取「剩余容量最大」的核 |
| 归零阈值 | `MIN_UTIL_FOR_ENERGY_EVAL = 52`（[walt_cfs.c:220](../../kernel/kernel/sched/walt/walt_cfs.c#L220)）——`task_util(p) < 52` 时跳过 |

> **与原生 EAS 的关键差异**：原生 EAS **总是**算能量；
> WALT 会在上述场景**完全跳过能量模型**。

→ [04-placement.md](../02-walt/04-placement.md)（`energy_eval_needed` 的四条清零路径）

---

### sync wakeup / wake_affine

WALT **完全不用** `wake_affine()`（它在 `select_task_rq_fair()` 内部，
而 WALT 的 hook 在函数最前面就接管了）。WALT 用自己的
`SYNC_WAKEUP` fastpath + `bias_to_this_cpu()` 表达「唤醒者与被唤醒者同核」的偏好。

---

## 3. 电源与频率

### core_ctl

| 项 | 值 |
|---|---|
| 结构 | `struct cluster_data`（[core_ctl.c:26-56](../../kernel/kernel/sched/walt/core_ctl.c#L26-L56)）|
| 需求计算 | `eval_need()` [core_ctl.c:889](../../kernel/kernel/sched/walt/core_ctl.c#L889) → 写 `need_cpus` |
| 作动 | `do_core_ctl()` [core_ctl.c:1378](../../kernel/kernel/sched/walt/core_ctl.c#L1378) |
| 工作线程 | `try_core_ctl()` [core_ctl.c:1419](../../kernel/kernel/sched/walt/core_ctl.c#L1419)（**单线程**）|
| 计数 | `active_cpus`（当前未 pause 的核数）、`need_cpus` |
| 手段 | `try_to_pause()` / `try_to_resume()` |

**已知 bug**：`eval_need_32bit()` [core_ctl.c:966](../../kernel/kernel/sched/walt/core_ctl.c#L966)
在 [core_ctl.c:1012](../../kernel/kernel/sched/walt/core_ctl.c#L1012) 写的是 `need_cpus`
而非 `need_32bit_cpus`。见 [open-questions.md Q-03](04-open-questions.md)。

→ [07-power-side.md](../02-walt/07-power-side.md)

---

### halt（软件隔离）

| 项 | 值 |
|---|---|
| 掩码 | `__cpu_halt_mask`（[walt_halt.c:16](../../kernel/kernel/sched/walt/walt_halt.c#L16)），访问宏 `cpu_halt_mask` / `cpu_halted()`（[walt.h:1008-1009](../../kernel/kernel/sched/walt/walt.h#L1008-L1009)）|
| 入口 | `walt_halt_cpus()` [walt_halt.c:380](../../kernel/kernel/sched/walt/walt_halt.c#L380) / `walt_start_cpus()` [walt_halt.c:420](../../kernel/kernel/sched/walt/walt_halt.c#L420) |
| 内部 | `halt_cpus()` [walt_halt.c:276](../../kernel/kernel/sched/walt/walt_halt.c#L276)、`walt_pause_cpus()` [walt_halt.c:411](../../kernel/kernel/sched/walt/walt_halt.c#L411) |
| 排空 | `halt_drain_rqs` kthread（`try_drain_rqs`），由 `walt_halt_init()` [walt_halt.c:584](../../kernel/kernel/sched/walt/walt_halt.c#L584) 创建 |
| 挂载点 | `set_cpus_allowed_by_task` :509 / `rto_next_cpu` :542 / `is_cpu_allowed` :559 |
| 暂停原因 | `enum pause_reason`（[walt.h:14-18](../../kernel/include/linux/sched/walt.h#L14-L18)）：`PAUSE_CORE_CTL=0x01`、`PAUSE_THERMAL=0x02`、`PAUSE_HYP=0x04` |

**halt ≠ offline**：

| | hotplug / offline | halt |
|---|---|---|
| online mask | **改变** | **不变** |
| 成本 | 高（走 CPU 上下线流程）| 低（改一个 cpumask 位）|
| 可逆性 | 慢 | 快 |
| 任务 | 需迁移 | **先排空** rq 再置位 |

证据：`cpu_active_mask & ~cpu_halt_mask`
（[walt.h:1023](../../kernel/kernel/sched/walt/walt.h#L1023)）——halt 后的核**仍是 active 的**。

---

### rotation（大任务轮转）

| 项 | 值 |
|---|---|
| 函数 | `walt_lb_check_for_rotation()` [walt_lb.c:134](../../kernel/kernel/sched/walt/walt_lb.c#L134) |
| work | `struct walt_lb_rotate_work`（[walt_lb.c:89-95](../../kernel/kernel/sched/walt/walt_lb.c#L89-L95)），执行 `walt_lb_rotate_work_func()` [walt_lb.c:99](../../kernel/kernel/sched/walt/walt_lb.c#L99) |
| 动作 | **`migrate_swap()`**——互换，不是单向迁移 |
| 阈值 | `WALT_ROTATION_THRESHOLD_NS = 16000000`（16 ms，[walt_lb.c:133](../../kernel/kernel/sched/walt/walt_lb.c#L133)）|
| 开关 | `sysctl_sched_walt_rotate_big_tasks`（[sysctl.c:62](../../kernel/kernel/sched/walt/sysctl.c#L62)）|
| 使能 | `walt_rotation_enabled = nr_big >= num_possible_cpus()`，且 `sched_boost_type == NO_BOOST`（[walt.c:4260-4270](../../kernel/kernel/sched/walt/walt.c#L4260-L4270)）|

**含义**：小核上等得最久的饥饿任务，与一个大核上**已连续运行超过 16 ms**
的任务**互换位置**——让前者得到大核的机会。
`rotation` 本质是「公平性补偿」，不是负载均衡。

→ [05-load-balance.md](../02-walt/05-load-balance.md)

---

### grp_time

| 项 | 值 |
|---|---|
| 结构 | `struct group_cpu_time`（[walt.h:85-90](../../kernel/kernel/sched/walt/walt.h#L85-L90)）|
| 字段 | `wrq->grp_time`（[walt.h:119](../../kernel/kernel/sched/walt/walt.h#L119)）|
| 四元组 | `curr_runnable_sum` / `prev_runnable_sum` / `nt_curr_runnable_sum` / `nt_prev_runnable_sum`（均 `u64`）|
| 分流逻辑 | [walt.c:1719-1728](../../kernel/kernel/sched/walt/walt.c#L1719-L1728)——当 `wts->grp` 非空时，四个累加器**改指向** `&wrq->grp_time` |
| 滚动 | [walt.c:1608-1627](../../kernel/kernel/sched/walt/walt.c#L1608-L1627) |

**含义**：把 **RTG 任务的忙时**与**普通任务的忙时**分开记账。
`grp_time` 记 RTG 的，`wrq` 自己的记非 RTG 的。
调频时 `freq_policy_load()` 会把 `prev_runnable_sum + aggr_grp_load`
一起看（**取 max，不是求和**）。

→ [03-cpufreq.md](../02-walt/03-cpufreq.md)

---

### nt_*（new task 记账）

| 符号 | 含义 |
|---|---|
| `nt_curr_runnable_sum` / `nt_prev_runnable_sum` | per-rq（[walt.h:117-118](../../kernel/kernel/sched/walt/walt.h#L117-L118)）|
| 同上，per-group | `struct group_cpu_time` 内（[walt.h:88-89](../../kernel/kernel/sched/walt/walt.h#L88-L89)）|

| 项 | 值 |
|---|---|
| 新任务判据 | `is_new_task()` [walt.c:1017](../../kernel/kernel/sched/walt/walt.c#L1017)：`wts->active_time < NEW_TASK_ACTIVE_TIME` |
| 阈值 | `NEW_TASK_ACTIVE_TIME = 100000000` ns（**100 ms**，[walt.c:51](../../kernel/kernel/sched/walt/walt.c#L51)）|
| 调频读出 | 报告为 `nl`（[walt.c:654-655](../../kernel/kernel/sched/walt/walt.c#L654-L655)）|
| 预测中排除 | `get_pred_busy()` 跳过 new task（[walt.c:1268](../../kernel/kernel/sched/walt/walt.c#L1268)）|

> **只有这两个 `nt_` 符号**。`nt_cpu_time` / `nt_cpu_active_time` / `nt_cpu_times`
> 在本内核树中**不存在**。

---

### WALT_CPUFREQ_* （调频标志位）

[walt.h:321-326](../../kernel/kernel/sched/walt/walt.h#L321-L326)：

| 常量 | 值 | 含义 |
|---|---|---|
| `WALT_CPUFREQ_ROLLOVER` | 0x1 | 窗口滚动触发 |
| **`WALT_CPUFREQ_CONTINUE`** | **0x2** | **「本簇还有 CPU 没上报」→ 本 CPU 先别算频率** |
| `WALT_CPUFREQ_IC_MIGRATION` | 0x4 | inter-cluster 迁移 |
| `WALT_CPUFREQ_PL` | 0x8 | 预测负载参与 |
| `WALT_CPUFREQ_EARLY_DET` | 0x10 | early detection |
| `WALT_CPUFREQ_BOOST_UPDATE` | 0x20 | boost 状态变化 |

**`WALT_CPUFREQ_CONTINUE` 的原理**（重点）：
per-CPU 回调会对簇内**每个** CPU 各调一次。若每次都算一次频率，
`waltgov_next_freq_shared()` 在前几个 CPU 上看到的是**没更新完**的 util。
所以 [walt.c:4089-4093](../../kernel/kernel/sched/walt/walt.c#L4089-L4093) 给
**除最后一个之外**的所有 CPU 打上该标志，
最终只有**最后一个 CPU** 真正计算并下发频率
（[cpufreq_walt.c:437-438](../../kernel/kernel/sched/walt/cpufreq_walt.c#L437-L438)）。

> 顺带一提：`wg_cpu->reasons = reason` 是**赋值不是按位或**
> （在 `waltgov_walt_adjust()` 里经 `max_and_reason()` 选择），
> 所以 reasons **只保留最后一次**的 boost 原因。

→ [cpufreq-diff.md](02-cpufreq-diff.md)

---

### fast_switch vs deferred update

| | `waltgov_fast_switch()` | `waltgov_deferred_update()` |
|---|---|---|
| 位置 | [cpufreq_walt.c:193](../../kernel/kernel/sched/walt/cpufreq_walt.c#L193) | [cpufreq_walt.c:202](../../kernel/kernel/sched/walt/cpufreq_walt.c#L202) |
| 方式 | `cpufreq_driver_fast_switch()` **同步**改频 | 排 `irq_work`，由 kthread 稍后 `__cpufreq_driver_target()` |
| 上下文 | 任意（不需进程上下文）| 需要可睡眠上下文 |
| 选择 | `policy->fast_switch_enabled` 为真 | 否则（[cpufreq_walt.c:444-447](../../kernel/kernel/sched/walt/cpufreq_walt.c#L444-L447)）|

---

### task boost（per-task 提升）

| 项 | 值 |
|---|---|
| 存储 | `wts->boost`（+ `boost_period` / `boost_expires`）|
| 读取 | `per_task_boost()` [walt.h:655](../../kernel/kernel/sched/walt/walt.h#L655)——**过期自动清零** |
| 设置 | `set_task_boost()` [walt.c:109](../../kernel/kernel/sched/walt/walt.c#L109) |

| 常量 | 值 | 含义 |
|---|---|---|
| `TASK_BOOST_NONE` | 0 | 无 |
| `TASK_BOOST_ON_MID` | 1 | 至少上中核 |
| `TASK_BOOST_ON_MAX` | 2 | 至少上大核 |
| `TASK_BOOST_STRICT_MAX` | 3 | **只**上最大簇（且映射到 `WALT_TASK_BOOST_MVP`）|
| `TASK_BOOST_END` | 4 | 哨兵 |

> **取值判据的坑**：`walt_get_indicies()` 用的是
> `per_task_boost > TASK_BOOST_ON_MID`，
> 所以 **`TASK_BOOST_ON_MID` 本身不会触发强路径**。
> 见 [04-placement.md](../02-walt/04-placement.md)。

---

## 4. 一个便捷索引：常量速查

| 常量 | 值 | 含义 |
|---|---|---|
| `DEFAULT_SCHED_RAVG_WINDOW` | 16000000 ns | 默认窗口长（16 ms）|
| `RAVG_HIST_SIZE` | 5 | demand 取最大值的窗口数 |
| `NUM_BUSY_BUCKETS` | 16 | 预测直方图桶数 |
| `NUM_BUSY_BUCKETS_SHIFT` | 4 | 桶索引移位 |
| `NUM_LOAD_INDICES` | 1000 | top task 负载粒度 |
| `NEW_TASK_ACTIVE_TIME` | 100000000 ns | 「新任务」判定阈值（100 ms）|
| `MAX_NUM_CGROUP_COLOC_ID` | 20 | RTG 组数上限 |
| `DEFAULT_CGROUP_COLOC_ID` | 1 | colocation 组 ID |
| `WALT_MVP_SLICE` / `WALT_MVP_LIMIT` | 3 ms / 12 ms | MVP 时间片 / 上限 |
| `WALT_ROTATION_THRESHOLD_NS` | 16000000 ns | rotation 阈值（16 ms）|
| `MIN_UTIL_FOR_ENERGY_EVAL` | 52 | 低于此 util 不做能量评估 |
| `sched_group_upmigrate` | 20000000 | 共置上迁阈值 |
| `sched_group_downmigrate` | 19000000 | 共置下迁阈值 |
| `SCHED_CAPACITY_SHIFT` | 10 | 容量刻度（1024 满）|
| `sysctl_sched_heavy_nr` | 0（默认关）| heavy 任务个数 |

---

## 5. 相关文档

- 架构与易错点 → [01-architecture.md](../00-overview/01-architecture.md)
- 字段级数据结构 → [03-data-structures.md](../00-overview/03-data-structures.md)
- 基线 vs WALT 对照 → [base-vs-walt.md](01-base-vs-walt.md)
- 未决问题 → [open-questions.md](04-open-questions.md)
