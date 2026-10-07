# 需求预测：16 桶直方图

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/include/linux/sched/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

`demand` 回答「这个任务**过去**要多少 CPU」，
`pred_demand` 回答「这个任务**下一个窗口**要多少 CPU」。

前者是历史的峰值，后者是用**直方图 + 泄漏积分器**外推的估计。
两者都在 `wts->demand_scaled` / `wts->pred_demand_scaled` 上以 1024 为满刻度表达。

本文件是 [window-model.md](01-window-model.md) 的续篇——建议先读那篇。

---

## 1. 为什么需要预测

`demand` 取历史 5 窗口的**最大值**，本身已经很保守。但它有两个盲区：

1. **突发上升**：任务刚从轻载变为重载，历史里还没有重载样本，
   `demand` 要等一个窗口才能反映出来
2. **周期性负载**：一个每 3 个窗口跑一次的重任务，5 窗口最大值能覆盖，
   但一个「逐渐爬升」的负载用最大值也是滞后的

`pred_demand` 用「这个任务的忙时**通常落在哪个量级**」来回答，
而不是「最近一次是多少」。

---

## 2. 16 个桶的定义

### 2.1 常量

[include/linux/sched/walt.h:42-44](../../kernel/include/linux/sched/walt.h#L42-L44)：

```c
/* wts->bucket_bitmask needs to be updated if NUM_BUSY_BUCKETS > 16 */
#define NUM_BUSY_BUCKETS 	16
#define NUM_BUSY_BUCKETS_SHIFT 	4
```

> **位掩码容量限制**：`bucket_bitmask` 是 `u16`，所以最多 16 个桶。
> 注释明确写了这条约束——如果要把桶数提到 17+，必须同步把
> `bucket_bitmask` 换成 `u32`。

### 2.2 桶的边界

`busy_to_bucket()` [walt.c:1220](../../kernel/kernel/sched/walt/walt.c#L1220)：

```c
static inline int busy_to_bucket(u16 normalized_rt)
{
        int bidx;

        bidx = normalized_rt >> (SCHED_CAPACITY_SHIFT - NUM_BUSY_BUCKETS_SHIFT);
        bidx = min(bidx, NUM_BUSY_BUCKETS - 1);

        /*
         * Combine lowest two buckets. The lowest frequency falls into
         * 2nd bucket and thus keep predicting lowest bucket is not
         * useful.
         */
        if (!bidx)
                bidx++;

        return bidx;
}
```

`SCHED_CAPACITY_SHIFT = 10`，`NUM_BUSY_BUCKETS_SHIFT = 4`，
所以移位量是 `10 - 4 = 6`：

```
bidx = normalized_rt >> 6
```

`normalized_rt` 是 `scale_time_to_util(curr_window)` 的结果，量程 0..1024。

| 桶 | 忙时范围（1024 刻度）| 占容量 |
|---:|---|---|
| **0** | **从不使用** | — |
| **1** | **0 – 127**（由桶 0 与 1 合并而来）| 0 – 12.5% |
| 2 | 128 – 191 | 12.5 – 18.75% |
| 3 | 192 – 255 | 18.75 – 25% |
| 4 | 256 – 319 | 25 – 31.25% |
| … | … | … |
| 15 | 960 – 1023 | 93.75 – 100% |

### 2.3 桶 0 被废弃 [反直觉]

```c
bidx = min(bidx, NUM_BUSY_BUCKETS - 1);   /* 1024 >> 6 = 16 → 15 */
if (!bidx)
        bidx++;
```

**经过这两行，`busy_to_bucket()` 永远不会返回 0 或 16。**
值域被压到 `[1, 15]`。

桶 0 的废弃是**永久性的**：`bucket_increase()` 从不清除它的计数
（它只会被衰减到 0），而 `get_pred_busy()` 在 `final < 2` 时
把 `dmin` 设为 0、`final` 设为 1，即**把桶 0 的范围并给桶 1**。

源码注释给出的理由：最低频率本身就落入桶 2（`>> 6` 后 128），
所以「预测最低桶」没有意义。

### 2.4 每桶最多记录 15 个窗口 [待确认]

桶计数是 `u8`（[walt.h:106](../../kernel/include/linux/sched/walt.h#L106)），
`INC_STEP = 8`、`INC_STEP_BIG = 16`。

命中一次时：
- 计数 `< 16`（`CONSISTENT_THRES`）→ `+= 8`
- 计数 `>= 16` → `+= 16`

所以计数序列是 `0 → 8 → 16 → 32 → 48 → …`，
**在 32 之后每次固定 +16，因而已「饱和」**。

【待确认】这个计数的绝对量级似乎不是设计重点——`get_pred_busy()` 只用
它做**相对比较**（通过位掩码筛出「有哪些桶被填过」），从不读具体数值。
真正被读取的只有 `bucket_bitmask`。

---

## 3. `bucket_increase()`：泄漏积分器

[walt.c:1196](../../kernel/kernel/sched/walt/walt.c#L1196)：

```c
#define INC_STEP          8
#define DEC_STEP          2
#define CONSISTENT_THRES  16
#define INC_STEP_BIG      16

static inline void bucket_increase(u8 *buckets, u16 *bucket_bitmask, int idx)
{
        int i, step;

        for (i = 0; i < NUM_BUSY_BUCKETS; i++) {
                if (idx != i) {
                        if (buckets[i] > DEC_STEP)
                                buckets[i] -= DEC_STEP;
                        else {
                                buckets[i] = 0;
                                *bucket_bitmask &= ~BIT_MASK(i);   /* 清位 */
                        }
                } else {
                        step = buckets[i] >= CONSISTENT_THRES ?
                                                INC_STEP_BIG : INC_STEP;
                        if (buckets[i] > U8_MAX - step)
                                buckets[i] = U8_MAX;
                        else
                                buckets[i] += step;
                        *bucket_bitmask |= BIT_MASK(i);            /* 置位 */
                }
        }
}
```

**每次窗口结束只调用一次**，命中桶 `idx` 增，另外 15 个桶全部减。
这就是「泄漏积分器」：一个桶即使不再被命中，也要 8 个窗口
（`16 / DEC_STEP`）才归零——**恰好略长于 `RAVG_HIST_SIZE = 5`**。

> **设计推论** `[推测]`：衰减窗口数（8）比历史深度（5）长，
> 意味着「被填过的桶」会**比历史记忆存活更久**。
> 这正是这个机制相对 `demand`（只看 5 窗口最大值）的优势：
> 它能记住「这个任务在前 8 个窗口的某个时刻处于某个量级」。

`BIT_MASK(i)` 的置位/清位是**冗余但便宜**的一致性维护——
清位发生在计数归零的同一分支里，所以位掩码严格等价于
「该桶计数 > 0」。

---

## 4. `get_pred_busy()`：从桶反查历史

[walt.c:1256](../../kernel/kernel/sched/walt/walt.c#L1256)。这是预测算法的核心。

### 4.1 输入与输出

| 参数 | 含义 |
|---|---|
| `p` | 目标任务 |
| `start` | 起始桶，由 `busy_to_bucket(curr_window_scaled)` 得来 |
| `runtime_scaled` | 当前窗口的忙时（1024 刻度）|
| `bucket_bitmask` | 哪些桶被填过 |

返回值：**不小于 `runtime_scaled`** 的预测忙时。

### 4.2 四步

```c
/* 步骤 1：新任务直接放弃预测 */
if (unlikely(is_new_task(p)))
        goto out;                        /* ret = runtime_scaled */

/* 步骤 2：找 >= start 的、被填过的最低桶 */
next_mask = bucket_bitmask >> start;
if (next_mask)
        first = ffs(next_mask) - 1 + start;
if (first >= NUM_BUSY_BUCKETS)           /* 没有更高的桶 */
        goto out;

/* 步骤 3：确定该桶的数值范围 */
final = first;
if (final < 2) {                         /* 最低两桶合并 */
        dmin = 0;
        final = 1;
} else {
        dmin = final << (SCHED_CAPACITY_SHIFT - NUM_BUSY_BUCKETS_SHIFT);
}
dmax = (final + 1) << (SCHED_CAPACITY_SHIFT - NUM_BUSY_BUCKETS_SHIFT);

/* 步骤 4：在历史里找落进这个范围的具体值 */
for (i = 0; i < RAVG_HIST_SIZE; i++) {
        if (hist_util[i] >= dmin && hist_util[i] < dmax) {
                ret = hist_util[i];
                break;
        }
}
if (ret < dmin)
        ret = (u16)(((u32)dmin + dmax) / 2);   /* 兜底：取桶中点 */

ret = max(runtime_scaled, ret);                /* 下限保护 */
```

### 4.3 关键设计点

**(a) 只向上取，不向下取**

`next_mask = bucket_bitmask >> start` 后取 `ffs()`（最低置位），
保证选出的桶 **`>= start`**。这四行是 WALT「宁可高估」哲学的体现——
预测值永远不会低于任务的当前忙时所处的桶。

**(b) 桶决定「量级」，历史决定「具体值」**

桶本身只是个粗粒度的范围（宽 64 个 1024 刻度单位）。
真正返回的值优先来自 `sum_history_util[]`——即**历史窗口的真实忙时**中，
第一个落进该桶的值。

「第一个」在 `sum_history_util[]` 里的遍历顺序是**从旧到新**
（索引 0 是最旧的，见 [data-structures.md](../00-overview/03-data-structures.md#6-walt_task_struct)）。
所以要返回的是「该桶内**最早**的历史样本」。

> `[推测]` 这与函数的文档注释 "returns **the latest** that falls into the bucket"
> 有出入。代码实际返回的是遍历中**第一个**匹配项。
> 因为环形缓冲的物理序不随时间前进而移动（`cidx` 才是「最新」标记），
> 数组序 ≠ 时间序。两者是否等价取决于 `cidx` 的值。
> **【待确认】** 此处语义存疑，已在 [04-open-questions.md](../03-comparison/04-open-questions.md) 登记。

**(c) 兜底值取桶中点**

如果历史里没有任何样本落进该桶（该桶是「陈旧记忆」，历史已经滚出去了），
就用 `(dmin + dmax) / 2`——**桶中值**。

**(d) `ret = max(runtime_scaled, ret)`**

即使桶给出的值更低，也至少返回当前忙时。注释解释了原因：

> when updating in middle of a window, runtime could be higher than all
> recorded history. Always predict at least runtime.

### 4.4 新任务不做预测

```c
if (unlikely(is_new_task(p)))
        goto out;
```

`is_new_task()` 即 `wts->active_time == 0`——任务刚 fork 出来，
没有历史可用，直接返回 `runtime_scaled`。

---

## 5. `update_task_pred_demand()`：何时重算

[walt.c:1321](../../kernel/kernel/sched/walt/walt.c#L1321)。

### 5.1 触发条件（两道过滤）

```c
if (is_idle_task(p))
        return;

/* 过滤 1：事件类型 */
if (event != PUT_PREV_TASK && event != TASK_UPDATE &&
        (!SCHED_FREQ_ACCOUNT_WAIT_TIME ||
         (event != TASK_MIGRATE && event != PICK_NEXT_TASK)))
        return;

/* 过滤 2：TASK_UPDATE 且任务在睡眠 */
if (event == TASK_UPDATE) {
        if (!p->on_rq && !SCHED_FREQ_ACCOUNT_WAIT_TIME)
                return;
}
```

**只在这些事件上重算**：`PUT_PREV_TASK`、`TASK_UPDATE`、
以及（当 `SCHED_FREQ_ACCOUNT_WAIT_TIME` 开启时）`TASK_MIGRATE` / `PICK_NEXT_TASK`。

**`TASK_WAKE` 和 `IRQ_UPDATE` 永不触发预测更新。**

### 5.2 单调性守卫

```c
curr_window_scaled = scale_time_to_util(wts->curr_window);
if (wts->pred_demand_scaled >= curr_window_scaled)
        return;
```

**预测值绝不会被下调到这里**——只有当当前窗口忙时**超过**了已有预测，
才重新计算。所以 `pred_demand_scaled` 在一个窗口内是**单调不减**的，
在新窗口开始时才可能因 `get_pred_busy()` 重算而下降。

### 5.3 同步更新 CPU 侧聚合

```c
new_pred_demand_scaled = get_pred_busy(p, busy_to_bucket(curr_window_scaled),
                                       curr_window_scaled, wts->bucket_bitmask);

if (task_on_rq_queued(p) && (!task_has_dl_policy(p) || !p->dl.dl_throttled))
        fixup_walt_sched_stats_common(rq, p, wts->demand_scaled,
                                      new_pred_demand_scaled);

wts->pred_demand_scaled = new_pred_demand_scaled;
```

注意 `fixup_walt_sched_stats_common()` 的**第一个参数传的是旧的 `demand_scaled`**
（不变），第二个传新预测值。函数内部（[walt.c:342](../../kernel/kernel/sched/walt/walt.c#L342)）计算：

```c
s64 task_load_delta    = (s64)updated_demand_scaled      - wts->demand_scaled;       /* = 0 */
s64 pred_demand_delta  = (s64)updated_pred_demand_scaled - wts->pred_demand_scaled;
```

**这是一个「只改预测、不动负载」的调用惯用法**——
传 `wts->demand_scaled` 自身使 `task_load_delta` 恒为 0。

---

## 6. 聚合与消费

### 6.1 `fixup_cumulative_runnable_avg()`

[walt.c:307](../../kernel/kernel/sched/walt/walt.c#L307)。这是所有
`demand_scaled` / `pred_demand_scaled` 增减的**唯一汇总点**：

```c
s64 cumulative_runnable_avg_scaled =
        stats->cumulative_runnable_avg_scaled + demand_scaled_delta;
s64 pred_demands_sum_scaled =
        stats->pred_demands_sum_scaled + pred_demand_scaled_delta;

if (cumulative_runnable_avg_scaled < 0) {
        WALT_BUG(WALT_BUG_WALT, p, "on CPU %d task ds=%llu is higher than cra=%llu\n", ...);
        cumulative_runnable_avg_scaled = 0;
}
stats->cumulative_runnable_avg_scaled = (u64)cumulative_runnable_avg_scaled;

if (pred_demands_sum_scaled < 0) {
        WALT_BUG(WALT_BUG_WALT, p, "on CPU %d task pds=%llu is higher than pds_sum=%llu\n", ...);
        pred_demands_sum_scaled = 0;
}
stats->pred_demands_sum_scaled = (u64)pred_demands_sum_scaled;
```

> **`WALT_BUG` 的可观测性**：这条日志说明「某任务的 demand_scaled
> 比它所在 rq 的累计值还大」——即**减得比加得多**。
> 排查现场问题时，这条 dmesg 直接指向一处增减不配对。

另外还有一条上游断言：

```c
if (task_rq(p) != rq)
        WALT_BUG(WALT_BUG_UPSTREAM, p, "on CPU %d task %s(%d) not on rq %d", ...);
```

即「往 rq X 上加/减，但这个任务现在不在 rq X 上」——迁移期间的竞态。

### 6.2 三个消费点

`pred_demands_sum_scaled` 被读取的位置：

| 位置 | 用途 |
|---|---|
| [walt.c:656](../../kernel/kernel/sched/walt/walt.c#L656) | `__cpu_util_freq_walt()` → 导出 `walt_load->pl` |
| [walt.c:1591](../../kernel/kernel/sched/walt/walt.c#L1591) | `do_pl_notif()` → 决定是否**立即加大核频率** |
| [walt.c:4364](../../kernel/kernel/sched/walt/walt.c#L4364) / :4388 | 窗口滚动时清零 |

### 6.3 `do_pl_notif()`：预测触发的紧急加频

[walt.c:1587](../../kernel/kernel/sched/walt/walt.c#L1587)：

```c
static bool do_pl_notif(struct rq *rq)
{
        u64 prev = wrq->old_busy_time;
        u64 pl = wrq->walt_stats.pred_demands_sum_scaled;
        int cpu = cpu_of(rq);

        /* If already at max freq, bail out */
        if (capacity_orig_of(cpu) == capacity_curr_of(cpu))
                return false;

        prev = max(prev, wrq->old_estimated_time);

        /* 400 MHz filter. */
        return (pl > prev) && (load_to_freq(rq, pl - prev) > 400000);
}
```

**这是 `pred_demand` 最重要的用途**：如果本窗口预测的负载比
上一窗口的实际负载高出「超过 400 MHz 的等效频率」，
则**不等下一个窗口，立即提升频率**。

三个门槛：
1. CPU 不在最高频（在最高频就无事可做）
2. `pl > prev`（预测必须**超过**历史，`prev` 取 `old_busy_time` 与
   `old_estimated_time` 的较大者）
3. 差值换算成频率要 **> 400 MHz**——滤掉小抖动

`old_busy_time` / `old_estimated_time` 在 `__cpu_util_freq_walt()`
[walt.c:658-659](../../kernel/kernel/sched/walt/walt.c#L658-L659) 中更新：

```c
wrq->old_busy_time      = util;
wrq->old_estimated_time = pl;
```

即：**每次调频时把「本次的实测」和「本次的预测」都存下来，
供下一次 `do_pl_notif()` 比较。**

> `[推测]` 这是 WALT 里唯一的**亚窗口响应**机制。其余所有决策都要
> 等窗口边界（见 [data-flow.md](../00-overview/04-data-flow.md#4-消费方-1调频窗口滚动驱动)），
> 而这里允许在一个窗口**内部**触发加频。

### 6.4 与 `demand` 的关系

| | `demand_scaled` | `pred_demand_scaled` |
|---|---|---|
| 来源 | 5 窗口历史的策略值（默认 MAX）| 16 桶直方图外推 |
| 更新时机 | 窗口结束（`update_history`）| 窗口内多次（见 §5.1）|
| 窗口内单调 | 是（窗口结束才变）| 是（§5.2 守卫）|
| 主要消费 | placement / core_ctl | **调频**（`do_pl_notif`）|
| 聚合字段 | `cumulative_runnable_avg_scaled` | `pred_demands_sum_scaled` |

> **两条独立通道**：`cumulative_runnable_avg_scaled` 服务于「放哪 / 开几个核」
> （空间决策，变化慢），`pred_demands_sum_scaled` 服务于「跑多快」
> （时间决策，需要快响应）。这是一个有意的**解耦**。`[推测]`

---

## 7. 调用链小结

```
窗口结束（update_history）
   └─ predict_and_update_buckets()
        └─ bucket_increase(buckets, bitmask, busy_to_bucket(...))
             └─ 命中桶 +8/+16，其余桶 -2（归零则清位）

窗口内（PUT_PREV / TASK_UPDATE / ... 事件）
   └─ update_task_pred_demand()
        ├─ if (pred_demand_scaled >= curr_window_scaled) return;   ← 单调守卫
        ├─ get_pred_busy()  ← 桶 + 历史 + 桶中点兜底
        └─ fixup_walt_sched_stats_common()  ← 更新 rq 聚合

调频时读取
   └─ do_pl_notif()  → 预测超出实测 400MHz 等效 → 立即加频
```

---

## 8. 相关文档

- 窗口与历史环 → [window-model.md](01-window-model.md)
- 预测值如何进入调频 → [cpufreq.md](03-cpufreq.md)
- 字段定义 → [03-data-structures.md](../00-overview/03-data-structures.md)
- `get_pred_busy` 的遍历序疑点 → [04-open-questions.md](../03-comparison/04-open-questions.md)
