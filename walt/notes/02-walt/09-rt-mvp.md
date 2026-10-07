# RT 任务处理与 MVP 抢占队列

> **源码**：[walt_rt.c](../../kernel/kernel/sched/walt/walt_rt.c)、[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)、[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt_lb.c](../../kernel/kernel/sched/walt/walt_lb.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本文回答两个常被混为一谈的问题：RT（`SCHED_FIFO`/`SCHED_RR`）任务在 WALT 里怎么被放置和记账？
MVP（Most-Valuable-Task）抢占队列是什么、任务怎么被标成 MVP？

先给四条结论，避免读者走错路：

- [反直觉] **MVP 的全部实现都在 `walt_cfs.c`（第 1220-1545 行），不在 `walt.c`**。
  `walt.c` 只负责数据结构初始化、enqueue/dequeue 时的转发、以及 yield 时的清理。
- [反直觉] **WALT 没有「RT 专属负载计数器」**。RT 任务走的是和 CFS 同一条 ravg 记账路径
  （`walt_update_task_ravg()`），最终累计进同一个 `cumulative_runnable_avg_scaled`。
- [反直觉] **`walt_rt.c` 只有 380 行，且不负责 RT 拉取**——RT pull 在 `walt_lb.c`。
- [反直觉] **`wts->rtg` 这个字段不存在**。RTG 指针叫 `wts->grp`。

字段含义见 [03-data-structures.md](../00-overview/03-data-structures.md)，hook 注册与调用点见
[02-integration-model.md](../00-overview/02-integration-model.md)，窗口与 ravg 记账见
[window-model.md](01-window-model.md)，RT 的原生实现见
[01-sched-framework.md](../01-baseline/01-sched-framework.md)。

---

## 1. 为什么 RT 需要 Vendor 接管

WALT 的原始假设是：**CFS 任务需要被预测，RT 不需要**——RT 有固定优先级，`cpupri` 已经能选出
「优先级上最合适」的 CPU。但在 sm8550 这类非对称 SoC 上，「prio 高」不等于「跑得快」：
小核上的 FIFO prio 50 远慢于大核上的 prio 10，而 `cpupri_find_fitness()` 只看 fitness 掩码，
不看 `cpu_util_cum` / idle exit latency。于是 Vendor 接管了三处：

| 需求 | 上游机制 | WALT 的接管点 |
|---|---|---|
| RT 唤醒选核 | `select_task_rq_rt()` [rt.c:1518](../../kernel/kernel/sched/rt.c#L1518) | `android_rvh_select_task_rq_rt` |
| RT push 选核 | `find_lowest_rq()` [rt.c:1844](../../kernel/kernel/sched/rt.c#L1844) | `android_rvh_find_lowest_rq` |
| 空闲核把别人的 RT 拉过来 | `rt_queue_pull_task()` | `walt_balance_rt()`（在 `walt_lb.c`） |

顺带解决了 RT 的经典问题：**一个 RT 线程卡住不睡，整条链路的 latency 就崩了**——
这就是第 2.6 节的 `long_running_rt_task` 检测。

---

## 2. `walt_rt.c` 逐块拆解

### 2.1 注册点

`walt_rt_init()` [walt_rt.c:366](../../kernel/kernel/sched/walt/walt_rt.c#L366) 做两件事：
为每个 CPU 分配一份 `walt_local_cpu_mask`（选核函数持 rq 锁，不能用全局共享 mask），
然后注册两个 hook：

```c
/* walt_rt_init() */
register_trace_android_rvh_select_task_rq_rt(walt_select_task_rq_rt, NULL);
register_trace_android_rvh_find_lowest_rq(walt_rt_find_lowest_rq, NULL);
```

调用者是 `walt_init()` [walt.c:5097](../../kernel/kernel/sched/walt/walt.c#L5097)，
紧跟其后就是 `walt_cfs_init()` [walt.c:5098](../../kernel/kernel/sched/walt/walt.c#L5098)。

> **只有这两个**。`android_rvh_sched_balance_rt`、`android_vh_sched_stat_runtime_rt`、
> `android_rvh_update_rt_rq_load_avg` 三个 RT 相关 hook 在本树里**存在但 WALT 没有注册**
> （`grep -rn register_trace_ kernel/kernel/sched/walt/*.c` 可验证）。

### 2.2 `walt_select_task_rq_rt()`：四条快路径

[walt_rt.c:234](../../kernel/kernel/sched/walt/walt_rt.c#L234)。上游无条件调用这个 hook，
**hook 一旦把 `*new_cpu` 写成非负值，上游就 `return target_cpu`，完全跳过自己的逻辑**。
函数用 `enum rt_fastpaths` [walt_rt.c:226](../../kernel/kernel/sched/walt/walt_rt.c#L226) 打标记，
只用于 trace `sched_select_task_rt`。

**① `NON_WAKEUP`** [walt_rt.c:250](../../kernel/kernel/sched/walt/walt_rt.c#L250)：
`sd_flag` 既不是 `SD_BALANCE_WAKE` 也不是 `SD_BALANCE_FORK` 时直接 `goto out`，不改 `*new_cpu`。

**② `SYNC_WAKEUP`** [walt_rt.c:261](../../kernel/kernel/sched/walt/walt_rt.c#L261)：在
`sysctl_sched_sync_hint_enable` 打开、当前 CPU active 未 halt、且在 `task->cpus_ptr` 内、
且 `walt_should_honor_rt_sync()` 成立时，直接选当前 CPU。
`walt_should_honor_rt_sync()` [walt_rt.c:218](../../kernel/kernel/sched/walt/walt_rt.c#L218)：

```c
return sync && p->prio <= rq->rt.highest_prio.next && rq->rt.rt_nr_running <= 2;
```

注释 [walt_rt.c:215-217](../../kernel/kernel/sched/walt/walt_rt.c#L215-L217) 点明与上游的差别：
上游只在 **waker 也是 RT** 时才认 sync 语义，WALT **不管 waker 是 CFS 还是 RT** 都认——
这正是 Android binder sync 链（CFS 线程唤醒 FIFO 线程）需要的。

**③ `CLUSTER_PACKING_FASTPATH`** [walt_rt.c:303](../../kernel/kernel/sched/walt/walt_rt.c#L303)：
`walt_find_and_choose_cluster_packing_cpu()`（inline，
[walt.h:1023](../../kernel/kernel/sched/walt/walt.h#L1023)）在**当前 CPU 所属 cluster 内**
找一个未 halt 的 active CPU（32 位任务还要落进 32 位子集），找到就把 `*new_cpu` 定下。
受 `sysctl_sched_idle_enough` 与 `sysctl_sched_cluster_util_thres_pct` 门控，任一为 0 就关掉。

**④ 慢路径** [walt_rt.c:299-331](../../kernel/kernel/sched/walt/walt_rt.c#L299-L331)：
先 `cpupri_find_fitness(..., walt_rt_task_fits_capacity)` 得到候选 `lowest_mask`，
再交给 `walt_rt_energy_aware_wake_cpu()`，最后：

```c
if (target != -1 &&
    (may_not_preempt || task->prio < cpu_rq(target)->rt.highest_prio.curr))
        *new_cpu = target;
```

- `walt_rt_task_fits_capacity()` [walt_rt.c:194](../../kernel/kernel/sched/walt/walt_rt.c#L194)
  只在 `CONFIG_UCLAMP_TASK` 下真判断（`cpu_cap >= min(uclamp_min, uclamp_max)`），
  否则 [walt_rt.c:208](../../kernel/kernel/sched/walt/walt_rt.c#L208) 恒返回 `true`。
- `may_not_preempt` 来自 `task_may_not_preempt()`，用于绕开「当前是 long softirq」的 rq。
- 尾部兜底 [walt_rt.c:323](../../kernel/kernel/sched/walt/walt_rt.c#L323)：若选中的 CPU 被
  WALT 的 halt 机制停掉，就从 `task->cpus_ptr - cpu_halt_mask` 取第一个。

### 2.3 `walt_rt_energy_aware_wake_cpu()`：RT 选核打分

[walt_rt.c:94](../../kernel/kernel/sched/walt/walt_rt.c#L94)。遍历顺序由
`rt_boost_on_big()` [walt.h:498](../../kernel/kernel/sched/walt/walt.h#L498) 决定
（真 = `FULL_THROTTLE_BOOST` 且 `SCHED_BOOST_ON_BIG`，此时 `order_index = 1` 从大簇开扫）。

候选过滤（[walt_rt.c:122-132](../../kernel/kernel/sched/walt/walt_rt.c#L122-L132)）：
`cpu_active` / `!cpu_halted` / `!sched_cpu_high_irqload` / `!__cpu_overutilized(cpu, tutil)`。
打分是四级的 [walt_rt.c:136-183](../../kernel/kernel/sched/walt/walt_rt.c#L136-L183)：

| 级 | 判据 |
|---|---|
| 1 | `lt` 优先：`lt = walt_low_latency_task(cpu_rq(cpu)->curr) \|\| walt_nr_rtg_high_prio(cpu)` [walt_rt.c:136](../../kernel/kernel/sched/walt/walt_rt.c#L136) |
| 2 | `cpu_util(cpu)` 更小者优先 [walt_rt.c:150](../../kernel/kernel/sched/walt/walt_rt.c#L150) |
| 3 | idle exit latency 更浅者优先（`walt_get_idle_exit_latency()`）[walt_rt.c:167](../../kernel/kernel/sched/walt/walt_rt.c#L167) |
| 4 | `cpu_util_cum(cpu)` 更小者优先（决胜）[walt_rt.c:169](../../kernel/kernel/sched/walt/walt_rt.c#L169) |

> [反直觉] 第 1 级里的 `walt_nr_rtg_high_prio(cpu)` [walt.h:762](../../kernel/kernel/sched/walt/walt.h#L762)
> 读的是 `wrq->walt_stats.nr_rtg_high_prio_tasks` [walt.h:79](../../kernel/kernel/sched/walt/walt.h#L79)，
> 而这个计数**只在 CFS 任务 enqueue/dequeue 时维护**（被 `walt_fair_task()` 门控，见 §3.3）。
> 所以纯 RT 负载的 CPU 上它恒为 0，`lt` 只可能由 `walt_low_latency_task(curr)` 点亮。

### 2.4 `walt_rt_find_lowest_rq()`：RT push 的选核

[walt_rt.c:339](../../kernel/kernel/sched/walt/walt_rt.c#L339)，供 `push_rt_task()` 使用。
逻辑与 2.2 慢路径相同，多了个尾部动作：

```c
/* walt_rt_find_lowest_rq() */
if (*best_cpu == -1)
        cpumask_andnot(lowest_mask, lowest_mask, cpu_halt_mask);
```

即使 WALT 自己没找到合适 CPU，也要**先把 halted CPU 摘掉**再交还给上游，
避免 push 目标落在已停机的核上。

### 2.5 RT 拉取在 `walt_lb.c`，不在 `walt_rt.c`

最容易找错地方的一段。[walt_lb.c:723](../../kernel/kernel/sched/walt/walt_lb.c#L723)：

```c
/* walt_balance_rt() */
if (sched_rt_runnable(this_rq)) return false;            /* 自己都有 RT，别抢 */
... for_each_possible_cpu(i) -> has_pushable_tasks(rq)
p = pick_highest_pushable_task(src_rq, this_cpu);
wallclock = max(this_rq->clock, src_rq->clock);
if (wallclock - wts->last_wake_ts < WALT_RT_PULL_THRESHOLD_NS) goto unlock;
deactivate_task(src_rq, p, 0); set_task_cpu(p, this_cpu); activate_task(this_rq, p, 0);
```

三个要点：

1. **唯一的任务级判据是 `wts->last_wake_ts`**，阈值 `WALT_RT_PULL_THRESHOLD_NS = 250000`
   [walt_lb.c:722](../../kernel/kernel/sched/walt/walt_lb.c#L722)，语义是「刚唤醒 250µs 内的
   RT 任务不要横向搬走」。**这里不看 WALT 的 demand。**
2. `[推测]` 用 `max(this_rq->clock, src_rq->clock)` 而非 `rq_clock()`，是因为这段代码跑在
   `__schedule() → pick_next_task()` 路径上，rq clock 刚更新过，再更新会触发 warning
   （源码注释 [walt_lb.c:767-771](../../kernel/kernel/sched/walt/walt_lb.c#L767-L771) 明说了）。
3. 访问受 `double_lock_balance()` 保护，锁释放后**必须重新检查 `sched_rt_runnable()`**
   [walt_lb.c:754](../../kernel/kernel/sched/walt/walt_lb.c#L754)。

调用点在 `walt_newidle_balance()` [walt_lb.c:854](../../kernel/kernel/sched/walt/walt_lb.c#L854)：
只有本 CPU 即将 idle、且 WALT 完全接管 newidle balance 时才拉。
拉取走 `deactivate_task()`/`activate_task()`，会触发 dequeue/enqueue hook，
**于是这次 RT 迁移照样更新 WALT 记账**（见 §3.2）。

另外 `walt_halt.c` 的 `android_rvh_rto_next_cpu()` [walt_halt.c:544](../../kernel/kernel/sched/walt/walt_halt.c#L544)
（注册于 [walt_halt.c:608](../../kernel/kernel/sched/walt/walt_halt.c#L608)）是 **RT push 定时器**
（`rto_next_cpu`）路径上的 hook：rto 选中的 CPU 被 halt 时改选 `cpumask_next()`。

### 2.6 「RT 跑太久」检测：唯一一个 tick hook

`walt_rt.c` 注册了 `android_vh_scheduler_tick`，**而 `walt.c` 也注册了同一个 hook**：

| 文件 | 回调 | 行号 |
|---|---|---|
| `walt.c` | `android_vh_scheduler_tick` | [walt.c:4997](../../kernel/kernel/sched/walt/walt.c#L4997) |
| `walt_rt.c` | `long_running_rt_task_notifier` | [walt_rt.c:85](../../kernel/kernel/sched/walt/walt_rt.c#L85) |

`android_vh_scheduler_tick` 是 `DECLARE_HOOK`（非 restricted），可以挂多个回调
（见 [02-integration-model.md](../00-overview/02-integration-model.md)）。

[反直觉] **`walt_rt.c` 的这个注册是懒的**，不在 `walt_rt_init()` 里，而在 sysctl 写回调里：

```c
/* sched_long_running_rt_task_ms_handler() [walt_rt.c:68] */
if (sysctl_sched_long_running_rt_task_ms > 0 && sysctl_sched_long_running_rt_task_ms < 800)
        sysctl_sched_long_running_rt_task_ms = 800;        /* 强制下限 800ms */
if (write && !long_running_rt_task_trace_rgstrd) {
        register_trace_sched_switch(rt_task_arrival_marker, NULL);
        register_trace_android_vh_scheduler_tick(long_running_rt_task_notifier, NULL);
        long_running_rt_task_trace_rgstrd = true;
}
```

即**默认（sysctl = 0）连 `sched_switch` hook 都不装**，零开销。开启后每个 tick 做两件事：

**① `rt_task_arrival_marker()`**（sched_switch hook，[walt_rt.c:16](../../kernel/kernel/sched/walt/walt_rt.c#L16)）

```c
if (next->policy == SCHED_FIFO && next != cpu_rq(cpu)->stop)
        per_cpu(rt_task_arrival_time, cpu) = rq_clock_task(this_rq());
else
        per_cpu(rt_task_arrival_time, cpu) = 0;
```

[反直觉] **只跟踪 `SCHED_FIFO`，`SCHED_RR` 完全不在覆盖范围内**；并排除了 `stop` 调度类
在 FIFO 策略下的 per-cpu 停机线程。`rt_task_arrival_time` 是 per-CPU 变量，
声明在 [walt.h:1147](../../kernel/kernel/sched/walt/walt.h#L1147)、定义在
[walt_rt.c:13](../../kernel/kernel/sched/walt/walt_rt.c#L13)。

**② `long_running_rt_task_notifier()`**（tick hook，[walt_rt.c:27](../../kernel/kernel/sched/walt/walt_rt.c#L27)）

```c
if (!sysctl_sched_long_running_rt_task_ms) return;
if (!per_cpu(rt_task_arrival_time, cpu))   return;
if (curr->policy != SCHED_FIFO) { per_cpu(rt_task_arrival_time, cpu) = 0; return; } /* 策略中途被改 */
if (rq->clock_task - per_cpu(rt_task_arrival_time, cpu)
        > sysctl_sched_long_running_rt_task_ms * MSEC_TO_NSEC) {
        printk_deferred("RT task %s (%d) runtime > %u ...");
        BUG();                     /* 硬崩，不是 WARN */
}
```

三个细节：用 `rq->clock_task` 而不调 `update_rq_clock_task()`，注释
[walt_rt.c:49-53](../../kernel/kernel/sched/walt/walt_rt.c#L49-L53) 说明是为了避免在 tick 里
触发 rq clock warning；sysctl 上界是 `two_thousand` [sysctl.c:1026](../../kernel/kernel/sched/walt/sysctl.c#L1026)；
**超时是 `BUG()`**——Vendor 认为 RT 长跑是死锁级故障，必须留现场。函数名里的 `_trace_` 有误导性。

### 2.7 与 RT 相关的 sysctl

| sysctl | 定义 | 作用 |
|---|---|---|
| `sched_long_running_rt_task_ms` | [sysctl.c:1020](../../kernel/kernel/sched/walt/sysctl.c#L1020) | 见 2.6；0 = 完全关闭 |
| `sched_sync_hint_enable` | [sysctl.c:855](../../kernel/kernel/sched/walt/sysctl.c#L855) | 门控 2.2 的 `SYNC_WAKEUP` |
| `sched_boost` → `sched_boost_type` / `boost_policy` | `rt_boost_on_big()` [walt.h:498](../../kernel/kernel/sched/walt/walt.h#L498) | 门控 2.3 的 `order_index` |
| `sched_idle_enough`、`sched_cluster_util_thres_pct` | [walt.h:202-203](../../kernel/kernel/sched/walt/walt.h#L202-L203) | 门控 cluster packing 快路径 |

---

## 3. RT 的负载记账：和 CFS 同一条路

### 3.1 没有 RT 专属计数器

`struct walt_rq` [walt.h:98](../../kernel/kernel/sched/walt/walt.h#L98) 里**没有**
`rt_load` / `rt_runnable_sum` 之类的字段。per-CPU 负载聚合只有 `struct walt_sched_stats`
（见 [03-data-structures.md](../00-overview/03-data-structures.md)），RT 的贡献靠任务级字段间接进入。

### 3.2 RT 任务照样进 ravg 与 16 桶

`walt_update_task_ravg()` [walt.c:2288](../../kernel/kernel/sched/walt/walt.c#L2288)
内部**没有任何 policy 判断**，只特判 `is_idle_task(p)` 与 DL 限流任务
（[walt.c:2043](../../kernel/kernel/sched/walt/walt.c#L2043)）。它的调用者也是 policy 无关的：

| 调用者 | 位置 | 覆盖 RT 吗 |
|---|---|---|
| `android_rvh_tick_entry` [walt.c:4790](../../kernel/kernel/sched/walt/walt.c#L4790) | 上游 [core.c:5454](../../kernel/kernel/sched/core.c#L5454) 在 `scheduler_tick()` 里无条件调用 | 是（RT 作为 `rq->curr`） |
| `android_rvh_try_to_wake_up` [walt.c:4760](../../kernel/kernel/sched/walt/walt.c#L4760) | [walt.c:4776](../../kernel/kernel/sched/walt/walt.c#L4776) | 是 |
| `android_rvh_schedule`（`PUT_PREV_TASK`） | [walt.c:4847](../../kernel/kernel/sched/walt/walt.c#L4847) | 是 |

`update_cpu_busy_time()` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678) 累加
`wts->curr_window` 的唯一条件是 `!is_idle_task(p)`：

```c
/* update_cpu_busy_time() */
if (!is_idle_task(p)) {
        wts->curr_window += delta;
        wts->curr_window_cpu[cpu] += delta;
}
```

`update_task_demand()` [walt.c:2127](../../kernel/kernel/sched/walt/walt.c#L2127) 与
`update_history()` [walt.c:1984](../../kernel/kernel/sched/walt/walt.c#L1984) 同样没有 RT 分支，
`predict_and_update_buckets()` [walt.c:1925](../../kernel/kernel/sched/walt/walt.c#L1925)
也不区分调度类。

[反直觉] 源码注释**点名了 RT**——`update_history()` 上方的注释
[walt.c:1978-1983](../../kernel/kernel/sched/walt/walt.c#L1978-L1983)：

> `samples` can be > 1 when, say, **a real-time task runs without preemption
> for several windows at a stretch.**

这句正是为 RT 写的：CFS 任务不可能连续跑满多个窗口不被抢占，**只有 RT 会**。

结论：**RT 任务的 `sum` / `demand` / `demand_scaled` / `pred_demand_scaled` 与它在 16 桶
直方图中的位置，全部由和 CFS 相同的代码算出**，`wts->curr_window` 也一样累加。

### 3.3 `walt_fair_task()` 划出的三条分界线

[walt.h:941](../../kernel/kernel/sched/walt/walt.h#L941)：

```c
static inline bool walt_fair_task(struct task_struct *p)
{
        return p->prio >= MAX_RT_PRIO && !is_idle_task(p);
}
```

它门控了三件事：

| 被门控的动作 | 位置 | 对 RT 的影响 |
|---|---|---|
| `inc_rq_walt_stats()` / `dec_rq_walt_stats()` | [walt.c:4659](../../kernel/kernel/sched/walt/walt.c#L4659)、[walt.c:4712](../../kernel/kernel/sched/walt/walt.c#L4712) | `misfit` 与 `nr_rtg_high_prio_tasks` 计数**不含 RT** |
| `walt_cfs_enqueue_task()` / `walt_cfs_dequeue_task()` | [walt.c:4660](../../kernel/kernel/sched/walt/walt.c#L4660)、[walt.c:4713](../../kernel/kernel/sched/walt/walt.c#L4713) | RT 任务**永远不进 MVP 队列**（见 §4.4） |
| `walt_cfs_tick()` | `walt_lb_tick()` [walt_lb.c:657](../../kernel/kernel/sched/walt/walt_lb.c#L657) | RT 正在跑时 **MVP 的 slice 记账不被驱动** |

**但** `walt_inc_cumulative_runnable_avg()` / `walt_dec_cumulative_runnable_avg()`
（[walt.c:4422](../../kernel/kernel/sched/walt/walt.c#L4422) / [walt.c:4432](../../kernel/kernel/sched/walt/walt.c#L4432)）
**不在 `walt_fair_task()` 分支里**，只受 `double_enqueue` 保护：

```c
/* android_rvh_enqueue_task() [walt.c:4611] */
if (walt_fair_task(p)) { ...; walt_cfs_enqueue_task(rq, p); }

if (!double_enqueue)
        walt_inc_cumulative_runnable_avg(rq, p);
```

所以 **RT 任务的 `demand_scaled` / `pred_demand_scaled` 会被加进 per-CPU 的
`cumulative_runnable_avg_scaled` / `pred_demands_sum_scaled`**。
这就是 WALT 里「RT 的 per-CPU 负载」的真实来源：不是独立计数器，而是
`cumulative_runnable_avg` 里 RT 的那一份，进而通过 `cpu_util_cum()` 影响 CPH、选核与调频。
`[推测]` 这是刻意设计：RT 的 CPU 占用当然应该抬升该核的 util。

### 3.4 断言

`walt_rt.c` 与 MVP 路径上**没有 `WALT_PANIC`**（定义在
[walt.h:1161](../../kernel/kernel/sched/walt/walt.h#L1161)；`WALT_BUG` 在
[walt.h:1199](../../kernel/kernel/sched/walt/walt.h#L1199)）。强断言只有三处：

| 断言 | 位置 | 含义 |
|---|---|---|
| `BUG()` | [walt_rt.c:64](../../kernel/kernel/sched/walt/walt_rt.c#L64) | RT 长跑超时，真 hard BUG |
| `WALT_BUG(UPSTREAM, ...)` | [walt_cfs.c:1507](../../kernel/kernel/sched/walt/walt_cfs.c#L1507)、[walt_cfs.c:1538](../../kernel/kernel/sched/walt/walt_cfs.c#L1538) | `replace_next_task_fair` 前后各查一次 `on_cpu/on_rq/cpu` 一致性——MVP 抢选任务后必须复核不变量 |
| `walt_lockdep_assert_rq(rq, NULL)` | [walt_cfs.c:1302](../../kernel/kernel/sched/walt/walt_cfs.c#L1302) | MVP 记账必须持 rq 锁 |

### 3.5 `wts->rtg` 不存在

RTG 相关的字段是：指针 `wts->grp`（`struct walt_related_thread_group __rcu *`），
读取 helper `task_related_thread_group()` [walt.h:875](../../kernel/kernel/sched/walt/walt.h#L875)；
以及 `bool wts->rtg_high_prio` 缓存 [walt.h:115](../../kernel/include/linux/sched/walt.h#L115)，
由 `inc_rq_walt_stats()` [walt.c:4481](../../kernel/kernel/sched/walt/walt.c#L4481) 刷新，
判据是 `task_rtg_high_prio()` [walt.h:868](../../kernel/kernel/sched/walt/walt.h#L868)：
`task_in_related_thread_group(p) && p->prio <= sysctl_walt_rtg_cfs_boost_prio`
（阈值 sysctl 在 [sysctl.c:827](../../kernel/kernel/sched/walt/sysctl.c#L827)）。

---

## 4. MVP（Most-Valuable-Task）抢占队列

**全部实现位于 `walt_cfs.c` 第 1220-1545 行。** `walt.c` 里只能找到数据结构初始化
与几个转发调用点（§4.7）。

### 4.1 解决的问题

CFS 的 `check_preempt_wakeup()` 用 vruntime 决定抢占。这对 Android 不够：
一个 binder 线程刚从 Binder 驱动唤醒，它的 vruntime 可能远大于当前正在跑的、已经跑了
一整窗口的 rtg 后台任务，按 CFS 规则它不能抢占。
MVP 是一张**绕过 vruntime 的优先队列**：队列里的任务在 `pick_next_task_fair()` 时被直接插队。
与单纯 boost 不同之处在于**有配额**——每个 MVP 只能连续跑 `WALT_MVP_SLICE`，
总计只能跑 `WALT_MVP_LIMIT`，用完降级。

### 4.2 四级 MVP 与配额

[walt.h:967-978](../../kernel/kernel/sched/walt/walt.h#L967-L978)：

```c
#define WALT_MVP_SLICE		3000000U            /* 3ms */
#define WALT_MVP_LIMIT		(4 * WALT_MVP_SLICE) /* 12ms */

/* higher number, better priority */
#define WALT_RTG_MVP		0
#define WALT_BINDER_MVP		1
#define WALT_TASK_BOOST_MVP	2
#define WALT_LL_PIPE_MVP	3

#define WALT_NOT_MVP		-1
#define is_mvp(wts) (wts->mvp_prio != WALT_NOT_MVP)
```

对应关系（依 `walt_get_mvp_task_prio()` [walt_cfs.c:1227](../../kernel/kernel/sched/walt/walt_cfs.c#L1227)
的检查顺序，**前面的先赢**）：

| `mvp_prio` | 值 | 判定 | 配额 |
|---|---|---|---|
| `WALT_LL_PIPE_MVP` | 3（最高） | `walt_procfs_low_latency_task(p) \|\| walt_pipeline_low_latency_task(p)` | 12ms |
| `WALT_TASK_BOOST_MVP` | 2 | `per_task_boost(p) == TASK_BOOST_STRICT_MAX` | 12ms |
| `WALT_BINDER_MVP` | 1 | `walt_binder_low_latency_task(p)` | **仅 3ms** |
| `WALT_RTG_MVP` | 0（最低） | `task_rtg_high_prio(p)` | 12ms |
| `WALT_NOT_MVP` | -1 | 都不满足 | — |

`walt_cfs_mvp_task_limit()` [walt_cfs.c:1245](../../kernel/kernel/sched/walt/walt_cfs.c#L1245)
实现「binder 只给一个 slice」的特例。注释
[walt_cfs.c:1220-1226](../../kernel/kernel/sched/walt/walt_cfs.c#L1220-L1226) 解释动机：

> binders will have higher prio MVP and they can preempt long running rtg prio tasks
> but binders loose their powers within 3 msec where as rtg prio tasks can run more than that.

即 **binder 优先级高但配额小，rtg 任务优先级低但配额大**——让干重活的（work horses）
能长时间跑，让只做唤醒转发的 binder 快速让位。

### 4.3 队列结构

队列是**每 CPU 一条链表**，不是全局：

```c
/* struct walt_rq [walt.h:98] */
struct list_head	mvp_tasks;      /* [walt.h:132] */
int                     num_mvp_tasks;  /* [walt.h:133] */
```

初始化在 `walt_sched_init_rq()` [walt.c:4354](../../kernel/kernel/sched/walt/walt.c#L4354) 尾部
（[walt.c:4407-4408](../../kernel/kernel/sched/walt/walt.c#L4407-L4408)：
`wrq->num_mvp_tasks = 0; INIT_LIST_HEAD(&wrq->mvp_tasks);`）。

任务侧的挂队节点与记账字段在 `struct walt_task_struct`
[walt.h:131-135](../../kernel/include/linux/sched/walt.h#L131-L135)：
`mvp_list`（链表节点）、`mvp_prio`（当前等级，`WALT_NOT_MVP` = 不在队列）、
`sum_exec_snapshot_for_slice`（本次 slice 起点）、`sum_exec_snapshot_for_total`（本轮总起点）、
`total_exec`（本轮累计执行时间）。初始值在 `init_new_task_load()`
[walt.c:2348](../../kernel/kernel/sched/walt/walt.c#L2348) 的
[walt.c:2399-2402](../../kernel/kernel/sched/walt/walt.c#L2399-L2402)。

**排序规则**——`walt_cfs_insert_mvp_task()` [walt_cfs.c:1256](../../kernel/kernel/sched/walt/walt_cfs.c#L1256)
按 `mvp_prio` **降序**插入；`at_front` 时用 `>=` 允许同优先级插到最前：

```c
/* walt_cfs_insert_mvp_task() */
if (at_front) { if (wts->mvp_prio >= tmp_wts->mvp_prio) break; }
else          { if (wts->mvp_prio >  tmp_wts->mvp_prio) break; }
list_add(&wts->mvp_list, pos->prev);
wrq->num_mvp_tasks++;
```

[反直觉] `at_front` 的实参是 `task_running(rq, p)`——**正在跑的任务入队时允许插到
同优先级最前**，因为它已经在 CPU 上，排到同优先级第二个会造成一次无谓的抢占。

### 4.4 入队、出队、slice 记账

**入队** `walt_cfs_enqueue_task()` [walt_cfs.c:1351](../../kernel/kernel/sched/walt/walt_cfs.c#L1351)：

```c
int mvp_prio = walt_get_mvp_task_prio(p);
if (mvp_prio == WALT_NOT_MVP) return;

/* 曾经是 MVP 被降级过 —— 必须睡一觉才能重新获得资格 */
if (wts->total_exec > walt_cfs_mvp_task_limit(p)) return;

wts->mvp_prio = mvp_prio;
walt_cfs_insert_mvp_task(wrq, wts, task_running(rq, p));

if (!wts->total_exec) /* queue after sleep */
        wts->sum_exec_snapshot_for_total = wts->sum_exec_snapshot_for_slice
                                         = p->se.sum_exec_runtime;
```

[反直觉] **降级是有粘性的**：`total_exec > limit` 的任务再次 enqueue 也不进队列，
直到 `walt_cfs_dequeue_task()` [walt_cfs.c:1382](../../kernel/kernel/sched/walt/walt_cfs.c#L1382)
里检测到它真的睡了才清零：

```c
/* walt_cfs_dequeue_task() */
if (!list_empty(&wts->mvp_list) && wts->mvp_list.next)
        walt_cfs_deactivate_mvp_task(rq, p);
if (READ_ONCE(p->__state) != TASK_RUNNING)
        wts->total_exec = 0;
```

`list_del_init()` 之后 `mvp_list.next == NULL`，所以 `!list_empty(&x) && x.next` 就是
「是否真挂在链表上」的廉价探针——**这是本文件反复出现的惯用法**，等价于 `is_mvp`。

**slice 记账** `walt_cfs_account_mvp_runtime()` [walt_cfs.c:1295](../../kernel/kernel/sched/walt/walt_cfs.c#L1295)，
函数头注释 [walt_cfs.c:1289-1294](../../kernel/kernel/sched/walt/walt_cfs.c#L1289-L1294)
写清了三种结局：

```c
if (!(rq->clock_update_flags & RQCF_UPDATED))
        update_rq_clock(rq);                        /* 见下方说明 */

wts->total_exec = curr->se.sum_exec_runtime - wts->sum_exec_snapshot_for_total;
slice           = curr->se.sum_exec_runtime - wts->sum_exec_snapshot_for_slice;

if (slice < WALT_MVP_SLICE) return;                 /* ① slice 未用完 */
wts->sum_exec_snapshot_for_slice = curr->se.sum_exec_runtime;

limit = walt_cfs_mvp_task_limit(curr);
if (wts->total_exec > limit) {                      /* ② 配额耗尽 → 降级 */
        walt_cfs_deactivate_mvp_task(rq, curr);
        trace_walt_cfs_deactivate_mvp_task(curr, wts, limit);
        return;
}
if (wrq->num_mvp_tasks == 1) return;                /* ③ 只有我一个，不用让位 */
list_del(&wts->mvp_list);                           /* ④ slice 到期，重排到队尾 */
wrq->num_mvp_tasks--;
walt_cfs_insert_mvp_task(wrq, wts, false);
```

[反直觉] 它主动调了 `update_rq_clock(rq)`。原因见
[walt_cfs.c:1304-1309](../../kernel/kernel/sched/walt/walt_cfs.c#L1304-L1309)：
vendor hook 是在调度器**释放 rq 锁之后**被调用的，`RQCF_UPDATED` 标志可能已被
另一次 lock/unlock 清掉，所以必须自己复核。

### 4.5 抢占决策

`walt_cfs_check_preempt_wakeup()` [walt_cfs.c:1428](../../kernel/kernel/sched/walt/walt_cfs.c#L1428)，
注册于 [walt_cfs.c:1555](../../kernel/kernel/sched/walt/walt_cfs.c#L1555)：

```c
p_is_mvp    = !list_empty(&wts_p->mvp_list) && wts_p->mvp_list.next;
curr_is_mvp = !list_empty(&wts_c->mvp_list) && wts_c->mvp_list.next;

if (!curr_is_mvp) {
        if (p_is_mvp) goto preempt;              /* 唤醒的是 MVP → 抢 */
        return;                                  /* 都不是 → 交回 CFS */
}
walt_cfs_account_mvp_runtime(rq, c);             /* 当前是 MVP，先结算 slice */
resched = (wrq->mvp_tasks.next != &wts_c->mvp_list);  /* 队首还是我吗 */
if (resched) goto preempt;
*nopreempt = true;                               /* 还是队首 → 不抢 */
```

注释 [walt_cfs.c:1464-1470](../../kernel/kernel/sched/walt/walt_cfs.c#L1464-L1470) 点出关键：
当前任务是因为 MVP 身份被选上来的，**它的 vruntime 早已落后于 CFS 树里的其它任务**，
一旦失去 MVP 身份必须强制 `resched`，让 CFS 重新按 vruntime（并再次考虑 MVP 队列）选一次。

输出参数约定 [walt_cfs.c:1424-1427](../../kernel/kernel/sched/walt/walt_cfs.c#L1424-L1427)：
`*preempt = true` 强制抢，`*nopreempt = true` 禁止抢，**两者都不设**表示交给 CFS 判断。

### 4.6 真正让 MVP 生效的地方

`walt_cfs_replace_next_task_fair()` [walt_cfs.c:1492](../../kernel/kernel/sched/walt/walt_cfs.c#L1492)：

```c
if (list_empty(&wrq->mvp_tasks)) return;         /* 没 MVP，让 CFS 正常选 */
wts = list_first_entry(&wrq->mvp_tasks, struct walt_task_struct, mvp_list);
mvp = wts_to_ts(wts);
*p = mvp; *se = &mvp->se; *repick = true;
if (simple) for_each_sched_entity((*se)) set_next_entity(cfs_rq_of(*se), *se);
```

**只要队列非空，队首任务就无条件替换掉 CFS 选出的任务**——这是 MVP 的临门一脚。
紧随其后（[walt_cfs.c:1507](../../kernel/kernel/sched/walt/walt_cfs.c#L1507) 与
[walt_cfs.c:1538](../../kernel/kernel/sched/walt/walt_cfs.c#L1538)）两次
`WALT_BUG(WALT_BUG_UPSTREAM, ...)`，判据是
`on_cpu == 1 || on_rq == 0 || on_rq == TASK_ON_RQ_MIGRATING || cpu != cpu_of(rq)`。

[待确认] `for_each_sched_entity` + `set_next_entity` 那段带上游 TODO 注释
[walt_cfs.c:1527](../../kernel/kernel/sched/walt/walt_cfs.c#L1527)：
「If CFS_BANDWIDTH is enabled, we might pick from a throttled cfs_rq」。
本树未启用 CFS bandwidth 时无影响，但值得在真机上用 trace 验证一次。

### 4.7 placement 侧的 MVP

`walt_should_reject_fbt_cpu()` [walt_cfs.c:340](../../kernel/kernel/sched/walt/walt_cfs.c#L340)，
被 `walt_find_best_target()` 在 [walt_cfs.c:478](../../kernel/kernel/sched/walt/walt_cfs.c#L478) 调用：

```c
if (wrq->num_mvp_tasks > 0 && per_task_boost(p) != TASK_BOOST_STRICT_MAX)
        return true;         /* 这个 CPU 已经是 MVP 的了，别人别来 */
```

语义：**一个 CPU 上一旦排了 MVP 任务，新唤醒的普通 CFS 任务就不要再放进来**，
唯一例外是 `TASK_BOOST_STRICT_MAX` 的任务。注意它只看 `num_mvp_tasks`（计数），不看优先级。

### 4.8 用户态怎么把任务标成 MVP

**没有专门的 syscall / ioctl。** 标记入口是 `pid value` 两段式 procfs 条目，
统一走 `sched_task_handler()` [sysctl.c:211](../../kernel/kernel/sched/walt/sysctl.c#L211)：

```
echo "<pid> <val>" > /proc/sys/walt/<entry>
```

路径依据：`walt_base_table` 的 `procname = "walt"` [sysctl.c:1094](../../kernel/kernel/sched/walt/sysctl.c#L1094)，
由 `register_sysctl_table(walt_base_table)` [walt.c:5126](../../kernel/kernel/sched/walt/walt.c#L5126) 注册。

| procfs 节点 | 行号 | 写入后 | MVP 等级 |
|---|---|---|---|
| `sched_low_latency` | [sysctl.c:954](../../kernel/kernel/sched/walt/sysctl.c#L954) | `wts->low_latency \|= WALT_LOW_LATENCY_PROCFS` | `WALT_LL_PIPE_MVP` (3) |
| `sched_pipeline` | [sysctl.c:961](../../kernel/kernel/sched/walt/sysctl.c#L961) | `add_pipeline(wts)` + `WALT_LOW_LATENCY_PIPELINE` | `WALT_LL_PIPE_MVP` (3) |
| `sched_per_task_boost` | [sysctl.c:940](../../kernel/kernel/sched/walt/sysctl.c#L940) | `wts->boost = val`（须 `== TASK_BOOST_STRICT_MAX`） | `WALT_TASK_BOOST_MVP` (2) |
| `sched_group_id` | [sysctl.c:933](../../kernel/kernel/sched/walt/sysctl.c#L933) | `sched_set_group_id()`，进入 RTG | `WALT_RTG_MVP` (0)，**还需 prio 够高** |

外加两条**隐式**路径，不需要用户态干预：

- **Binder**：`walt_binder_low_latency_set()` [walt_cfs.c:1165](../../kernel/kernel/sched/walt/walt_cfs.c#L1165)，
  挂 `android_vh_binder_wakeup_ilocked`（[walt_cfs.c:1550](../../kernel/kernel/sched/walt/walt_cfs.c#L1550)）。
  当「waker 在 RTG 里且 target 的 `group_leader` 是 RT prio」或反过来时，
  给 target 打 `WALT_LOW_LATENCY_BINDER`；条件不满足时**清掉**该标志
  （注释 [walt_cfs.c:1178-1186](../../kernel/kernel/sched/walt/walt_cfs.c#L1178-L1186) 说明这处理了
  「一个任务打标、另一个任务复用同一个 binder 线程」的场景）。
- **STRICT_MAX boost 的 binder 事务传播**：`binder_set_priority_hook()` /
  `binder_restore_priority_hook()` [walt_cfs.c:1191](../../kernel/kernel/sched/walt/walt_cfs.c#L1191) /
  [walt_cfs.c:1207](../../kernel/kernel/sched/walt/walt_cfs.c#L1207)，把 `TASK_BOOST_STRICT_MAX`
  顺着一笔 need-reply 事务传给对端，原值存在 `bndrtrans->android_vendor_data1`。

**清理路径**：`walt_do_sched_yield()` [walt.c:4965](../../kernel/kernel/sched/walt/walt.c#L4965)，
挂 `android_rvh_do_sched_yield`（[walt.c:5009](../../kernel/kernel/sched/walt/walt.c#L5009)）：

```c
/* walt_do_sched_yield() */
if (!list_empty(&wts->mvp_list) && wts->mvp_list.next)
        walt_cfs_deactivate_mvp_task(rq, curr);
if (per_cpu(rt_task_arrival_time, cpu_of(rq)))
        per_cpu(rt_task_arrival_time, cpu_of(rq)) = 0;
```

**同一个函数里既清理 MVP 又清理 RT 到达时间戳**——本树里 RT 与 MVP 唯一共享的代码路径。

### 4.9 观测手段

MVP 有 4 个 trace 事件，全部来自同一个模板 `walt_cfs_mvp_task_template`
[trace.h:1315](../../kernel/kernel/sched/walt/trace.h#L1315)，
字段为 `comm/pid/prio/mvp_prio/cpu/exec/limit`：

| 事件 | 触发点 |
|---|---|
| `walt_cfs_deactivate_mvp_task` | 配额耗尽降级 [walt_cfs.c:1338](../../kernel/kernel/sched/walt/walt_cfs.c#L1338) |
| `walt_cfs_mvp_pick_next` | MVP 被选为 next [walt_cfs.c:1543](../../kernel/kernel/sched/walt/walt_cfs.c#L1543) |
| `walt_cfs_mvp_wakeup_nopreempt` | 唤醒的 MVP 没能抢占队首 [walt_cfs.c:1475](../../kernel/kernel/sched/walt/walt_cfs.c#L1475) |
| `walt_cfs_mvp_wakeup_preempt` | 唤醒的 MVP 抢占了当前 [walt_cfs.c:1479](../../kernel/kernel/sched/walt/walt_cfs.c#L1479) |

另外 enqueue/dequeue trace `sched_enq_deq_task` 带 `is_mvp(wts)`
[walt.c:4669](../../kernel/kernel/sched/walt/walt.c#L4669)，可判断「enqueue 瞬间是不是 MVP」。

排障建议：`deactivate_mvp_task` 的 `exec` 必然大于 `limit`
（模板注释 [trace.h:1347](../../kernel/kernel/sched/walt/trace.h#L1347) 明说）。
`mvp_pick_next` 频繁而 `deactivate` 很少说明队列在正常工作；`wakeup_nopreempt` 极多则要查
是不是某个 `WALT_LL_PIPE_MVP` 长期占着队首。

---

## 5. ANDROID_VENDOR_DATA 槽位

WALT 在本树里**只用了 `android_vendor_data1` 一个槽**（`grep -o android_vendor_data[0-9]`
在整个 `sched/walt/` 下 226 处命中，全是 `data1`）：

| 对象 | 存放内容 | 取法 |
|---|---|---|
| `struct task_struct` | `struct walt_task_struct *` | `p->android_vendor_data1` |
| `struct rq` | `struct walt_rq *` | `rq->android_vendor_data1` |
| `struct binder_transaction` | `int`（被保存的 boost 值） | `bndrtrans->android_vendor_data1`，[walt_cfs.c:1202](../../kernel/kernel/sched/walt/walt_cfs.c#L1202) |

前两者是长期占用的指针，第三个是 `binder_set_priority_hook()` 临时借用**同一槽位编号**
存一个 int。三者结构体不同不冲突，但读代码时看到 `android_vendor_data1`
**一定先看左边的类型**。MVP 的字段全在 `struct walt_task_struct` 内，
**没有额外占用 vendor data 槽**。

---

## 6. 相关文档

- 字段定义 → [03-data-structures.md](../00-overview/03-data-structures.md)
- hook 注册与调用点 → [02-integration-model.md](../00-overview/02-integration-model.md)
- 窗口与 ravg 记账 → [window-model.md](01-window-model.md)
- 16 桶直方图与 pred_demand → [demand-prediction.md](02-demand-prediction.md)
- RT 的原生实现（`select_task_rq_rt` / `push_rt_task` / `find_lowest_rq`）→
  [01-sched-framework.md](../01-baseline/01-sched-framework.md)
- 放置与 CPU halt → [placement.md](04-placement.md)
- 未决问题登记 → [04-open-questions.md](../03-comparison/04-open-questions.md)
