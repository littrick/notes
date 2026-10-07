# 数据流动总图

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)、[core_ctl.c](../../kernel/kernel/sched/walt/core_ctl.c)、[cpufreq_walt.c](../../kernel/kernel/sched/walt/cpufreq_walt.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本文档回答一个问题：**一次调度事件，到最终改变 CPU 频率 / 核数 / 任务位置，中间经过了什么？**

字段含义见 [data-structures.md](03-data-structures.md)，hook 注册/调用点见 [integration-model.md](02-integration-model.md)。

---

## 1. 全景图

```
 ┌────────────────────────────────────────────────────────────────────┐
 │ 原生调度器事件（未被 WALT 修改）                                      │
 │  enqueue / dequeue / wake_up / tick / migrate                       │
 └───────────────────────────┬────────────────────────────────────────┘
                             │ trace_android_rvh_* 回调
                             ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │ walt_update_task_ravg(p, rq, event, wallclock, irqtime)   walt.c:2288│
 │  ── 唯一的负载/需求更新入口 ──                                        │
 │  1. update_window_start()          必要时滚动 CPU 窗口                │
 │  2. update_task_rq_cpu_cycles()    采样 CPU 周期计数                  │
 │  3. update_task_demand()           更新 wts->sum / demand / hist     │
 │  4. update_cpu_busy_time()         更新 rq 的 curr/prev_runnable_sum │
 │  5. update_task_pred_demand()      更新 16 桶直方图与 pred_demand     │
 │  6. trace_sched_update_task_ravg() 观测                              │
 │  7. run_walt_irq_work_rollover()   窗口滚动 → 触发全局 irq_work       │
 └───────────────────────────┬────────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │ 路径 A：窗口未滚动            │ 路径 B：窗口滚动
              │ （绝大多数调用）              │ （每窗口一次）
              ▼                             ▼
    ┌──────────────────┐      ┌───────────────────────────────────────┐
    │ 数据就地累积      │      │ run_walt_irq_work_rollover() walt.c:2271│
    │ 无决策发生        │      │  atomic64_cmpxchg 全局去重 → 仅一个CPU触发│
    └──────────────────┘      └───────────────┬───────────────────────┘
                                              │ walt_irq_work_queue()
                                              ▼
                            ┌─────────────────────────────────────────┐
                            │ walt_irq_work()              walt.c:4213 │
                            │  （hard irq 上下文，锁住所有 rq）          │
                            ├─────────────────────────────────────────┤
                            │ __walt_irq_work_locked()     walt.c:3992 │
                            │   per cluster, per cpu:                  │
                            │     waltgov_run_callback(ROLLOVER)       │
                            │       └─▶ 【消费方 1】调频                │
                            ├─────────────────────────────────────────┤
                            │ if (!is_migration):           walt.c:4250│
                            │   find_heaviest_topapp()                 │
                            │   rearrange_heavy()          ← 放置调整   │
                            │   rearrange_pipeline_preferred_cpus()    │
                            │   core_ctl_check()      ← 【消费方 2】核数 │
                            └─────────────────────────────────────────┘
```

**核心结论**：WALT 的所有**决策**都发生在**窗口滚动**这一个时刻，
由 `walt_irq_work` 统一驱动。`walt_update_task_ravg()` 在滚动之外只做数据累积。

【消费方 3】任务 placement 是例外——它不走窗口边界，而是**在唤醒时**同步决策，
见 §4。

---

## 2. 入口：`walt_update_task_ravg()`

定义 [walt.c:2288](../../kernel/kernel/sched/walt/walt.c#L2288)。

### 2.0 谁调用它

不是所有 hook 都直接调它。**记账类 hook** 是它的主要调用者
（完整对照见 [integration-model.md §5](02-integration-model.md#5-完整-hook-表)）：

| 事件 | hook | 回调 |
|---|---|---|
| 入队/出队 | `android_rvh_after_enqueue_task` / `after_dequeue_task` | walt.c:4611 / :4672 |
| 上下文切换 | `android_rvh_schedule` | walt.c:4833 |
| tick | `android_rvh_tick_entry` / `android_vh_scheduler_tick` | walt.c:4790 / :4806 |
| 唤醒 | `android_rvh_try_to_wake_up` | walt.c:4760 |
| 迁移 | `android_rvh_set_task_cpu` | walt.c:4532 |
| IRQ | `android_rvh_account_irq_start` / `_end` | walt.c:4558 / :4582 |

这些回调各自传入不同的 `event` 值（见 §3），进入同一个入口函数。

### 2.1 提前返回

```c
if (!wrq->window_start || wts->mark_start == wallclock)
        return;
```

- `!wrq->window_start`：CPU 的窗口还没初始化
- `mark_start == wallclock`：**同一时刻重复调用**，直接幂等返回

第二条是**性能关键路径**——调度器在一次事件里可能从多个 hook 调用同一个更新，
靠这个判断去重。

### 2.2 首次 vs 后续

```c
if (!wts->mark_start) {
        update_task_cpu_cycles(p, cpu_of(rq), wallclock);
        goto done;              /* 跳过 3/4/5 三个更新 */
}
```

`mark_start == 0` 表示任务刚初始化、还没有基准点。
此时**只采样周期计数**，不做需求/忙时计算——因为任何 `delta` 都无意义。

这解释了为什么 `update_task_demand()` / `update_cpu_busy_time()` 内部
仍要处理「首窗口」情形：它们第一次被真正调用时，历史是空的。

### 2.3 四步更新（严格顺序）

| 步骤 | 函数 | 更新对象 | 文档 |
|---|---|---|---|
| 2 | `update_task_rq_cpu_cycles` | `wrq->cycles`、`wts->cpu_cycles` | 频率归一化的输入 |
| 3 | `update_task_demand` [walt.c:2127](../../kernel/kernel/sched/walt/walt.c#L2127) | `wts->sum/history/demand/demand_scaled` | [01-window-model.md](../02-walt/01-window-model.md) |
| 4 | `update_cpu_busy_time` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678) | `wrq->curr/prev_runnable_sum`、`wts->*_window_cpu` | 同上 |
| 5 | `update_task_pred_demand` [walt.c:1321](../../kernel/kernel/sched/walt/walt.c#L1321) | `wts->busy_buckets/pred_demand_scaled` | [02-demand-prediction.md](../02-walt/02-demand-prediction.md) |

**顺序不可交换**：步骤 3 产出新的 `demand_scaled`，步骤 4 用它做 CPU 侧的增减记账，
步骤 5 依赖步骤 3 已更新的历史。

### 2.4 收尾

```c
done:
        wts->mark_start = wallclock;
        /* 断言：mark_start 不能超前 window_start 超过一个窗口 */
        run_walt_irq_work_rollover(old_window_start, rq);
```

`mark_start` 无条件前移到 `wallclock`——**这是下一个 delta 的起点**。
`mark_start` 超前 `window_start` 超过一个窗口意味着记账错乱，触发 `WALT_BUG`。

---

## 3. 六种事件

`enum task_event` [walt.h:40](../../kernel/kernel/sched/walt/walt.h#L40)：

| 值 | 名称 | 触发时机 |
|---:|---|---|
| 0 | `PUT_PREV_TASK` | 任务被换出（prev）|
| 1 | `PICK_NEXT_TASK` | 任务被选中（next）|
| 2 | `TASK_WAKE` | 任务被唤醒 |
| 3 | `TASK_MIGRATE` | 任务迁移 |
| 4 | `TASK_UPDATE` | 定时更新（tick 等）|
| 5 | `IRQ_UPDATE` | IRQ 时间更新 |

【反直觉】事件类型**不只是标签**——`update_task_demand()` 内部按事件分成
三条完全不同的处理路径（见 [01-window-model.md](../02-walt/01-window-model.md)）。
把 `TASK_WAKE` 和 `PUT_PREV_TASK` 当成同一类处理是常见错误。

---

## 4. 消费方 1：调频（窗口滚动驱动）

这是 WALT 与原生 schedutil 最大的**结构差异**。

### 4.1 全局去重

`run_walt_irq_work_rollover()` [walt.c:2271](../../kernel/kernel/sched/walt/walt.c#L2271)：

```c
if (old_window_start == wrq->window_start)
        return;                                    /* 本 rq 窗口没动 */

result = atomic64_cmpxchg(&walt_irq_work_lastq_ws, old_window_start,
                           wrq->window_start);
if (result == old_window_start) {                  /* 只有第一个成功的 CPU 进入 */
        walt_irq_work_queue(&walt_cpufreq_irq_work);
        trace_walt_window_rollover(wrq->window_start);
}
```

**设计意图**：每个 CPU 的窗口是独立滚动的，但**调频需要全局视角**
（要比较各簇负载、做频率聚合）。用一次 `cmpxchg` 把「第一个滚动到新窗口的 CPU」
选为触发者，保证**每个窗口只做一次全局调频**。

`walt_irq_work_lastq_ws` 是全局原子变量（[walt.c:5013](../../kernel/kernel/sched/walt/walt.c#L5013) 一带）。

### 4.2 调频回调

`__walt_irq_work_locked()` [walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992)
遍历簇与 CPU：

```c
if (i == num_cpus)
        waltgov_run_callback(cpu_rq(cpu), wflag);
else
        waltgov_run_callback(cpu_rq(cpu), wflag | WALT_CPUFREQ_CONTINUE);
```

`WALT_CPUFREQ_CONTINUE` 标志让 governor 知道「后面还有 CPU 要处理」，
用于做**频率聚合**（同一簇内各 CPU 取一致频率）。

`waltgov_run_callback()` 是内联函数 [walt.h:402](../../kernel/kernel/sched/walt/walt.h#L402)，
取出 per-CPU 注册的回调并调用：

```c
cb = rcu_dereference_sched(*per_cpu_ptr(&waltgov_cb_data, cpu_of(rq)));
if (cb)
        cb->func(cb, walt_sched_clock(), flags);
```

回调由 `cpufreq_walt.c` 通过 `waltgov_add_callback()` [walt.h:383](../../kernel/kernel/sched/walt/walt.h#L383) 注册，
最终进入 `waltgov_walt_adjust()` [cpufreq_walt.c:305](../../kernel/kernel/sched/walt/cpufreq_walt.c#L305)。

详见 [03-cpufreq.md](../02-walt/03-cpufreq.md)。

### 4.3 与 schedutil 的对比

| | 原生 schedutil | WALT governor |
|---|---|---|
| 触发源 | `cpufreq_update_util()`，事件驱动 | **窗口滚动**，时间驱动 |
| 触发频率 | 每次 util 变化 | 每窗口一次（~20ms）|
| 全局性 | 每 CPU 独立 | 跨簇统一决策 |
| 回调注册 | `cpufreq_set_policy` | `waltgov_add_callback` |

**推论**：WALT 调频的响应延迟下界是**一个窗口**。这是它相对 schedutil
在突发负载上可能更慢、但在稳态上更平稳的根本原因。[推测]

---

## 5. 消费方 2：核数（`core_ctl`）

同一次 `walt_irq_work` 中，非迁移路径的末尾：

```c
if (!is_migration) {
        wrq = (struct walt_rq *) this_rq()->android_vendor_data1;
        find_heaviest_topapp(wrq->window_start);
        rearrange_heavy(wrq->window_start);
        rearrange_pipeline_preferred_cpus(wrq->window_start);
        core_ctl_check(wrq->window_start);      /* walt.c:4256 */
}
```

`core_ctl_check()` [core_ctl.c:1129](../../kernel/kernel/sched/walt/core_ctl.c#L1129)
内部有**同窗口去重**：

```c
if (window_start == core_ctl_check_timestamp)
        return;
core_ctl_check_timestamp = window_start;
```

源码注释给出了契约（[core_ctl.c:1121-1128](../../kernel/kernel/sched/walt/core_ctl.c#L1121-L1128)）：

> `sched_get_nr_running_avg` will wipe out previous statistics...
> `core_ctl_check` assumes that the statistics are stable, hence window based.
> Therefore `core_ctl_check` must only be called from window rollover, or
> `walt_irq_work` for not migration.

即：**统计量在窗口内是脏的，只有滚动后才稳定**。任何在窗口中途调用
`core_ctl_check` 的尝试都会读到被 wipe 掉的统计。

详见 [07-power-side.md](../02-walt/07-power-side.md)。

---

## 6. 消费方 3：placement（事件驱动，非窗口驱动）

放置决策**不**在窗口边界发生。它在**任务唤醒时**同步完成：

```
try_to_wake_up()  [core.c:4110]
   └─ select_task_rq()
        └─ 【hook】android_rvh_select_task_rq_fair
             └─ walt_select_task_rq_fair()   walt_cfs.c:1149
                  └─ walt_find_energy_efficient_cpu()  walt_cfs.c:933
```

读的是**当前**的 `wts->demand_scaled` 与 `wrq->walt_stats.cumulative_runnable_avg_scaled`
——即「上一窗口结算后的值」。

**这带来一个微妙的一致性窗口**：唤醒发生时，CPU 侧窗口可能已经滚动、
而任务侧还没滚动（反之亦然）。WALT 用 `wts->window_start` 与
`wrq->window_start` 的对比来处理这种不一致。

详见 [04-placement.md](../02-walt/04-placement.md)。

---

## 7. 三条路径对照

| | 调频 | 核数 | placement |
|---|---|---|---|
| 触发 | 窗口滚动 | 窗口滚动 | 任务唤醒/迁移 |
| 驱动 | `walt_irq_work` | `walt_irq_work` | `select_task_rq` hook |
| 上下文 | hard irq | hard irq | 进程上下文 |
| 频率 | 每窗口 1 次（全局去重）| 每窗口 1 次 | 每次唤醒 |
| 入口 | `waltgov_run_callback` | `core_ctl_check` | `walt_select_task_rq_fair` |
| 主要读 | `curr/prev_runnable_sum`、`aggr_grp_load` | `nr_big_tasks`、`cumulative_runnable_avg_scaled` | `demand_scaled`、`cumulative_runnable_avg_scaled` |

---

## 8. 一句话时序

```
tick/wake/migrate
   → hook → walt_update_task_ravg()            [累积数据]
        → 窗口边界？
             ├─ 否 → 返回
             └─ 是 → 第一个 CPU 触发 irq_work
                       → walt_irq_work()
                            ├─ waltgov_run_callback → 调频
                            ├─ rearrange_*()        → 放置修正
                            └─ core_ctl_check()     → 核数
```

---

## 9. 相关文档

- hook 具体挂在哪些内核函数上 → [integration-model.md](02-integration-model.md)
- 各字段定义 → [data-structures.md](03-data-structures.md)
- 算法细节 → [02-walt/](../02-walt/)
