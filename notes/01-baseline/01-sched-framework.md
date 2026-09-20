# 原生调度器框架（baseline）

> **源码**：[core.c](../../kernel/kernel/sched/core.c)、[sched.h](../../kernel/kernel/sched/sched.h)、[vmlinux.lds.h](../../kernel/include/asm-generic/vmlinux.lds.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本篇是 **baseline 系列的第一篇**，目标是给出 WALT 所依赖（也所修改）的那套
原生框架。**不追求完整覆盖 CFS**，只覆盖 WALT 有 hook 或有语义依赖的位置。

> **怎么用这篇文档**：读 [02-integration-model.md](../00-overview/02-integration-model.md)
> 看到某个 hook 挂在 `core.c:2044` 时，回到本文查到
> 「`enqueue_task()` 包装函数、在 class 分发**之后**」，就知道
> WALT 在那个位置能观测到什么。

---

## 1. 调度类：`struct sched_class`

### 1.1 定义

[sched.h:2168-2231](../../kernel/kernel/sched/sched.h#L2168-L2231)。核心方法：

| 方法 | 何时被调用 |
|---|---|
| `enqueue_task(rq, p, flags)` | 任务变可运行 |
| `dequeue_task(rq, p, flags)` | 任务不再可运行 |
| `check_preempt_curr(rq, p, flags)` | 新任务能否抢占当前任务 |
| `pick_next_task(rq)` | 选下一个任务 |
| `put_prev_task(rq, p)` | 把当前任务放回 |
| `set_next_task(rq, p, first)` | 设置 `rq->curr` 后的收尾 |
| `task_tick(rq, p, queued)` | 周期 tick |
| `select_task_rq(p, cpu, flags)` | **决定任务去哪个 CPU**（SMP）|
| `migrate_task_rq(p, new_cpu)` | 迁移后的记账（SMP）|
| `balance(rq, prev, rf)` | 负载均衡（SMP）|
| `task_woken(rq, p)` | 被唤醒后（SMP）|

### 1.2 类的顺序由**链接器**决定 [反直觉]

[sched.h:2253](../../kernel/kernel/sched/sched.h#L2253) 的 `DEFINE_SCHED_CLASS`
把每个类的实例放进**独立的链接段**：

```c
#define DEFINE_SCHED_CLASS(name) \
const struct sched_class name##_sched_class \
        __aligned(__alignof__(struct sched_class)) \
        __section("__" #name "_sched_class")
```

段的排列顺序写在 [vmlinux.lds.h:127-135](../../kernel/include/asm-generic/vmlinux.lds.h#L127-L135)：

```
__begin_sched_classes = .;
*(__idle_sched_class)
*(__fair_sched_class)
*(__rt_sched_class)
*(__dl_sched_class)
*(__stop_sched_class)
__end_sched_classes = .;
```

注意**链接顺序是 idle 在前、stop 在后**，但遍历方向相反
（[sched.h:2262-2269](../../kernel/kernel/sched/sched.h#L2262-L2269)）：

```c
#define sched_class_highest (__end_sched_classes - 1)
#define sched_class_lowest  (__begin_sched_classes - 1)
#define for_each_class(class) \
        for_class_range(class, sched_class_highest, sched_class_lowest)
```

**所以 `for_each_class` 的实际优先级序是**：

```
stop → dl → rt → fair → idle
```

> **[反直觉] 推论**：`prev->sched_class <= &fair_sched_class` 这个
> 指针比较（见 §3.2）之所以成立，是因为类指针按优先级**单调递增**——
> 这是链接顺序的直接后果，不是巧合。改链接顺序会静默破坏这个快速路径。

| 类 | 实例定义位置 |
|---|---|
| stop | [stop_task.c:121](../../kernel/kernel/sched/stop_task.c#L121) |
| dl | [deadline.c:2568](../../kernel/kernel/sched/deadline.c#L2568) |
| rt | [rt.c:2637](../../kernel/kernel/sched/rt.c#L2637) |
| fair | [fair.c:12037](../../kernel/kernel/sched/fair.c#L12037) |
| idle | [idle.c:523](../../kernel/kernel/sched/idle.c#L523) |

---

## 2. `struct rq` 与 `struct cfs_rq`

### 2.1 `struct rq`

[sched.h:940](../../kernel/kernel/sched/sched.h#L940)。与 WALT 相关的字段：

| 字段 | 行 | 用途 |
|---|---:|---|
| `__lock` | 942 | rq 自旋锁 |
| `nr_running` | 948 | 所有类可运行任务数 |
| `nr_switches` | 967 | 上下文切换计数 |
| `cfs` / `rt` / `dl` | 976-978 | 三类的子队列 |
| **`curr`** | 994 | 当前任务 |
| `idle` / `stop` | 995-996 | idle / stop 任务 |
| `clock` | 1001 | rq 时钟（含 IRQ 时间）|
| **`clock_task`** | 1003 | 扣除 IRQ 的时钟 |
| `cpu_capacity` | 1023 | **当前**容量（受热压/RT 影响）|
| `cpu_capacity_orig` | 1024 | **原始**容量 |
| `cpu` | 1039 | CPU 编号 |
| `avg_idle` / `max_idle_balance_cost` | 1053 / 1059 | 空闲时长统计 |

> **`cpu_capacity` vs `cpu_capacity_orig`**：WALT 里大量出现
> `capacity_orig_of(cpu)`——它读的是 `cpu_capacity_orig`，
> 即**不受 RT 压力和热限制影响的标称容量**。而原生调度器
> 更多用 `capacity_of()`（`cpu_capacity`）。
> 【反直觉】两者在负载重时会不一致。
> 见 [../02-walt/placement.md](../02-walt/04-placement.md)。

WALT 自己的 `struct walt_rq` 通过 `rq->android_vendor_data1`
内联挂载，见 [data-structures.md §0](../00-overview/03-data-structures.md#0-挂载机制android_vendor_data)。

### 2.2 `struct cfs_rq`

[sched.h:539](../../kernel/kernel/sched/sched.h#L539)：

| 字段 | 行 | 用途 |
|---|---:|---|
| `load` | 540 | 运行权重 |
| `nr_running` | 541 | 本级可运行数 |
| **`h_nr_running`** | 542 | **层级**可运行数（含子 cgroup）|
| `tasks_timeline` | 556 | vruntime 红黑树 |
| `curr` / `next` / `last` / `skip` | 562-565 | 各调度点指针 |
| **`avg`** | 575 | **PELT 平均**（`struct sched_avg`）|

> **【待确认修正】** 这个 5.15 树里 **`struct cfs_rq` 没有 `runnable_weight` 字段**。
> 「runnable weight」只作为 `struct sched_entity` 的字段存在
> （[sched.h:558](../../kernel/kernel/sched/sched.h#L558)，`CONFIG_FAIR_GROUP_SCHED`），
> 经 `se_runnable()` 访问。cfs_rq 层面的可运行权重就是 `cfs_rq->load.weight`。

`struct sched_avg`（PELT 的核心）在
[include/linux/sched.h:486](../../kernel/include/linux/sched.h#L486)，
详见 [pelt.md](02-pelt.md)。

---

## 3. 主调度流程

### 3.1 `schedule()` → `__schedule()`

| 函数 | 位置 |
|---|---|
| `schedule()` | [core.c:6614](../../kernel/kernel/sched/core.c#L6614) |
| `__schedule(sched_mode)` | [core.c:6415](../../kernel/kernel/sched/core.c#L6415) |

`schedule()` 只是外壳：`sched_submit_work()` → 循环调用 `__schedule(SM_NONE)`
直到 `!need_resched()` → `sched_update_worker()`。

`__schedule()` 的关键步骤（[core.c:6415-6546](../../kernel/kernel/sched/core.c#L6415-L6546)）：

```
1.  rq = cpu_rq(cpu); prev = rq->curr                         :6424
2.  schedule_debug(prev)                                      :6428
3.  local_irq_disable(); rcu_note_context_switch()            :6433
4.  rq_lock(rq, &rf)                                          :6451
5.  update_rq_clock(rq)                                       :6456
6.  若阻塞：deactivate_task(DEQUEUE_SLEEP) + iowait 记账        :6468-6499
7.  next = pick_next_task(rq, prev, &rf)                      :6501
8.  trace_android_rvh_schedule(prev, next, rq)   ← 【WALT hook】 :6508
9.  if (prev != next) 切换 rq->curr 等                        :6509-6533
10. trace_sched_switch(preempt, prev, next)                   :6535
11. rq = context_switch(rq, prev, next, &rf)                  :6538
```

> **WALT 的位置**：`trace_android_rvh_schedule` 在 **第 8 步**——
> `pick_next_task()` 之后、`rq->curr` 真正切换**之前**。
> 此时 `rq->curr` 仍是 `prev`。WALT 的 `walt_rvh_schedule()`
> [walt.c:4833](../../kernel/kernel/sched/walt/walt.c#L4833) 利用这一点
> 用 `prev` / `next` 两个任务分别做 `PUT_PREV_TASK` / `PICK_NEXT_TASK` 记账。

### 3.2 `pick_next_task()` 的快速路径

[core.c:5797](../../kernel/kernel/sched/core.c#L5797)：

```c
if (likely(prev->sched_class <= &fair_sched_class &&
           rq->nr_running == rq->cfs.h_nr_running)) {
        p = pick_next_task_fair(rq, prev, rf);
        if (unlikely(p == RETRY_TASK)) goto restart;
        if (!p) { put_prev_task(rq, prev); p = pick_next_task_idle(rq); }
        return p;
}

restart:
        put_prev_task_balance(rq, prev, rf);
        for_each_class(class) {                 /* core.c:5827 */
                p = class->pick_next_task(rq);
                if (p) return p;
        }
        BUG();
```

**两个条件同时成立才走快速路径**：

1. `prev` 是 fair 或更低优先级的类（§1.2 的指针单调性）
2. `rq->nr_running == rq->cfs.h_nr_running`——即**没有** RT/DL 任务

> **`h_nr_running` 的含义**：层级计数，含 cgroup 子队列。
> 用 `h_nr_running` 而非 `nr_running` 比较是因为 `nr_running`
> 只统计本级。见 [sched.h:542](../../kernel/kernel/sched/sched.h#L542)。

### 3.3 上下文切换

| 函数 | 位置 | 职责 |
|---|---|---|
| `prepare_task_switch()` | [core.c:4975](../../kernel/kernel/sched/core.c#L4975) | 通知（perf、vtime）|
| `context_switch()` | [core.c:5126](../../kernel/kernel/sched/core.c#L5126) | MM 切换 + `switch_to()` |
| `finish_task_switch()` | [core.c:5007](../../kernel/kernel/sched/core.c#L5007) | 释放锁、`finish_task()` |
| `finish_task()` | [core.c:4784](../../kernel/kernel/sched/core.c#L4784) | `on_cpu = 0` |
| `finish_lock_switch()` | [core.c:4921](../../kernel/kernel/sched/core.c#L4921) | 放 rq 锁 |

`finish_task()` 里的 `smp_store_release(&prev->on_cpu, 0)` 是
「前一 CPU 已停止引用本任务」的信号，`try_to_wake_up()` 中的
`smp_cond_load_acquire(&p->on_cpu, !VAL)`（[core.c:4258](../../kernel/kernel/sched/core.c#L4258)）
与之配对。

---

## 4. 唤醒路径 `try_to_wake_up()`

[core.c:4110](../../kernel/kernel/sched/core.c#L4110)。这是 **WALT placement 的入口**。

```
1.  preempt_disable()                                         :4115
2.  快路径 p == current：直接置 TASK_RUNNING                    :4116-4135
3.  raw_spin_lock_irqsave(&p->pi_lock)                        :4143
4.  ttwu_state_match()                                        :4145
5.  trace_sched_waking(p)                                     :4161
6.  if (READ_ONCE(p->on_rq) && ttwu_runnable(p))  ← 已在 rq 上 :4186
7.  WRITE_ONCE(p->__state, TASK_WAKING)                       :4224
8.  ttwu_queue_wakelist()  （remote wake 走 IPI）              :4245
9.  smp_cond_load_acquire(&p->on_cpu, !VAL)                   :4258
10. trace_android_rvh_try_to_wake_up(p)          ← 【WALT hook】:4260
11. cpu = select_task_rq(p, p->wake_cpu, WF_TTWU)             :4262
12. if (task_cpu(p) != cpu) set_task_cpu(p, cpu)  ← WALT hook :4263-4272
13. ttwu_queue(p, cpu, wake_flags)                            :4277
14. trace_android_rvh_try_to_wake_up_success(p)  ← 【WALT hook】:4282
```

### 4.1 `select_task_rq()` 的分发

[core.c:3533](../../kernel/kernel/sched/core.c#L3533) 是核心包装，
调用 `p->sched_class->select_task_rq(...)`。对 CFS 任务即
`select_task_rq_fair()` → 详见 [placement-eas.md](04-placement-eas.md)。

> **【关键】WALT 的放置决策不走这里。**
> `select_task_rq_fair()` 内部在开头就有
> `trace_android_rvh_select_task_rq_fair` hook（见
> [integration-model.md](../00-overview/02-integration-model.md#5-完整-hook-表)），
> WALT 在那里**接管整个决策并返回**，原生的 EAS 逻辑不再执行。
> 见 [../02-walt/placement.md](../02-walt/04-placement.md)。

### 4.2 ttwu 子路径

| 函数 | 位置 | 说明 |
|---|---|---|
| `ttwu_state_match()` | [core.c:3954](../../kernel/kernel/sched/core.c#L3954) | 状态匹配检查 |
| `ttwu_do_wakeup()` | [core.c:3663](../../kernel/kernel/sched/core.c#L3663) | `check_preempt_curr` + `task_woken` |
| `ttwu_do_activate()` | [core.c:3699](../../kernel/kernel/sched/core.c#L3699) | `activate_task()` |
| `ttwu_runnable()` | [core.c:3751](../../kernel/kernel/sched/core.c#L3751) | 已在 rq 上，只改状态 |
| `sched_ttwu_pending()` | [core.c:3770](../../kernel/kernel/sched/core.c#L3770) | IPI 回调 |
| `ttwu_queue()` | [core.c:3926](../../kernel/kernel/sched/core.c#L3926) | 加 rq 锁后 activate |

> **`ttwu_runnable()` 的意义**：如果任务**已经在某个 rq 上**
> （`p->on_rq`），唤醒只是把状态改回 `TASK_RUNNING`，
> **不重新入队、不重新选 CPU**。所以这类唤醒**不会触发
> WALT 的 placement hook**——但它仍会触发
> `trace_android_rvh_try_to_wake_up`（第 10 步在 `on_rq` 检查**之后**，
> 不过第 6 步已经 `return` 了）。
>
> 【待确认】已在 [04-open-questions.md](../03-comparison/04-open-questions.md) 登记。

---

## 5. enqueue / dequeue 包装：WALT 记账的挂点

[core.c:2031-2067](../../kernel/kernel/sched/core.c#L2031-L2067)：

```c
static inline void enqueue_task(struct rq *rq, struct task_struct *p, int flags)
{
        if (!(flags & ENQUEUE_NOCLOCK))
                update_rq_clock(rq);
        if (!(flags & ENQUEUE_RESTORE)) { psi_enqueue/uclamp ... }
        trace_android_rvh_enqueue_task(rq, p, flags);          /* :2042 */
        p->sched_class->enqueue_task(rq, p, flags);            /* :2043 */
        trace_android_rvh_after_enqueue_task(rq, p, flags);    /* :2044 */
        sched_core_enqueue(rq, p);
}

static inline void dequeue_task(struct rq *rq, struct task_struct *p, int flags)
{
        sched_core_dequeue(rq, p, flags);
        if (!(flags & DEQUEUE_NOCLOCK))
                update_rq_clock(rq);
        if (!(flags & DEQUEUE_SAVE)) { psi_dequeue/uclamp ... }
        trace_android_rvh_dequeue_task(rq, p, flags);          /* :2064 */
        p->sched_class->dequeue_task(rq, p, flags);            /* :2065 */
        trace_android_rvh_after_dequeue_task(rq, p, flags);    /* :2066 */
}
```

**WALT 同时用了 before 和 after 两个 hook**——见
[integration-model.md §5](../00-overview/02-integration-model.md#5-完整-hook-表)。
before 位置类还没入队，after 位置已经入队。

| 函数 | 位置 |
|---|---|
| `activate_task()` | [core.c:2069](../../kernel/kernel/sched/core.c#L2069) |
| `deactivate_task()` | [core.c:2080](../../kernel/kernel/sched/core.c#L2080) |

`activate_task()` = `enqueue_task()` + `p->on_rq = TASK_ON_RQ_QUEUED`。

---

## 6. `scheduler_tick()`

[core.c:5439](../../kernel/kernel/sched/core.c#L5439)：

```
rq_lock; update_rq_clock;
trace_android_rvh_tick_entry(rq)                 ← 【WALT hook】 :5454
thermal pressure
curr->sched_class->task_tick(rq, curr, 0)                     :5458
calc_global_load_tick(rq)                                     :5461
unlock
perf_event_task_tick()
trigger_load_balance(rq)                                      :5472
trace_android_vh_scheduler_tick(rq)              ← 【WALT hook】 :5475
```

> **唯一被注册两次的 hook**：`android_vh_scheduler_tick` 同时被
> [walt.c:4997](../../kernel/kernel/sched/walt/walt.c#L4997) 与
> [walt_rt.c:85](../../kernel/kernel/sched/walt/walt_rt.c#L85) 注册。
> 这是 `DECLARE_HOOK`（多回调）而非 `DECLARE_RESTRICTED_HOOK`，
> 允许挂多个。详见 [02-integration-model.md](../00-overview/02-integration-model.md)。

---

## 7. WALT 在本篇范围内触及的位置总表

| 原生位置 | hook | WALT 用途 |
|---|---|---|
| [core.c:2042](../../kernel/kernel/sched/core.c#L2042) | `android_rvh_enqueue_task` | 入队前记账 |
| [core.c:2044](../../kernel/kernel/sched/core.c#L2044) | `android_rvh_after_enqueue_task` | 入队后 `walt_update_task_ravg` |
| [core.c:2064](../../kernel/kernel/sched/core.c#L2064) | `android_rvh_dequeue_task` | 出队前记账 |
| [core.c:2066](../../kernel/kernel/sched/core.c#L2066) | `android_rvh_after_dequeue_task` | 出队后记账 |
| [core.c:2165](../../kernel/kernel/sched/core.c#L2165) | `check_preempt_curr()` | 抢占判定（含 WALT 的 MVP 逻辑）|
| [core.c:3175](../../kernel/kernel/sched/core.c#L3175) | `android_rvh_set_task_cpu` | 迁移记账 |
| [core.c:3460](../../kernel/kernel/sched/core.c#L3460) | `android_rvh_select_fallback_rq` | fallback CPU 选择 |
| [core.c:3906](../../kernel/kernel/sched/core.c#L3906) | `android_rvh_ttwu_cond` | 唤醒队列条件 |
| [core.c:4260](../../kernel/kernel/sched/core.c#L4260) | `android_rvh_try_to_wake_up` | 唤醒记账 |
| [core.c:4282](../../kernel/kernel/sched/core.c#L4282) | `android_rvh_try_to_wake_up_success` | 唤醒成功记账 |
| [core.c:6508](../../kernel/kernel/sched/core.c#L6508) | `android_rvh_schedule` | 上下文切换记账 |
| [core.c:6535](../../kernel/kernel/sched/core.c#L6535) | `trace_sched_switch`（原生）| 观测（非 hook）|
| [core.c:5454](../../kernel/kernel/sched/core.c#L5454) | `android_rvh_tick_entry` | tick 记账 |
| [core.c:5475](../../kernel/kernel/sched/core.c#L5475) | `android_vh_scheduler_tick` | tick 后处理（**双注册**）|
| [core.c:7328](../../kernel/kernel/sched/core.c#L7328) | `android_rvh_effective_cpu_util` | 替换 util 计算（**关键**）|
| [core.c:9444](../../kernel/kernel/sched/core.c#L9444) | `android_rvh_sched_cpu_starting` | CPU 上线 |
| [core.c:9518](../../kernel/kernel/sched/core.c#L9518) | `android_rvh_sched_cpu_dying` | CPU 下线 |
| [core.c:5086](../../kernel/kernel/sched/core.c#L5086) | `android_rvh_flush_task` | 任务退出清理 |

> `android_rvh_effective_cpu_util` [core.c:7328](../../kernel/kernel/sched/core.c#L7328)
> 是 **WALT 接管调频的关键**——它让 `effective_cpu_util()` 返回
> WALT 计算的 util 而非 PELT 的。见 [schedutil.md](03-schedutil.md)。

---

## 8. 相关文档

- PELT 负载跟踪（被 WALT 旁路的机制）→ [pelt.md](02-pelt.md)
- schedutil 调频 → [schedutil.md](03-schedutil.md)
- EAS 放置 → [placement-eas.md](04-placement-eas.md)
- 负载均衡 → [load-balance.md](05-load-balance.md)
- WALT 如何挂进这些位置 → [../00-overview/integration-model.md](../00-overview/02-integration-model.md)
