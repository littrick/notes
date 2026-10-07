# 核心数据结构（字段级）

> **源码**：[include/linux/sched/walt.h](../../kernel/include/linux/sched/walt.h)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本文档是**字段含义的唯一权威来源**。其他文档需要解释字段时链接到本文对应小节。

---

## 0. 挂载机制：结构体内联在 GKI 预留区

理解所有 `wts = (struct walt_task_struct *) p->android_vendor_data1` 的前提。

### 0.1 预留区的定义

| 结构体 | 预留声明 | 位置 | 大小 |
|---|---|---|---|
| `struct task_struct` | `ANDROID_VENDOR_DATA_ARRAY(1, 64)` | [sched.h:1497](../../kernel/include/linux/sched.h#L1497) | 64 × 8 = **512 B** |
| `struct rq` | `ANDROID_VENDOR_DATA_ARRAY(1, 96)` | [sched.h:1133](../../kernel/kernel/sched/sched.h#L1133) | 96 × 8 = **768 B** |

宏定义在 [android_vendor.h:41-42](../../kernel/include/linux/android_vendor.h#L41-L42)：

```c
#define ANDROID_VENDOR_DATA(n)          u64 android_vendor_data##n
#define ANDROID_VENDOR_DATA_ARRAY(n, s) u64 android_vendor_data##n[s]
```

### 0.2 关键点：不是指针，是内联存储 [反直觉]

因为声明的是 `u64 ...[s]` **数组**，表达式 `p->android_vendor_data1` 会退化为
指向该数组首元素的 `u64 *`。所以：

```c
struct walt_task_struct *wts = (struct walt_task_struct *) p->android_vendor_data1;
```

这一行**不是「从指针取出结构体」，而是「把数组首地址当作结构体地址」**。
WALT 没有为 `walt_task_struct` 分配任何堆内存，它直接**内联**在 task_struct 尾部。

由此产生两个后果：

1. **`wts_to_ts()` 用减法反推 task_struct**
   [include/linux/sched/walt.h:157](../../kernel/include/linux/sched/walt.h#L157)：

   ```c
   #define wts_to_ts(wts) ({ \
           void *__mptr = (void *)(wts); \
           ((struct task_struct *)(__mptr - \
               offsetof(struct task_struct, android_vendor_data1))); })
   ```

   减去字段偏移即得到结构体基址。**这也意味着 `walt_task_struct` 的起始地址
   就是 `&p->android_vendor_data1`**，两者是同一个地址的两种解释。

2. **尺寸必须放得下** [待确认]
   按字段累加估算：`sizeof(struct walt_task_struct)` ≈ **360 B** < 512 B ✓；
   `sizeof(struct walt_rq)` ≈ **583 B** < 768 B ✓。
   两者均有富余，但**没有编译器断言保护**——若后续字段增加越界，
   表现是静默踩踏 task_struct 的相邻字段。这是改动 WALT 结构体时的重点风险。

   > 估算未做实际编译验证（需交叉编译环境），标记为 `[待确认]`。

### 0.3 访问模式全览

| 需要 | 写法 |
|---|---|
| task → wts | `(struct walt_task_struct *)p->android_vendor_data1` |
| wts → task | `wts_to_ts(wts)` |
| cpu → wrq | `(struct walt_rq *)cpu_rq(cpu)->android_vendor_data1` |
| cpu → cluster | `cpu_cluster(cpu)` [walt.h:256](../../kernel/kernel/sched/walt/walt.h#L256) |
| cgroup → wtg | `(struct walt_task_group *)tg->android_vendor_data1` [walt.h:527](../../kernel/kernel/sched/walt/walt.h#L527) |

`struct task_group` 的预留区见 [sched.h:443](../../kernel/kernel/sched/sched.h#L443)
（`ANDROID_VENDOR_DATA_ARRAY(1, 4)` = 32 B，容纳 `struct walt_task_group`）。

> **注意** [反直觉]：task_struct 和 rq 的预留区是**无条件**存在的，
> 但 `task_group` 的这个预留区**被包在 `#ifdef CONFIG_UCLAMP_TASK_GROUP` 里**
> （[sched.h:435-444](../../kernel/kernel/sched/sched.h#L435-L444)）。
> 若该 config 关闭，`tg->android_vendor_data1` 根本不存在，WALT 的 cgroup boost
> 逻辑会编译失败或被条件屏蔽。读 `task_sched_boost()` 时要注意这个前提。

---

## 1. `struct walt_task_struct`（per-task）

定义：[include/linux/sched/walt.h:58](../../kernel/include/linux/sched/walt.h#L58)

共 **51 个字段**，按**功能**分成 6 组（不按源码顺序，便于建立心智模型）。
「消费者」列给出该字段被谁读取——这是理解数据流的关键。

### 1.1 窗口记账（核心状态）

| 字段 | 类型 | 含义 | 生产 | 消费 |
|---|---|---|---|---|
| `mark_start` | `u64` | 当前事件（唤醒 / 开始执行 / 被抢占）在窗口内的起点 | `walt_update_task_ravg` | 同左，算 `delta` |
| `window_start` | `u64` | 上次滚动任务窗口时的 CPU 时刻 | `rollover_task_window` | 判断是否需要滚动 |
| `sum` | `u32` | **当前窗口**内可运行时间（含等待），已频率归一化 | `update_task_demand` | `update_history` |
| `sum_history[5]` | `u32[]` | 过去 `RAVG_HIST_SIZE=5` 个窗口的 `sum`，环形缓冲。**完全睡眠的窗口被忽略** | `update_history` :1984 | `demand` 计算、`get_pred_busy` |
| `sum_history_util[5]` | `u16[]` | 上者按 1024 归一化的版本 | `update_history` | `get_pred_busy` :1256 |
| `demand` | `u32` | 过去 5 窗口 `sum` 的**最大值** | `update_history` | `demand_scaled` |
| `demand_scaled` | `u16` | `demand` 归一到 1024 单位 | `update_task_demand` :2127 | **placement**（`task_util()`）、调频 |
| `last_win_size` | `u64` | 上次窗口大小，用于窗口 size 变化时的一致性处理 | `walt_update_task_ravg` | 同左 |
| `active_time` | `u64` | 任务活跃时间累计 | `walt_update_task_ravg` | 统计 |

> **`sum` 与 `demand` 的区别**：`sum` 是**本窗口实测**，`demand` 是**历史峰值**。
> 前者瞬时、后者保守。WALT 用 `demand` 做决策就是为了「不低估突发」。

### 1.2 CPU 忙时贡献（迁移簿记的核心）

| 字段 | 类型 | 含义 |
|---|---|---|
| `curr_window_cpu[8]` | `u32[]` | 本窗口内，任务对**各个 CPU** 忙时的贡献 |
| `prev_window_cpu[8]` | `u32[]` | 上一窗口的同上 |
| `curr_window` | `u32` | `curr_window_cpu[]` 之和 |
| `prev_window` | `u32` | `prev_window_cpu[]` 之和 |

**为什么需要 per-CPU 数组**：任务迁移时，它在**旧 CPU** 上累计的忙时
必须从该 CPU 的 `rq` 负载中扣除、加到新 CPU 上。逐 CPU 记账让这件事可以精确完成，
而不必重算整个窗口。

- `WALT_NR_CPUS = 8`（[include/linux/sched/walt.h:40](../../kernel/include/linux/sched/walt.h#L40)）
  —— 这是**编译期上限**，不是实际核数

### 1.3 需求预测

| 字段 | 类型 | 含义 |
|---|---|---|
| `busy_buckets[16]` | `u8[]` | 16 桶直方图，把历史忙时按桶位量化 |
| `bucket_bitmask` | `u16` | 桶位掩码，快速判断哪些桶非空。**`NUM_BUSY_BUCKETS` 改动时必须同步** |
| `pred_demand_scaled` | `u16` | 预测的下一窗口忙时（1024 单位） |
| `unfilter` | `u32` | 未滤波的预测值 |
| `iowaited` | `bool` | 任务是否正在 iowait |

细节见 [02-demand-prediction.md](../02-walt/02-demand-prediction.md)。

### 1.4 迁移与放置簿记

| 字段 | 类型 | 含义 |
|---|---|---|
| `prev_cpu` | `int` | 上次运行的 CPU |
| `new_cpu` | `int` | 本次选定的 CPU |
| `enqueue_after_migration` | `u8` | 迁移后是否已入队，避免重复记账 |
| `pipeline_cpu` | `int` | pipeline 任务绑定的 CPU |
| `prev_on_rq` | `int` | 入队/出队状态机：`0` 无 / `1` 已入队 / `2` 已出队 |
| `prev_on_rq_cpu` | `int` | 上者对应的 CPU |
| `cpus_requested` | `cpumask_t` | 任务请求的 CPU 亲和集快照 |
| `misfit` | `bool` | 是否被标记为 misfit（放不下） |

> `prev_on_rq` 是**一致性校验**字段，用于检测 enqueue/dequeue 配对被破坏的错误条件，
> 不是调度决策的输入。

### 1.5 分组 / 优先级 / boost

| 字段 | 类型 | 含义 |
|---|---|---|
| `grp` | `struct walt_related_thread_group __rcu *` | 所属 RTG |
| `grp_list` | `struct list_head` | 挂进 `grp->tasks` 的链表节点 |
| `rtg_high_prio` | `bool` | RTG 内高优先级成员 |
| `coloc_demand` | `u32` | 参与共置（colocation）的 demand。消费者：RTG 聚合 [walt.c:2958](../../kernel/kernel/sched/walt/walt.c#L2958) |
| `boost` | `int` | per-task boost 值 |
| `boost_period` / `boost_expires` | `u64` | boost 有效期，超时自动清 `boost`（见 `per_task_boost` [walt.h:655](../../kernel/kernel/sched/walt/walt.h#L655)） |
| `load_boost` | `int` | 负载侧 boost |
| `boosted_task_load` | `int64_t` | boost 期间的任务负载 |
| `init_load_pct` | `u32` | 新建任务的初始负载百分比 |
| `low_latency` | `u8` | 低延迟任务标志位掩码（4 种来源，见下） |
| `wake_up_idle` | `bool` | 唤醒时倾向 idle CPU |

`low_latency` 位定义于 [walt.h:55-58](../../kernel/kernel/sched/walt/walt.h#L55-L58)：

| 位 | 名称 | 含义 |
|---|---|---|
| `BIT(0)` | `WALT_LOW_LATENCY_PROCFS` | 用户态通过 procfs 设置 |
| `BIT(1)` | `WALT_LOW_LATENCY_BINDER` | binder 通信标记 |
| `BIT(2)` | `WALT_LOW_LATENCY_PIPELINE` | pipeline 任务 |
| `BIT(3)` | `WALT_LOW_LATENCY_HEAVY` | heavy 任务 |

`WALT_LOW_LATENCY_MASK = PIPELINE | HEAVY`——即 pipeline/heavy 是**无条件**低延迟，
而 PROCFS/BINDER 还需要满足 `task_util < sysctl_walt_low_latency_task_threshold`
（见 `walt_low_latency_task` [walt.h:451](../../kernel/kernel/sched/walt/walt.h#L451)）。

### 1.6 MVP（Most-Valuable-Task）与统计

| 字段 | 类型 | 含义 |
|---|---|---|
| `mvp_list` | `struct list_head` | MVP 队列节点 |
| `mvp_prio` | `int` | MVP 优先级，`-1` 表示不是 MVP |
| `cidx` | `int` | 簇内索引 |
| `sum_exec_snapshot_for_slice` | `u64` | 时间片起点快照 |
| `sum_exec_snapshot_for_total` | `u64` | 累计执行时间快照 |
| `total_exec` | `u64` | 累计执行时间 |
| `last_sleep_ts` / `last_wake_ts` / `last_enqueued_ts` | `u64` | 时间戳 |
| `cpu_cycles` | `u64` | 周期计数，用于频率归一化 |
| `hung_detect_status` | `u8` | 卡死检测状态 |
| `mark_start_birth_ts` | `u64` | 任务出生时间戳 |
| `flags` | `u32` | 通用标志位（`enum walt_flags`，目前只有 `WALT_INIT`） |

MVP 优先级常量 [walt.h:967-978](../../kernel/kernel/sched/walt/walt.h#L967-L978)：

```c
#define WALT_MVP_SLICE      3000000U          /* 3ms */
#define WALT_MVP_LIMIT      (4 * WALT_MVP_SLICE)
#define WALT_RTG_MVP        0     /* 数值越大优先级越高 */
#define WALT_BINDER_MVP     1
#define WALT_TASK_BOOST_MVP 2
#define WALT_LL_PIPE_MVP    3
#define WALT_NOT_MVP        -1
#define is_mvp(wts) (wts->mvp_prio != WALT_NOT_MVP)
```

> **易混**：`WALT_RTG_MVP = 0` 是**最低**的 MVP 优先级，
> 但 `0` 又常被当作「无优先级」。判空必须用 `is_mvp()` / `WALT_NOT_MVP`，
> **不能用 `mvp_prio != 0`**。

---

## 2. `struct walt_rq`（per-CPU）

定义：[walt.h:98](../../kernel/kernel/sched/walt/walt.h#L98)

### 2.1 簇与拓扑

| 字段 | 类型 | 含义 |
|---|---|---|
| `cluster` | `struct walt_sched_cluster *` | 所属簇，placement/LB 的最基本判据 |
| `freq_domain_cpumask` | `struct cpumask` | 同频域的 CPU 集合，`same_freq_domain()` 使用 |
| `walt_stats` | `struct walt_sched_stats` | 见 §3 |
| `walt_flags` | `unsigned long` | 标志位，目前仅 `CPU_RESERVED`（[walt.h:898](../../kernel/kernel/sched/walt/walt.h#L898)） |

### 2.2 窗口与负载聚合

| 字段 | 类型 | 含义 |
|---|---|---|
| `window_start` | `u64` | CPU 侧窗口起点 |
| `prev_window_size` | `u32` | 上一个窗口长度 |
| `curr_runnable_sum` | `u64` | 本窗口所有任务的可运行时间和 |
| `prev_runnable_sum` | `u64` | 上一窗口同上 |
| `nt_curr_runnable_sum` | `u64` | 本窗口**新任务**（new task）的可运行时间和 |
| `nt_prev_runnable_sum` | `u64` | 上一窗口同上 |
| `old_busy_time` | `u64` | 滚动时保存的旧忙时 |
| `old_estimated_time` | `u64` | 旧估计时间 |

**`nt_*` 的用途**：新建任务在第一个窗口内的负载会「凭空出现」，
在迁移/均衡计算中会造成跳变。把新任务负载单独记账，可以在统计时扣除，
避免因 fork 抖动触发误判。

### 2.3 分组时间

| 字段 | 类型 | 含义 |
|---|---|---|
| `grp_time` | `struct group_cpu_time` | 与 `curr/prev_runnable_sum` 同构，但只统计 RTG 成员。见 §4 |

### 2.4 迁移簿记

| 字段 | 类型 | 含义 |
|---|---|---|
| `load_subs[2]` | `struct load_subtractions` | 两个窗口的迁移减法记账，见 §5 |
| `push_task` | `struct task_struct *` | active balance 推的任务 |

### 2.5 top task 追踪

| 字段 | 类型 | 含义 |
|---|---|---|
| `top_tasks_bitmap[2][BITS_TO_LONGS(1000)]` | 位图 | 两个窗口的 top task 位图（`NUM_LOAD_INDICES = 1000`） |
| `top_tasks[2]` | `u8 *` | 按负载索引指向任务 |
| `curr_table` | `u8` | 当前使用哪张表 |
| `prev_top` / `curr_top` | `int` | 两个窗口的 top 索引 |

> 位图大小 `2 × 16 × 8 = 256 B`，是 `walt_rq` 中最大的成员。
> `cpu_array`（容量排序的 CPU 表）等的构建依赖 top_tasks。

### 2.6 调频与 IRQ

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_exec_scale` | `u64` | 当前 CPU 的执行缩放因子 |
| `cycles` | `u64` | 周期计数 |
| `last_cc_update` | `u64` | 上次周期计数更新时间 |
| `util` | `u64` | 利用率缓存 |
| `latest_clock` | `u64` | 最新时钟快照 |
| `avg_irqload` | `u64` | 平均 IRQ 负载 |
| `last_irq_window` | `u64` | 上次 IRQ 统计窗口 |
| `prev_irq_time` | `u64` | 上次 IRQ 时间 |
| `high_irqload` | `bool` | 高 IRQ 负载标志，`sched_cpu_high_irqload()` 读取 |

> `avg_irqload` 与 `high_irqload` 用途不同：
> **placement 用 `high_irqload`**（见注释 [walt.h:698-701](../../kernel/kernel/sched/walt/walt.h#L698-L701)），
> `sched_irqload()` 读的 `avg_irqload` **只给 tracepoint 打印用**。

### 2.7 MVP 与其它

| 字段 | 类型 | 含义 |
|---|---|---|
| `mvp_tasks` | `struct list_head` | MVP 任务队列 |
| `num_mvp_tasks` | `int` | 队列长度 |
| `ed_task` | `struct task_struct *` | early detection 任务 |
| `notif_pending` | `bool` | core_ctl 通知挂起标志 |
| `enqueue_counter` | `u32` | 入队计数 |

---

## 3. `struct walt_sched_stats`（per-CPU 统计聚合）

定义：[walt.h:74](../../kernel/kernel/sched/walt/walt.h#L74)

| 字段 | 类型 | 含义 | 消费方 |
|---|---|---|---|
| `cumulative_runnable_avg_scaled` | `u64` | **CPU 的总需求**（所有任务 `demand_scaled` 之和，增量维护） | `cpu_util()`、placement、LB |
| `pred_demands_sum_scaled` | `u64` | 所有任务 `pred_demand_scaled` 之和 | 调频（预测侧） |
| `nr_big_tasks` | `int` | 大任务数（**`!task_fits_max(p, rq->cpu)`**，即容量相对的 misfit；注意 `max_task_load()` [walt.h:728](../../kernel/kernel/sched/walt/walt.h#L728) 全树零调用者，是死代码） | core_ctl、big task rotation |
| `nr_32bit_big_tasks` | `int` | 32 位大任务数 | placement（32 位任务只能放特定核） |
| `nr_rtg_high_prio_tasks` | `int` | RTG 高优先级任务数 | placement |

**增量维护**是这里的要点：`cumulative_runnable_avg_scaled` 不是每次遍历重算，
而是通过 `fixup_cumulative_runnable_avg()`
（[walt.c:307](../../kernel/kernel/sched/walt/walt.c#L307)）按 `(old, new)` 差值更新。
其调用者是 `fixup_walt_sched_stats_common()`
（[walt.c:342](../../kernel/kernel/sched/walt/walt.c#L342)），
后者在任务入队/出队/需求变化时被调用：

```c
s64 cumulative_runnable_avg_scaled =
    stats->cumulative_runnable_avg_scaled + demand_scaled_delta;
...
if (cumulative_runnable_avg_scaled < 0) {
    /* 异常路径：打点并夹到 0 */
}
```

这段负值保护是**判断「增减配对是否被破坏」的探针**——
如果 trace 里频繁出现该告警，说明 enqueue/dequeue 的记账配对有 bug。

---

## 4. `struct group_cpu_time`（RTG 专用计数器）

定义：[walt.h:85](../../kernel/kernel/sched/walt/walt.h#L85)

| 字段 | 类型 | 含义 |
|---|---|---|
| `curr_runnable_sum` / `prev_runnable_sum` | `u64` | 本/上一窗口中 **RTG 成员**的可运行时间和 |
| `nt_curr_runnable_sum` / `nt_prev_runnable_sum` | `u64` | 同上，新任务部分 |

**与 `walt_rq` 上同名字段的区别**：`walt_rq->curr_runnable_sum` 统计**所有**任务，
`walt_rq->grp_time.curr_runnable_sum` **只统计 RTG 成员**。

用途：当 RTG 活跃时，调频需要按 RTG 的需求而非全体需求来给频率
（即「保证这一组线程的算力」），见 `freq_policy_load()` 中的 `aggr_grp_load`
[walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590)。

---

## 5. `struct load_subtractions`（迁移减法记账）

定义：[walt.h:92](../../kernel/kernel/sched/walt/walt.h#L92)，
`NUM_TRACKED_WINDOWS = 2`

| 字段 | 类型 | 含义 |
|---|---|---|
| `window_start` | `u64` | 该条记录所属窗口 |
| `subs` | `u64` | 已确认的减法量（已生效窗口） |
| `new_subs` | `u64` | 待确认的减法量（当前窗口，可能在滚动时转正） |

### 5.1 解决的问题

任务从 CPU A 迁到 CPU B 时：

1. 它在本窗口已贡献给 A 的忙时需要从 A **扣除**
2. 同时需要**加到** B 上

但如果直接扣，而该任务本窗口后续还在 B 上运行，扣减的时机与
`prev_runnable_sum` / `curr_runnable_sum` 的滚动时机可能错配，导致计数漂移。

`load_subtractions` 的做法是**延迟确认**：把减法记在 `new_subs` 里，
等窗口滚动时再转成 `subs` 生效。每个 CPU 保留 2 条（对应两个被追踪的窗口）。

相关函数：`migrate_busy_time_subtraction` / `migrate_busy_time_addition`
[walt.c:1021](../../kernel/kernel/sched/walt/walt.c#L1021)。

### 5.2 滚动时的应用

`rollover_cpu_window` 中会把 `new_subs` 结算掉并检查下溢
（[walt.c:722-760](../../kernel/kernel/sched/walt/walt.c#L722-L760) 一带），
负值同样会触发异常告警。

---

## 6. `struct walt_sched_cluster`（per-cluster）

定义：[walt.h:138](../../kernel/kernel/sched/walt/walt.h#L138)

| 字段 | 类型 | 含义 |
|---|---|---|
| `load_lock` | `raw_spinlock_t` | 保护 `aggr_grp_load` 等的锁 |
| `list` | `struct list_head` | 挂进全局 `cluster_head` |
| `cpus` | `struct cpumask` | 簇内 CPU |
| `id` | `int` | 簇编号（0 起，越大容量越高） |
| `cur_freq` | `unsigned int` | 当前频率 |
| `max_possible_freq` | `unsigned int` | 硬件支持的最高频率 |
| `max_freq` | `unsigned int` | 受限频约束后的最高频率 |
| `aggr_grp_load` | `u64` | **该簇所有 RTG 的聚合负载**，调频消费（[walt.c:594](../../kernel/kernel/sched/walt/walt.c#L594)） |
| `util_to_cost[1024]` | `unsigned long[]` | util→能耗 查找表，由 EM 预计算 |

遍历宏 [walt.h:253](../../kernel/kernel/sched/walt/walt.h#L253)：

```c
#define for_each_sched_cluster(cluster) \
	list_for_each_entry_rcu(cluster, &cluster_head, list)
```

### 6.1 `cur_freq` / `max_possible_freq` / `max_freq` 三者的区别 [反直觉]

源码注释（[walt.h:143-146](../../kernel/kernel/sched/walt/walt.h#L143-L146)）：

- `max_possible_freq` = **硬件**支持的最高
- `max_freq` = **cpufreq 限频后**的最高（热限频、用户限频都会改这个）

做归一化时用哪个很关键：`sched_cpu_legacy_freq()` 用的是 `max_possible_freq`，
而容量计算通常关心 `max_freq`。写实验脚本时若混淆，会得到系统性偏差。

### 6.2 `util_to_cost` 的两段式构建

- 构建：`create_util_to_cost()` [walt_cfs.c:42](../../kernel/kernel/sched/walt/walt_cfs.c#L42)
  → 遍历 perf domain 调 `create_util_to_cost_pd()`
- 消费：`get_util_to_cost()` [walt_cfs.c:716](../../kernel/kernel/sched/walt/walt_cfs.c#L716)，
  可被 `sysctl_em_inflate_pct` 放大

`MAX_CLUSTERS = 3`、`MAX_CPUS_PER_CLUSTER = 6`
（[include/linux/sched/walt.h:22-23](../../kernel/include/linux/sched/walt.h#L22-L23)）
——**编译期硬上限**，sm8550 的 1+3+4 拓扑正好用满 3 簇。

---

## 7. `struct walt_related_thread_group`（RTG）

定义：[include/linux/sched/walt.h:46](../../kernel/include/linux/sched/walt.h#L46)

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | `int` | 组 ID（`DEFAULT_CGROUP_COLOC_ID = 1`） |
| `lock` | `raw_spinlock_t` | 保护组内链表 |
| `tasks` | `struct list_head` | 组内任务（`wts->grp_list` 挂这里） |
| `list` | `struct list_head` | 挂进全局 RTG 链表 |
| `skip_min` | `bool` | **跳过最小簇**——组内任务不下放到 LITTLE |
| `rcu` | `struct rcu_head` | RCU 释放 |
| `last_update` | `u64` | 上次更新时间 |
| `downmigrate_ts` | `u64` | 下行迁移时间戳 |
| `start_ktime_ts` | `u64` | 组启动时间戳 |

细节见 [06-groups-and-clusters.md](../02-walt/06-groups-and-clusters.md)。

---

## 8. `struct walt_task_group`（cgroup 侧）

定义：[walt.h:357](../../kernel/kernel/sched/walt/walt.h#L357)，
内联在 `task_group->android_vendor_data1`

| 字段 | 类型 | 含义 |
|---|---|---|
| `colocate` | `bool` | 该 cgroup 的任务是否要求互相共置 |
| `sched_boost_enable[4]` | `bool[]` | 该 cgroup 参与哪几种 boost（索引为 boost 类型） |

初始化：`walt_init_tg()` / `walt_init_topapp_tg()` / `walt_init_foreground_tg()`
[boost.c:22](../../kernel/kernel/sched/walt/boost.c#L22)。

查询：`task_sched_boost()` [walt.h:509](../../kernel/kernel/sched/walt/walt.h#L509)
（有 `FULL_THROTTLE_BOOST` 快速路径，跳过 cgroup 查找）。

---

## 9. 常量速查

| 常量 | 值 | 定义位置 | 说明 |
|---|---:|---|---|
| `WALT_NR_CPUS` | 8 | [sched/walt.h:40](../../kernel/include/linux/sched/walt.h#L40) | CPU 编译期上限 |
| `RAVG_HIST_SIZE` | 5 | [sched/walt.h:41](../../kernel/include/linux/sched/walt.h#L41) | 历史窗口数 |
| `NUM_BUSY_BUCKETS` | 16 | [sched/walt.h:43](../../kernel/include/linux/sched/walt.h#L43) | 预测直方图桶数 |
| `MAX_CLUSTERS` | 3 | [sched/walt.h:23](../../kernel/include/linux/sched/walt.h#L23) | 簇上限 |
| `MAX_CPUS_PER_CLUSTER` | 6 | [sched/walt.h:22](../../kernel/include/linux/sched/walt.h#L22) | 单簇 CPU 上限 |
| `NUM_TRACKED_WINDOWS` | 2 | [walt.h:82](../../kernel/kernel/sched/walt/walt.h#L82) | 追踪窗口数 |
| `NUM_LOAD_INDICES` | 1000 | [walt.h:83](../../kernel/kernel/sched/walt/walt.h#L83) | top_tasks 索引空间 |
| `DEFAULT_SCHED_RAVG_WINDOW` | 16 ms / 16.67 ms | [walt.h:27](../../kernel/kernel/sched/walt/walt.h#L27) | 见 [CONVENTIONS.md §4.1](../CONVENTIONS.md) |
| `MAX_SCHED_RAVG_WINDOW` | 1 s | [walt.h:31](../../kernel/kernel/sched/walt/walt.h#L31) | 窗口上限 |
| `WALT_MVP_SLICE` | 3 ms | [walt.h:967](../../kernel/kernel/sched/walt/walt.h#L967) | MVP 时间片 |
| `DEFAULT_CGROUP_COLOC_ID` | 1 | [walt.h:746](../../kernel/kernel/sched/walt/walt.h#L746) | 默认共置组 ID |
| `WALT_MANY_WAKEUP_DEFAULT` | 1000 | [walt.h:242](../../kernel/kernel/sched/walt/walt.h#L242) | many-wakeup 阈值 |

---

## 10. 相关文档

- hook 如何读写这些结构 → [integration-model.md](02-integration-model.md)
- 数据何时被更新 → [data-flow.md](04-data-flow.md)
- 各字段驱动的算法 → [02-walt/](../02-walt/)
