# WALT 窗口模型

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本文档是 WALT 的**核心**。WALT 的一切决策都建立在本文描述的这套记账之上。

字段定义见 [03-data-structures.md](../00-overview/03-data-structures.md)，
驱动关系见 [04-data-flow.md](../00-overview/04-data-flow.md)。

---

## 1. 一句话模型

把连续时间切成固定长度的**窗口**，在每个窗口里累计「任务可运行了多久」
（已做频率归一化），窗口边界上滚一次并把结果压入长度为 5 的历史环。
`demand` 取历史里的**最大值**——宁可高估，不可低估。

```
时间轴 ──────────────────────────────────────────────▶
        │         │         │         │         │
     窗口 N-4    N-3       N-2       N-1       N(当前)
        └─────────┴─────────┴─────────┴─────────┘
                     sum_history[5] 环形缓冲
                            │
                            ▼
              demand = f(sum_history, 策略)  ← 默认取 MAX
```

---

## 2. 窗口的定义

### 2.1 窗口长度

`unsigned int __read_mostly sched_ravg_window`（[walt.h:196](../../kernel/kernel/sched/walt/walt.h#L196)）。

编译期默认值见 [walt.h:19-31](../../kernel/kernel/sched/walt/walt.h#L19-L31)：

| 条件 | `DEFAULT_SCHED_RAVG_WINDOW` |
|---|---|
| `CONFIG_HZ_300` | `3333333 * 5` ≈ 16.67 ms |
| 否则 | `16000000` = 16 ms |

上限 `MAX_SCHED_RAVG_WINDOW = 1000000000`（1 秒）。

> 常说的「20ms 窗口」是运行时通过 `sysctl_sched_ravg_window_nr_ticks`
> 设定的常见配置，**不是**编译期常量。详见 [CONVENTIONS.md §4.1](../CONVENTIONS.md)。

### 2.2 两套窗口：CPU 侧与任务侧

这是理解 WALT 的关键前提：**CPU 和任务各有一套 `window_start`**。

| | 字段 | 含义 |
|---|---|---|
| CPU 侧 | `wrq->window_start` | 该 CPU 的窗口边界 |
| 任务侧 | `wts->window_start` | 该任务上次参与滚动时的窗口边界 |

**它们会不一致**：任务在 CPU A 上运行到窗口中途、然后迁移到 CPU B，
B 的窗口边界可能与 A 不同。WALT 用大量 `WALT_BUG` 断言来保证两者在
**需要一致的时刻**是一致的（见 §10）。

此外还有一个全局的 `walt_irq_work_lastq_ws`（见 §9）。

---

## 3. 三个核心量

| 量 | 字段 | 窗口内语义 | 跨窗口语义 |
|---|---|---|---|
| **sum** | `wts->sum` | 当前窗口累计的可运行时间（含等待），已频率归一化 | 在 `update_history` 中被**清零** |
| **demand** | `wts->demand` | 无当前含义 | 历史 5 窗口中按策略取的值（默认 MAX） |
| **demand_scaled** | `wts->demand_scaled` | 无 | `demand` 归一化到 1024 单位 |

`demand_scaled` 是**唯一被外部消费的量**：

- `task_util()` [walt.h:430](../../kernel/kernel/sched/walt/walt.h#L430) 直接返回它
- 它经 `fixup_walt_sched_stats_common()` 聚合进 `wrq->walt_stats.cumulative_runnable_avg_scaled`
- placement / core_ctl / LB 读的都是聚合后的那个值

### 3.1 `sum` 的截断

`add_to_task_demand()` [walt.c:2065](../../kernel/kernel/sched/walt/walt.c#L2065)：

```c
static u64 add_to_task_demand(struct rq *rq, struct task_struct *p, u64 delta)
{
        delta = scale_exec_time(delta, rq, wts);
        wts->sum += delta;
        if (unlikely(wts->sum > sched_ravg_window))
                wts->sum = sched_ravg_window;
        return delta;
}
```

**`sum` 被夹到 `sched_ravg_window`**——即一个任务在一个窗口内
最多算「跑满一个窗口」。这是 WALT 用**时间**（而非容量）做单位的结果：
频率越高，同样的实际时间换算出的归一化时间越长，但不会超过窗口长度。

---

## 4. 主入口：`walt_update_task_ravg()`

[walt.c:2288](../../kernel/kernel/sched/walt/walt.c#L2288)。完整流程见
[data-flow.md §2](../00-overview/04-data-flow.md#2-入口walt_update_task_ravg)。

这里只强调两个**性能与正确性关键点**：

### 4.1 幂等去重

```c
if (!wrq->window_start || wts->mark_start == wallclock)
        return;
```

同一次调度事件可能从多个 hook 进入本函数（例如 enqueue 后又立即 schedule），
`mark_start == wallclock` 让重复调用**零成本返回**。

### 4.2 `mark_start` 的双重角色

`mark_start` 既是「本次区间的起点」，也是「上次更新结束的时刻」。
函数末尾无条件 `wts->mark_start = wallclock`（[walt.c:2323](../../kernel/kernel/sched/walt/walt.c#L2323)）。

> `update_task_demand()` 的注释明确警告
> （[walt.c:2124-2125](../../kernel/kernel/sched/walt/walt.c#L2124-L2125)）：
> **"IMPORTANT: Leave `wts->mark_start` unchanged, as `update_cpu_busy_time()` depends on it!"**
> —— 两步更新共用同一个 `mark_start` 作起点。

---

## 5. 六种事件与两个记账谓词

`enum task_event` [walt.h:40](../../kernel/kernel/sched/walt/walt.h#L40)：

| 值 | 名称 | 含义 |
|---:|---|---|
| 0 | `PUT_PREV_TASK` | 被换出 |
| 1 | `PICK_NEXT_TASK` | 被选中 |
| 2 | `TASK_WAKE` | 被唤醒 |
| 3 | `TASK_MIGRATE` | 迁移 |
| 4 | `TASK_UPDATE` | 定时更新 |
| 5 | `IRQ_UPDATE` | IRQ 时间更新 |

### 5.1 两个不同的谓词 [反直觉]

WALT 用了**两个独立的**记账判定函数，很容易混淆：

| 谓词 | 位置 | 服务对象 |
|---|---|---|
| `account_busy_for_task_demand()` | [walt.c:1939](../../kernel/kernel/sched/walt/walt.c#L1939) | 任务侧的 `sum` / `demand` |
| `account_busy_for_cpu_time()` | [walt.c:1531](../../kernel/kernel/sched/walt/walt.c#L1531) | CPU 侧的 `curr/prev_runnable_sum` |

**同一个事件，两者的答案可能相反**。例：

- `TASK_WAKE` → `account_busy_for_task_demand` 返回 **0**（唤醒标志非忙段结束），
  `account_busy_for_cpu_time` 也返回 **0**
- `TASK_UPDATE` 且 `rq->curr != p` 且 `p->on_rq` →
  demand 返回 `SCHED_ACCOUNT_WAIT_TIME`，cpu_time 返回 `SCHED_FREQ_ACCOUNT_WAIT_TIME`

**两个编译期开关决定等待时间是否算忙**：

| 宏 | 影响 |
|---|---|
| `SCHED_ACCOUNT_WAIT_TIME` | demand 侧是否把等待算作忙 |
| `SCHED_FREQ_ACCOUNT_WAIT_TIME` | cpu_time 侧是否把等待算作忙 |

【推测】这两个宏的设计意图是分别控制「需求估计」与「频率估计」是否计入
排队等待时间——排队中的任务确实需要 CPU，但还没真正占用它。

### 5.2 `account_busy_for_task_demand` 全文逻辑

```c
if (is_idle_task(p))           return 0;
if (event == TASK_WAKE ||
    (!SCHED_ACCOUNT_WAIT_TIME &&
     (event == PICK_NEXT_TASK || event == TASK_MIGRATE)))
                               return 0;
if (event == PICK_NEXT_TASK && rq->curr == rq->idle)
                               return 0;   /* idle exit 不计 */
if (event == TASK_UPDATE) {
        if (rq->curr == p)     return 1;
        return p->on_rq ? SCHED_ACCOUNT_WAIT_TIME : 0;
}
return 1;
```

### 5.3 `account_busy_for_cpu_time` 全文逻辑

```c
if (is_idle_task(p)) {
        if (event == PICK_NEXT_TASK)    return 0;
        return irqtime || cpu_is_waiting_on_io(rq);
}
if (event == TASK_WAKE)                 return 0;
if (event == PUT_PREV_TASK || event == IRQ_UPDATE)  return 1;
if (event == TASK_UPDATE) {
        if (rq->curr == p)              return 1;
        return p->on_rq ? SCHED_FREQ_ACCOUNT_WAIT_TIME : 0;
}
return SCHED_FREQ_ACCOUNT_WAIT_TIME;    /* TASK_MIGRATE, PICK_NEXT_TASK */
```

注意 idle 任务在 `account_busy_for_cpu_time` 里**不是无条件返回 0**——
IRQ 时间和 iowait 时间会记在 idle 任务头上。这正是 §7.5 那段 IRQ 处理的前提。

---

## 6. `update_task_demand()` 的三段式切分

[walt.c:2127](../../kernel/kernel/sched/walt/walt.c#L2127)。源码注释
（[walt.c:2077-2126](../../kernel/kernel/sched/walt/walt.c#L2077-L2126)）给出了三种情形，
代码与之一一对应：

| 情形 | 条件 | 处理 |
|---|---|---|
| **a** 事件在单个窗口内 | `!new_window` | 一次 `add_to_task_demand(wallclock - mark_start)` |
| **b** 事件跨两个窗口 | `new_window` | 先 `add(ws - ms)`，再 `update_history(sum, 1)`，再 `add(wc - ws)` |
| **c** 事件跨多个窗口 | `nr_full_windows > 0` | 同上，但中间插入 `nr_full_windows` 个 `window_size` 样本 |

### 6.1 情形 c 的窗口回退技巧

```c
delta = window_start - mark_start;
nr_full_windows = div64_u64(delta, window_size);
window_start -= (u64)nr_full_windows * (u64)window_size;   /* 回退 */

runtime  = add_to_task_demand(rq, p, window_start - mark_start);
update_history(rq, p, wts->sum, 1, event);
if (nr_full_windows) {
        u64 scaled_window = scale_exec_time(window_size, rq, wts);
        update_history(rq, p, scaled_window, nr_full_windows, event);
        runtime += nr_full_windows * scaled_window;
}
window_start += (u64)nr_full_windows * (u64)window_size;   /* 再前进 */

mark_start = window_start;
runtime += add_to_task_demand(rq, p, wallclock - mark_start);
```

**把 `window_start` 临时回退到「`mark_start` 之后的第一个窗口边界」**，
于是三段处理复用同一套代码。

> 中间的 `nr_full_windows` 个完整窗口被记为「跑满一个窗口」
> （`scaled_window = scale_exec_time(window_size)`）。
> 这适用于「RT 任务连续跑了多个窗口没被抢占」的场景——
> 源码注释 [walt.c:1978-1982](../../kernel/kernel/sched/walt/walt.c#L1978-L1982) 明说了这一点。

### 6.2 无记账时的特殊处理

```c
if (!account_busy_for_task_demand(rq, p, event)) {
        if (new_window)
                update_history(rq, p, wts->sum, 1, event);
        return 0;
}
```

即使事件本身不计入忙时，**只要跨了窗口就必须把已有 `sum` 结算进历史**，
否则这段 `sum` 会丢失。注释解释了为什么不补记中间的多个窗口：

> Multiple windows may have elapsed, but since **empty windows are dropped**,
> it is not necessary to account those.

---

## 7. `update_cpu_busy_time()` 的完整分支

[walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678)。这是最复杂的函数，
源码自带详细的 ASCII 图注释（[walt.c:1630-1677](../../kernel/kernel/sched/walt/walt.c#L1630-L1677)）。

### 7.0 RTG 任务走另一套计数器 [反直觉]

```c
grp = wts->grp;
if (grp) {
        struct group_cpu_time *cpu_time = &wrq->grp_time;
        curr_runnable_sum     = &cpu_time->curr_runnable_sum;
        prev_runnable_sum     = &cpu_time->prev_runnable_sum;
        nt_curr_runnable_sum  = &cpu_time->nt_curr_runnable_sum;
        nt_prev_runnable_sum  = &cpu_time->nt_prev_runnable_sum;
}
```

**属于 RTG 的任务，其忙时记入 `grp_time` 而非 rq 的主计数器。**

这意味着 `wrq->curr_runnable_sum` **不包含 RTG 任务**。调频时两者相加
（`freq_policy_load()` 中的 `prev_runnable_sum + aggr_grp_load`）。

**为什么要分开**：RTG 的负载需要跨簇聚合（一个 RTG 可能横跨多个簇），
单独记账才能把「这一组线程的总需求」报在单个 CPU 上。

### 7.1 分支总览

```
new_window   = mark_start < window_start
full_window  = (window_start - mark_start) >= window_size
if new_window: rollover_task_window(p, full_window); wts->window_start = window_start
new_task     = is_new_task(p)
if !account_busy_for_cpu_time(...)          → done

┌─ A. !new_window
│      delta = irqtime 或 (wallclock - mark_start)
│      delta = scale_exec_time(delta)
│      *curr_runnable_sum += delta;  if new_task → *nt_curr += delta
│      if !idle: wts->curr_window += delta; wts->curr_window_cpu[cpu] += delta
│
├─ B. new_window && !p_is_curr_task
│      if !full_window: delta = scale(ws - ms); wts->prev_window += delta   ← 累加
│      else:            delta = scale(window_size); wts->prev_window = delta ← 赋值
│      *prev_runnable_sum += delta; if new_task → *nt_prev += delta
│      delta = scale(wallclock - window_start); *curr_runnable_sum += delta
│      wts->curr_window = delta; wts->curr_window_cpu[cpu] = delta
│
├─ C. new_window && p_is_curr_task && (!irqtime || !idle || iowait)
│      与 B 几乎相同，但有两点差异：
│        · wts->prev_window 用 += 而非 =          ← 见 §7.2
│        · 有 !is_idle_task(p) 保护
│
└─ D. new_window && p_is_curr_task && irqtime && idle
       WALT_PANIC(!is_idle_task(p))
       mark_start = wallclock - irqtime
       if mark_start > window_start:
               *curr_runnable_sum += scale(irqtime); return
       delta = min(window_start - mark_start, window_size)
       *prev_runnable_sum += scale(delta)
       *curr_runnable_sum += scale(wallclock - window_start)
done:
  if !idle: update_top_tasks(p, rq, old_curr_window, new_window, full_window)
```

### 7.2 分支 B 与 C 的不对称 [反直觉]

```c
/* B: !p_is_curr_task */
wts->prev_window += delta;      /* walt.c:1792 */

/* C: p_is_curr_task */
wts->prev_window += delta;      /* walt.c:1842 —— 同样是 += */
```

但 `full_window` 的子分支不同：

| | 分支 B（`!p_is_curr_task`）| 分支 C（`p_is_curr_task`）|
|---|---|---|
| `!full_window` | `wts->prev_window += delta` (:1792) | `wts->prev_window += delta` (:1842) |
| `full_window` | `wts->prev_window = delta` (:1801) | `wts->prev_window = delta` (:1853) |

两者其实**一致**。真正的差异是：**分支 B 会覆盖 `wts->curr_window`**
（`wts->curr_window = delta`，:1815），而分支 C 同样覆盖（:1869）。

> 修正记录：**初次阅读时误以为此处有 `+=` / `=` 的不对称，实测两分支一致。**
> 保留此条以警示：这两个 130 行长的分支**结构高度重复**，
> 差异只在 idle 保护与 irqtime 处理上，读的时候容易看出不存在的差异。

### 7.3 `full_window` 的两种语义

| 名称 | 定义处 | 含义 |
|---|---|---|
| `full_window`（局部） | [walt.c:1702](../../kernel/kernel/sched/walt/walt.c#L1702) | `(window_start - mark_start) >= window_size` |
| `full_window`（传给 rollover） | [walt.c:437](../../kernel/kernel/sched/walt/walt.c#L437) | `nr_windows > 1` |

两处同名但**语义不同**：
- 前者问「任务上次更新是否已是**至少一个窗口之前**」
- 后者问「**多个**窗口是否已经过去」

`update_cpu_busy_time` 用前者；`update_window_start` 用后者传给
`rollover_cpu_window` / `rollover_task_window`。

### 7.4 `rollover_task_window`：空窗口丢弃

[walt.c:1494](../../kernel/kernel/sched/walt/walt.c#L1494)：

```c
u32 *curr_cpu_windows = empty_windows;   /* 全零的静态数组 */
curr_window = 0;
if (!full_window) {                      /* 恰好一个窗口过去 */
        curr_window = wts->curr_window;
        curr_cpu_windows = wts->curr_window_cpu;
}
wts->prev_window = curr_window;
wts->curr_window = 0;
for (i = 0; i < nr_cpu_ids; i++) {
        wts->prev_window_cpu[i] = curr_cpu_windows[i];
        wts->curr_window_cpu[i] = 0;
}
if (is_new_task(p))
        wts->active_time += wrq->prev_window_size;
```

**`full_window` 为真时，直接把 `prev_window` 置 0**——任务睡过了整个窗口，
它那个窗口的贡献被丢弃。这与 `update_history` 中「空窗口被忽略」是同一个设计。

### 7.5 分支 D：IRQ 时间记账

源码注释用三张图说明了 irqtime 的三种落点
（[walt.c:1630-1677](../../kernel/kernel/sched/walt/walt.c#L1630-L1677)）。
核心约束：**`irqtime` 只会记在 idle 任务上**
（`WALT_PANIC(!is_idle_task(p))` 强制保证，:1889）。

```c
mark_start = wallclock - irqtime;      /* mark_start 在此被临时改写 */
if (mark_start > window_start) {       /* IRQ 完全落在当前窗口 */
        *curr_runnable_sum += scale_exec_time(irqtime, rq, wts);
        return;
}
delta = window_start - mark_start;     /* IRQ 跨越窗口边界 */
if (delta > window_size)
        delta = window_size;           /* 最多补一个满窗口 */
*prev_runnable_sum += scale_exec_time(delta, rq, wts);
delta = wallclock - window_start;
wrq->curr_runnable_sum += scale_exec_time(delta, rq, wts);
```

> **注意** `mark_start` 在这里被改写为 `wallclock - irqtime`，
> 并且本分支**不回到 `done` 标签**（直接 `return`），
> 所以不会执行 `update_top_tasks()`。这与 `mark_start` 的语义
> 「本次区间起点」是自洽的：IRQ 期间的起点就是 IRQ 开始时刻。

---

## 8. 频率归一化：`scale_exec_time()`

[walt.c:1566](../../kernel/kernel/sched/walt/walt.c#L1566)：

```c
static inline u64 scale_exec_time(u64 delta, struct rq *rq, struct walt_task_struct *wts)
{
        struct walt_rq *wrq = (struct walt_rq *) rq->android_vendor_data1;

        delta = (delta * wrq->task_exec_scale) >> SCHED_CAPACITY_SHIFT;

        if (wts->load_boost && wts->grp && wts->grp->skip_min)
                delta = (delta * (1024 + wts->boosted_task_load) >> 10);

        return delta;
}
```

### 8.1 主归一化

`task_exec_scale` 是 `wrq` 上的当前缩放因子，`SCHED_CAPACITY_SHIFT = 10`，
所以 `>> 10` 即 `/1024`。语义：

```
归一化时间 = 实际时间 × (task_exec_scale / 1024)
```

`task_exec_scale` 越大代表当前跑得越快（频率越高）——
**同样的实际 1ms，在快核上换算出的归一化时间更长**。

### 8.2 `task_exec_scale` 的来源

由 `update_task_rq_cpu_cycles()` 系列函数基于 **CPU 周期计数器**计算，
而不是直接读 `cpufreq` 的当前频率。

```c
static inline u64 read_cycle_counter(int cpu, u64 wallclock)
{
        if (wrq->last_cc_update != wallclock) {
                wrq->cycles = qcom_cpufreq_get_cpu_cycle_counter(cpu);
                wrq->last_cc_update = wallclock;
        }
        return wrq->cycles;
}
```

`qcom_cpufreq_get_cpu_cycle_counter()` 是 **Qualcomm 平台专有 API**——
这是 WALT 与高通 cpufreq 驱动强耦合的一处证据。

【推测】用周期计数器而非标称频率，是为了在 DVFS 切换的过渡期内
仍能得到准确的时间换算，避免频率跳变造成的负载估计抖动。

### 8.3 RTG 任务的额外放大

```c
if (wts->load_boost && wts->grp && wts->grp->skip_min)
        delta = (delta * (1024 + wts->boosted_task_load) >> 10);
```

三个条件同时满足才放大：`load_boost` 非零 **且** 属于 RTG **且** 该 RTG `skip_min`。
放大系数 `(1024 + boosted_task_load) / 1024`。

---

## 9. `update_history()` 与四种策略

[walt.c:1984](../../kernel/kernel/sched/walt/walt.c#L1984)。

### 9.1 空窗口丢弃

```c
if (!runtime || is_idle_task(p) || !samples)
        goto done;
```

**注意这里是 `goto done` 而不是 `return`**——`done` 标签处有 tracepoint
（:2061-2062），保证观测不漏。

### 9.2 环形缓冲的写入

```c
for (; samples > 0; samples--) {
        hist[wts->cidx] = runtime;
        hist_util[wts->cidx] = runtime_scaled;
        wts->cidx = ++(wts->cidx) % RAVG_HIST_SIZE;
}
```

`samples > 1` 时**连续写入多个相同的值**，覆盖掉中间的多个窗口。
对应 §6.1 中「RT 任务连跑多个窗口」的情形。

### 9.3 四种 demand 策略

`sysctl_sched_window_stats_policy`（[walt.h:288-292](../../kernel/kernel/sched/walt/walt.h#L288-L292)）：

| 值 | 名称 | `demand =` | 特点 |
|---:|---|---|---|
| 0 | `WINDOW_STATS_RECENT` | `runtime` | 只看刚结束的窗口，最激进 |
| 1 | `WINDOW_STATS_MAX` | `max(hist[])` | 历史峰值，最保守 |
| 2 | `WINDOW_STATS_MAX_RECENT_AVG` | `max(avg, runtime)` | **默认** |
| 3 | `WINDOW_STATS_AVG` | `sum / RAVG_HIST_SIZE` | 纯平均 |

代码用 `if/else if/else` 实现，**值 2 落在 `else` 分支**
（[walt.c:2021-2027](../../kernel/kernel/sched/walt/walt.c#L2021-L2027)）——
读代码时容易误以为 `else` 是「其它非法值」，实际它承担了默认策略。

### 9.4 `coloc_demand` 用平均值 [反直觉]

```c
wts->coloc_demand = div64_u64(sum, RAVG_HIST_SIZE);
```

**`coloc_demand` 恒定取 5 窗口平均，不受 `window_stats_policy` 影响**。

即：共置（colocation）决策用的是**平滑的**信号，而 placement / 调频
用的是**保守的**（峰值）信号。设计意图 [推测]：共置是为了减少
跨簇通信延迟，用平均值可以避免因单个窗口的突发就把任务搬来搬去。

### 9.5 `unfilter` 的衰减

```c
if (demand_scaled > sysctl_sched_min_task_util_for_colocation)
        wts->unfilter = sysctl_sched_task_unfilter_period;
else if (wts->unfilter)
        wts->unfilter = max_t(int, 0, wts->unfilter - wrq->prev_window_size);
```

`unfilter` 是一个**非线性衰减的定时器**：需求高于阈值时被重置为满值，
否则按窗口长度递减。它标记「这个任务最近有过较高需求」，
消费者是 `walt_should_kick_upmigrate()` [walt.h:747](../../kernel/kernel/sched/walt/walt.h#L747)。

---

## 10. 迁移簿记

### 10.1 为什么需要

任务从 CPU A 迁到 B 时：

1. 它在 A 的 `curr_runnable_sum` 里已有一份贡献，必须扣除
2. 需要在 B 上加上

### 10.2 `enqueue_after_migration` 的取值

`migrate_busy_time_subtraction()` [walt.c:1021](../../kernel/kernel/sched/walt/walt.c#L1021)：

```c
if (!same_freq_domain(task_cpu(p), new_cpu))
        wts->enqueue_after_migration = 2;   /* 跨簇 */
else
        wts->enqueue_after_migration = 1;   /* 簇内 */
```

### 10.3 跨簇才扣主计数器 [反直觉]

```c
if (grp) {
        /* RTG 任务：无条件扣（簇内簇外都扣）*/
        if (wts->curr_window) {
                *src_curr_runnable_sum -= wts->curr_window;
                if (new_task) *src_nt_curr_runnable_sum -= wts->curr_window;
        }
        if (wts->prev_window) { ... }
} else {
        /* 非 RTG：只有跨簇才扣 */
        if (wts->enqueue_after_migration == 2)
                migrate_inter_cluster_subtraction(p, task_cpu(p), new_task);
}
```

**非 RTG 任务的簇内迁移不做减法。** 源码注释给出了理由
（[walt.c:1071-1075](../../kernel/kernel/sched/walt/walt.c#L1071-L1075)）：

> For frequency aggregation, we continue to do migration fixups even for
> intra cluster migrations. This is because, the aggregated load has to
> be reported on a single CPU regardless.

即：**频率聚合是按簇做的**，簇内迁移不影响簇级总量，所以不需要修正；
而 RTG 的负载要报在**单个 CPU** 上，所以必须修正。

【推测】这也意味着簇内频繁迁移不会引起频率抖动——这是有意的性能优化。

### 10.4 `load_subtractions` 的延迟确认

`migrate_inter_cluster_subtraction()` 不直接减，而是记入
`wrq->load_subs[]`（见 [data-structures.md §5](../00-overview/03-data-structures.md#5-struct-load_subtractions迁移减法记账)），
在窗口滚动时结算。

### 10.5 调用 `walt_update_task_ravg` 先结算

```c
wallclock = walt_sched_clock();
walt_update_task_ravg(p, task_rq(p), TASK_MIGRATE, wallclock, 0);
```

**在扣减之前先把任务在旧 CPU 上的账结清**——否则 `wts->curr_window`
不是最新值，扣减量会算错。

---

## 11. 窗口滚动的全局触发

见 [data-flow.md §4](../00-overview/04-data-flow.md#4-消费方-1调频窗口滚动驱动)。

`run_walt_irq_work_rollover()` [walt.c:2271](../../kernel/kernel/sched/walt/walt.c#L2271)
用 `atomic64_cmpxchg` 保证每个窗口只有**一个** CPU 触发全局的
`walt_irq_work`，避免 N 个 CPU 各触发一次调频。

---

## 12. 不变量与断言

WALT 用 `WALT_BUG` 宏（[walt.h:1199](../../kernel/kernel/sched/walt/walt.h#L1199)）
在关键不变量被破坏时报错。排查现场问题时，**这些断言的触发点就是线索**：

| 断言 | 位置 | 含义 |
|---|---|---|
| `wallclock < wrq->latest_clock` | [walt.c:414](../../kernel/kernel/sched/walt/walt.c#L414) | 时钟回退（通常 suspend/resume 问题）→ `WALT_PANIC` |
| `wallclock < wrq->window_start` | [walt.c:422](../../kernel/kernel/sched/walt/walt.c#L422) | 同上 |
| `mark_start` 超前 `window_start` 超过一个窗口 | [walt.c:2324](../../kernel/kernel/sched/walt/walt.c#L2324) | 记账错乱 |
| `wts->window_start != wrq->window_start` | [walt.c:1730](../../kernel/kernel/sched/walt/walt.c#L1730) | 任务侧与 CPU 侧窗口失配 |
| 同上（迁移路径） | [walt.c:1057](../../kernel/kernel/sched/walt/walt.c#L1057) | 迁移时失配 |
| `cumulative_runnable_avg_scaled < 0` | [walt.c:325](../../kernel/kernel/sched/walt/walt.c#L325) | 增减配对被破坏（见 [data-structures.md §3](../00-overview/03-data-structures.md#3-struct-walt_sched_statsper-cpu-统计聚合)）|

---

## 13. 相关文档

- 字段定义 → [03-data-structures.md](../00-overview/03-data-structures.md)
- 需求预测 → [demand-prediction.md](02-demand-prediction.md)
- 消费这些数据的算法 → [cpufreq.md](03-cpufreq.md) / [placement.md](04-placement.md) / [power-side.md](07-power-side.md)
- 与 PELT 的对比 → [01-base-vs-walt.md](../03-comparison/01-base-vs-walt.md)
