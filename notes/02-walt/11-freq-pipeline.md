# 从窗口到频率：一次完整的调频计算流水线

> **源码**：[cpufreq_walt.c](../../kernel/kernel/sched/walt/cpufreq_walt.c)、[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)、[core.c](../../kernel/kernel/sched/core.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

---

## 0. 本文的视角与边界

同一件事——「把 WALT 统计变成频率」——本仓库已有三份文档从不同切面写过：

| 文档 | 切面 |
|---|---|
| [03-cpufreq.md](03-cpufreq.md) | **文件归属**：哪个函数在 `walt.c`、哪个在 `cpufreq_walt.c`，数据结构长什么样 |
| [02-cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md) | **逐函数差异**：schedutil 的对应函数 vs WALT 的对应函数 |
| [04-data-flow.md](../00-overview/04-data-flow.md) | **事件流**：六种事件如何驱动三条消费路径 |

**本文是第四种切面：时间轴 / 数据变换视图。** 它只回答一个问题：

> 一个窗口结束的那一刻，到 `cpufreq` 驱动真正写寄存器为止，
> 数据被**依次**做了哪些变换？每一步的**输入是什么、输出是什么、单位是什么**？

所以本文按**流水线阶段**编号，而不是按文件或函数编号；并且全程追踪一个
具体的数值算例（§10），让每一步的中间值都可见。函数细节请查上面三份文档，
本文不重复。

---

## 1. 九阶段全景

| 阶段 | 环节 | 函数 | 输出 | 单位 |
|---|---|---|---|---|
| 0 | 频率归一化后的忙时累积 | `update_cpu_busy_time` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678) | `wrq->curr_runnable_sum` | ns（已归一化）|
| 1 | 窗口滚动 `curr` → `prev` | `rollover_cpu_window` [walt.c:1604](../../kernel/kernel/sched/walt/walt.c#L1604) | `wrq->prev_runnable_sum` | ns（已归一化）|
| 2 | 全局聚合 + 发起回调 | `__walt_irq_work_locked` [walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992) | `cluster->aggr_grp_load`；waltgov 回调（带 `flags`）| ns（已归一化）|
| 3 | 负载源四路取最大 | `freq_policy_load` [walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590) | `load` + `reason` | ns |
| 4 | 时间 → util | `scale_time_to_util` [walt.h:986](../../kernel/kernel/sched/walt/walt.h#L986) | `util` | 0–1024 |
| 5 | uclamp 钳制 | `uclamp_rq_util_with` [sched.h:2947](../../kernel/kernel/sched/sched.h#L2947)（调用点 [cpufreq_walt.c:285](../../kernel/kernel/sched/walt/cpufreq_walt.c#L285)）| `util`（钳制后）| 0–1024 |
| 6 | 簇内取最大 + 六路 boost | `waltgov_next_freq_shared` [cpufreq_walt.c:364](../../kernel/kernel/sched/walt/cpufreq_walt.c#L364) | `util`、`max`、`driving_cpu` | 0–1024 / CPU 号 |
| 7 | util → 频率 | `walt_map_util_freq` [cpufreq_walt.c:209](../../kernel/kernel/sched/walt/cpufreq_walt.c#L209) | `raw_freq` | kHz |
| 8 | 整形 + 限速 + 写频率 | `get_next_freq` [cpufreq_walt.c:237](../../kernel/kernel/sched/walt/cpufreq_walt.c#L237)、`waltgov_update_next_freq` [cpufreq_walt.c:124](../../kernel/kernel/sched/walt/cpufreq_walt.c#L124) | `next_freq` → cpufreq 驱动 | kHz |

**阶段之间的驱动关系**：阶段 0 由任务调度事件（enqueue / dequeue / tick / 迁移）
逐次累加；窗口边界触发阶段 1 与阶段 2；阶段 3–8 全部在阶段 2 发起的那次
waltgov 回调内完成。

### 1.1 阶段 0 是三路分叉点

表格第 0 行只列了 CPU 忙时那一支。窗口边界的记账实际发生在
`walt_update_task_ravg` [walt.c:2288](../../kernel/kernel/sched/walt/walt.c#L2288)
内，它并列调用三个函数：

| 支路 | 函数 | 产出 | 去向 |
|---|---|---|---|
| 任务需求 | `update_task_demand` [walt.c:2127](../../kernel/kernel/sched/walt/walt.c#L2127)、`update_history` [walt.c:1984](../../kernel/kernel/sched/walt/walt.c#L1984) | `wts->demand`、`wts->demand_scaled` | **placement 为主**；回到调频链的唯一入口是 ksoftirqd（阶段 3 第 2 路）|
| CPU 忙时 | `update_cpu_busy_time` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678) | `wrq->curr_runnable_sum` | 本流水线阶段 1→8 |
| 需求预测 | `update_task_pred_demand` [walt.c:1321](../../kernel/kernel/sched/walt/walt.c#L1321) | `wts->pred_demand_scaled` → `pred_demands_sum_scaled` | 本流水线阶段 3 第 4 路（`pl`）|

三者是**并列关系而非先后关系**——同一次调度事件、同一个 `wallclock`、
同一个入口函数，且**各自都只有这一个调用点**（`update_task_demand`
[walt.c:2311](../../kernel/kernel/sched/walt/walt.c#L2311)、
`update_cpu_busy_time` [walt.c:2312](../../kernel/kernel/sched/walt/walt.c#L2312)、
`update_task_pred_demand` [walt.c:2313](../../kernel/kernel/sched/walt/walt.c#L2313)）。
所以 `demand` 不是「更靠前的阶段」，而是阶段 0 处分出去、
主要流向 placement 的另一条链。

`demand` 与 `pred_demand` 回流调频的入口不同，不要混为一谈：

- `demand` → 仅经 ksoftirqd（`task_load()` 返回的就是 `demand`），见 §5.3
- `pred_demand` → 经 `pred_demands_sum_scaled` [walt.c:656](../../kernel/kernel/sched/walt/walt.c#L656)
  进入 `pl`，即阶段 3 第 4 路

**一句话概括整条链**：忙时（ns）→ 归一化忙时（ns）→ 上一窗口的归一化忙时
（ns）→ 取最大后的负载（ns）→ util（0-1024）→ uclamp 后的 util → 簇内最大
util → 频率（kHz）→ 限速整形后的频率（kHz）。

> **关键认知**：这条链**不是**从「平均负载百分比」出发的，而是从「时间」出发的。
> WALT 的 util 本质是 **「一个窗口内被占用的、折算到参考频率的时间」**，
> 只在最后一步才被当作「容量占比」使用。

---

## 2. 阶段 0：频率归一化——为什么 util 与频率无关

这是整条链的**地基**，也是 WALT 最容易被忽略的设计。如果跳过这一步，
后面所有数值都会随当前频率漂移，导致反馈震荡。

### 2.1 忙时不是直接累加的

任务在 `[mark_start, wallclock)` 内运行的时间 `delta` **不直接**进计数器，
而是先过 `scale_exec_time` [walt.c:1566](../../kernel/kernel/sched/walt/walt.c#L1566)：

```c
static inline u64 scale_exec_time(u64 delta, struct rq *rq, struct walt_task_struct *wts)
{
	struct walt_rq *wrq = (struct walt_rq *) rq->android_vendor_data1;

	delta = (delta * wrq->task_exec_scale) >> SCHED_CAPACITY_SHIFT;
	...
}
```

`task_exec_scale` 在 `update_task_rq_cpu_cycles` 中算出
[walt.c:2215](../../kernel/kernel/sched/walt/walt.c#L2215)（不用 cycle counter 的分支）：

```c
wrq->task_exec_scale = DIV64_U64_ROUNDUP(cpu_cur_freq(cpu) *
		arch_scale_cpu_capacity(cpu),
		wrq->cluster->max_possible_freq);
```

即：

```
task_exec_scale = 当前频率 × 该 CPU 容量 / 簇最大频率
scaled_delta    = delta × task_exec_scale >> 10
```

**语义**：把「在 `f` 频率下跑了 `delta` 纳秒」折算成「在满频下需要跑多久」。
频率越高，同样的墙钟时间折算出的忙时越长（因为「占用」了更多算力）。

### 2.2 由此推出的一个不变式

一个窗口内**满负载**的 CPU（跑满 `sched_ravg_window`），其
`curr_runnable_sum` 恰好等于 `sched_ravg_window × (f / f_max)`。
再经阶段 4 除以 `sched_ravg_window >> 10`，得到：

```
满负载 CPU 的 util = 1024 × f / f_max
```

**这就是归一化的意义**：util 只反映「算力被用掉了多少」，
不反映「当时跑在什么频率上」。读源码时若忘记这一点，
会误以为频率越高 util 越大。

累积点见 `update_cpu_busy_time`
[walt.c:1749-1752](../../kernel/kernel/sched/walt/walt.c#L1749-L1752)：

```c
delta = scale_exec_time(delta, rq, wts);
*curr_runnable_sum += delta;
if (new_task)
	*nt_curr_runnable_sum += delta;
```

> `nt_` 前缀 = **new task**，只统计本窗口内新创建的任务，用于识别
> 「新任务突发」。它不参与 normal load，只喂给 `nl`（见阶段 6 的 NWD boost）。
> 注意这里的 `nt` **不是** "net" 或 "non-top"。

### 2.3 三套并行的计数器

同一次累加会写进三个不同归属的计数器，它们的区别是**「这笔时间算谁的」**：

| 计数器 | 归属 | 消费方 |
|---|---|---|
| `wrq->curr_runnable_sum` | 当前 CPU | CPU 自身负载 |
| `wts->curr_window` → 任务需求 | 任务 | placement、pred_demand |
| `wrq->grp_time.curr_runnable_sum` | 该 CPU 上的 RTG 组 | `aggr_grp_load` 聚合（阶段 2） |

`grp_time` 是**组时间**：只有当任务属于某个 RTG 时才计入。
它是阶段 2 里 `cluster->aggr_grp_load` 的唯一来源。

---

## 3. 阶段 1：窗口滚动——`curr` 变 `prev`

窗口边界到达时，`rollover_cpu_window`
[walt.c:1604](../../kernel/kernel/sched/walt/walt.c#L1604) 做一次整体搬家：

```c
wrq->prev_runnable_sum = curr_sum;                    /* :1619 */
wrq->nt_prev_runnable_sum = nt_curr_sum;              /* :1620 */
wrq->grp_time.prev_runnable_sum = grp_curr_sum;       /* :1621 */
wrq->grp_time.nt_prev_runnable_sum = grp_nt_curr_sum; /* :1622 */

wrq->curr_runnable_sum = 0;
wrq->nt_curr_runnable_sum = 0;
wrq->grp_time.curr_runnable_sum = 0;
wrq->grp_time.nt_curr_runnable_sum = 0;
```

两个要点：

1. **调频看的是 `prev_*`，不是 `curr_*`。** 因为窗口刚结束时 `curr` 已经被清零，
   而「上一个完整窗口」才是可信的负载样本。这也解释了为什么调频是
   **窗口驱动**而非实时驱动——它天然有一个窗口的延迟。
2. `full_window` 为真时先清零再搬（[walt.c:1612-1617](../../kernel/kernel/sched/walt/walt.c#L1612-L1617)），
   用于「中间丢了若干个窗口」的情形，避免把陈旧的 `curr` 当成新鲜数据。

---

## 4. 阶段 2：全局聚合与回调发起

### 4.1 为什么要一个「先锁住所有 CPU」的批处理

`__walt_irq_work_locked` [walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992)
不是针对单个 CPU 的，它**遍历所有簇、所有 CPU**。原因是调频要跨簇比较
（阶段 6 的 `aggr_grp_load` 需要先知道全局态势），而各簇的统计必须被
**同一时刻**冻结，否则会出现「A 簇用新数据、B 簇用旧数据」的错配。
调用方因此需要在进入前把所有相关 CPU 的 rq 锁住
（窗口滚动场景锁 `cpu_possible_mask`，迁移场景只锁涉及的两个簇）。

### 4.2 聚合出 `aggr_grp_load`

[walt.c:4010-4033](../../kernel/kernel/sched/walt/walt.c#L4010-L4033)：

```c
for_each_cpu(cpu, &cluster->cpus) {
	...
	/* update aggr_grp_load for all clusters, all cpus */
	aggr_grp_load += wrq->grp_time.prev_runnable_sum;
}
...
cluster->aggr_grp_load = aggr_grp_load;
```

注意第 4021 行的注释 **"for all clusters, all cpus"**——即使某个 CPU 不在
`lock_cpus` 里（它的统计没有刷新），它的 `grp_time.prev_runnable_sum` 也照样
被加进来。这是刻意的：宁可加一个「上一窗口的旧值」，也不要丢掉一个组。

非对称容量机器上还有一次特殊处理
[walt.c:4040-4047](../../kernel/kernel/sched/walt/walt.c#L4040-L4047)：
把「总组负载 − 最小簇组负载」赋给所有 `asym_cap_sibling_cpus`，
让同容量但不同簇的 CPU 看到同一个大核组负载。

### 4.3 `WALT_CPUFREQ_CONTINUE`：为什么只有最后一个 CPU 算频率

[walt.c:4089-4093](../../kernel/kernel/sched/walt/walt.c#L4089-L4093)：

```c
if (i == num_cpus)
	waltgov_run_callback(cpu_rq(cpu), wflag);
else
	waltgov_run_callback(cpu_rq(cpu), wflag | WALT_CPUFREQ_CONTINUE);
```

一个簇里有 `num_cpus` 个 CPU，回调被发起 `num_cpus` 次，
但**只有最后一次不带 `CONTINUE`**。消费侧
[cpufreq_walt.c:437-438](../../kernel/kernel/sched/walt/cpufreq_walt.c#L437-L438)：

```c
if (waltgov_should_update_freq(wg_policy, time) &&
    !(flags & WALT_CPUFREQ_CONTINUE)) {
	next_f = waltgov_next_freq_shared(wg_cpu, time);
	...
}
```

**设计意图**：`waltgov_next_freq_shared` 要遍历**整个 policy** 的所有 CPU
（阶段 6），所以它只需要被触发一次。如果每个 CPU 都触发，同一个 policy 会被
重复计算 `num_cpus` 次，且每次都拿到不同时刻的快照，纯属浪费且可能引入不一致。
带 `CONTINUE` 的调用仍然完成了一半工作——注意
[cpufreq_walt.c:414](../../kernel/kernel/sched/walt/cpufreq_walt.c#L414) 的
`wg_cpu->util = waltgov_get_util(wg_cpu)` 在 `CONTINUE` 检查**之前**：

```c
if (!wg_policy->tunables->pl && flags & WALT_CPUFREQ_PL)
	return;

wg_cpu->util = waltgov_get_util(wg_cpu);   /* ← 先算好，供后续聚合用 */
wg_cpu->flags = flags;
raw_spin_lock(&wg_policy->update_lock);
...
if (waltgov_should_update_freq(wg_policy, time) &&
    !(flags & WALT_CPUFREQ_CONTINUE)) {    /* ← 只有这里被跳过 */
```

**这就是协议的全部**：每个 CPU 都刷新自己的 `wg_cpu->util`，
但只有最后一个负责汇总。所以 `CONTINUE` 不是「跳过这个 CPU」，
而是「**先记账，先别汇总**」。

> `wflag` 的取值见 [walt.h:321-326](../../kernel/kernel/sched/walt/walt.h#L321-L326)。
> 窗口滚动路径置 `WALT_CPUFREQ_ROLLOVER`(0x1)，
> 迁移路径置 `WALT_CPUFREQ_IC_MIGRATION`(0x4)。

---

## 5. 阶段 3：`freq_policy_load()` —— 四路取最大

[freq_policy_load walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590)

这是**唯一**决定「用哪个数当负载」的地方。四路候选，
**规则是取最大值，不是求和**：

| # | 来源 | 条件 | reason |
|---|---|---|---|
| 1 | `prev_runnable_sum + aggr_grp_load` | `sched_freq_aggr_en` | `FREQ_AGR` (0x40) |
| 1' | `prev_runnable_sum + grp_time.prev_runnable_sum` | 否则 | （沿用初始值） |
| 2 | `task_load(cpu_ksoftirqd)` | ksoftirqd 处于 `TASK_RUNNING` | `KSOFTIRQD` (0x80) |
| 3 | `top_task_load(rq)` | 顶任务负载更大 | `TT_LOAD` (0x100) |
| 4 | `load × sysctl_sched_user_hint / 100` | `should_apply_suh_freq_boost()` | `SUH` (0x200) |

### 5.1 为什么是取最大

因为四路描述的是**同一份时间的四种不同切法**，彼此高度重叠：

- 路径 1 是常规的「CPU 上一个窗口有多忙」；
- 路径 2 是「其中 ksoftirqd 占了多久」——软中断多时它代表真实的 CPU 压力，
  而它可能因为跑在别的记账口径下而不体现在 `prev_runnable_sum` 里；
- 路径 3 是「最重的单个任务」，防止一个重任务被众多轻任务稀释；
- 路径 4 是「上层明确要求提速」。

**求和会重复计算**（同一个任务的运行时间既在路径 1 也在路径 3 里），
所以只能取最大。这与 `schedutil` 的 `cpu_util_cfs()` 直接加总
cfs+rt+dl+irq 的做法形成对比——PELT 的几路是**互斥的时间片**，
WALT 的这几路是**同一时间的不同视角**。

### 5.2 `top_task_load()` 的数据来源 —— 一张量化直方图

[top_task_load walt.c:557](../../kernel/kernel/sched/walt/walt.c#L557)
读的是 `wrq->prev_top`——**上一个窗口**的最高单任务桶号。但它只是消费者，
真正的生产者链是：

```
wts->curr_window                              ← 任务忙时（已 scale_exec_time 归一化）
  │  update_cpu_busy_time() 收尾 [walt.c:1920-1922]
  ▼
update_top_tasks()          [walt.c:1372]     ← 全树唯一调用者
  ├─ new_index = load_to_index(curr_window)   [walt.c:925]
  │            = min(curr_window / sched_load_granule, NUM_LOAD_INDICES-1)
  ├─ curr_table[new_index] += 1               ← u8 计数
  ├─ if (new_index > curr_top) curr_top = new_index
  └─ __set_bit(NUM_LOAD_INDICES-1-new_index, top_tasks_bitmap[curr])
  │
  │  窗口边界
  ▼
rollover_top_tasks()        [walt.c:1471]
  │  清空「旧 prev」缓冲（交换后成为新 curr）  [walt.c:1478-1479]
  │  if (full_window) { curr_top = 0; 连 curr 缓冲一起清空 } [walt.c:1481-1485]
  │  curr_table = prev_table          ← 缓冲交换，非拷贝  [walt.c:1487]
  │  prev_top = curr_top;  curr_top = 0;                    [walt.c:1488-1489]
  ▼
top_task_load(rq)           [walt.c:557]
```

**数据源是 `wts->curr_window`（任务忙时），不是 `demand`、也不是 `pred_demand`。**
这与 §5.3 的结论一致：调频侧只关心「CPU 多忙」，顶任务只是它的一个细化视角。

#### 两个数据结构（双缓冲）

| | 类型 | 作用 |
|---|---|---|
| `wrq->top_tasks[2]` | `u8 *` × `NUM_LOAD_INDICES` | 每个负载桶里的**任务计数** |
| `wrq->top_tasks_bitmap[2]` | 位图 × `NUM_LOAD_INDICES` bit | 哪些桶**非空** |

`[2]` = `NUM_TRACKED_WINDOWS`
[walt.h:82-83](../../kernel/kernel/sched/walt/walt.h#L82-L83)，
由 `wrq->curr_table` 选择当前缓冲；`kcalloc` 于
[walt.c:4399](../../kernel/kernel/sched/walt/walt.c#L4399)。
两个都要：表支持按任务增减计数，位图支持 O(1) 找最高非空桶——
只有表就得线性扫 1000 项。

#### 三个反直觉点

1. **索引是倒置的**。`__set_bit(NUM_LOAD_INDICES - new_index - 1, ...)`
   [walt.c:1408](../../kernel/kernel/sched/walt/walt.c#L1408)——
   负载越大，置的 bit 位置**越小**。于是 `find_next_bit()` 找到的第一个
   置位 bit 恰好就是最高负载桶，`get_top_index()`
   [walt.c:769](../../kernel/kernel/sched/walt/walt.c#L769)
   再翻回来：`return NUM_LOAD_INDICES - 1 - index`。
   这个倒置纯粹是为了让 `find_next_bit` 能直接用。

2. **`sched_load_granule` 用编译期默认窗口算，不是运行时窗口**
   [sysctl.c:1126](../../kernel/kernel/sched/walt/sysctl.c#L1126)：
   `DEFAULT_SCHED_RAVG_WINDOW / NUM_LOAD_INDICES` = 16,000,000 / 1000
   = **16,000 ns per 桶**。注释
   [walt.c:72-77](../../kernel/kernel/sched/walt/walt.c#L72-L77)
   解释了原因（初始化时 `sched_ravg_window` 尚未确定）。
   副作用：1000 × 16μs 刚好覆盖 16ms 默认窗口；若运行时窗口调成 20ms，
   满负载会算出 index 1250，被 `min(..., NUM_LOAD_INDICES-1)` 钳到最高桶。

3. **返回值是桶下界的向上取整，刻意保守**
   [walt.c:573](../../kernel/kernel/sched/walt/walt.c#L573)：
   `(index + 1) * sched_load_granule`。实际负载落在
   `[index × g, (index+1) × g)`，返回值取 `(index+1) × g`，
   即「**至少这么多**」——宁可高估也不低估。

#### 两条边界

```c
if (!index) {                        /* prev_top == 0 */
	int msb = NUM_LOAD_INDICES - 1;
	if (!test_bit(msb, wrq->top_tasks_bitmap[prev]))
		return 0;
	else
		return sched_load_granule;
} else if (index == NUM_LOAD_INDICES - 1) {
	return sched_ravg_window;
}
```

- `index == 0` **不等于**负载为 0——0 号桶的存在性无法从这个字段判断。
  所以改查最高位 bit 999：有任务在最高桶就返回 `sched_load_granule`，
  否则才返回 0。
- `index == NUM_LOAD_INDICES - 1`：返回满窗口 `sched_ravg_window`。

#### 其余要点

- 只对**非 idle 任务**更新
  [walt.c:1920](../../kernel/kernel/sched/walt/walt.c#L1920)
- 任务迁移会搬桶：`migrate_top_tasks_subtraction`
  [walt.c:932](../../kernel/kernel/sched/walt/walt.c#L932) /
  `migrate_top_tasks_addition`
  [walt.c:975](../../kernel/kernel/sched/walt/walt.c#L975)
- `full_window` 时 `curr_top` 一并清零
  [walt.c:1482](../../kernel/kernel/sched/walt/walt.c#L1482)——
  窗口丢了，curr 表的语义已不可信
- 调频读到的 `prev_top` 总是**上一个完整窗口**的值，与
  `prev_runnable_sum` 的时序一致

### 5.3 task demand 会进入这条流水线吗？—— 只有一条窄通道 [重要]

**结论：`wts->demand` 与 cpufreq 之间只有唯一一条通路——ksoftirqd。**

上表第 2 路（[walt.c:606-612](../../kernel/kernel/sched/walt/walt.c#L606-L612)）
调用的 `task_load()` 就是 demand：

```c
if (cpu_ksoftirqd && READ_ONCE(cpu_ksoftirqd->__state) == TASK_RUNNING) {
	kload = task_load(cpu_ksoftirqd);      /* task_load() → wts->demand */
	if (kload > load) {
		load = kload;
		*reason = CPUFREQ_REASON_KSOFTIRQD;
	}
}
```

而 [task_load() walt.h:861](../../kernel/kernel/sched/walt/walt.h#L861)
的实现就是 `return wts->demand;`。`task_load()` 全树消费者只有三类：
这里一处、`update_preferred_cluster()` 的 RTG 路径
（[:2990](../../kernel/kernel/sched/walt/walt.c#L2990)、
[:4771](../../kernel/kernel/sched/walt/walt.c#L4771)、
[:4823](../../kernel/kernel/sched/walt/walt.c#L4823)）、以及 trace。

**为什么这么窄**——因为 demand 与 `runnable_sum` 是同一问题的两个口径：

| | `wts->demand` | `wrq->prev_runnable_sum` |
|---|---|---|
| 归属 | **任务** | **CPU** |
| 含义 | 该任务过去 `RAVG_HIST_SIZE` 个窗口的最大 sum | 该 CPU 上个窗口的忙时 |
| 口径 | 峰值 | 上窗口实测 |

一个 CPU 上「10 个轻任务」与「1 个重任务」可以给出相同的 `runnable_sum`，
但 `demand` 相差极大。**调频问的是「这个 CPU 有多忙」，
不是「上面的任务有多重」**，所以主路径不走 demand。
ksoftirqd 是例外，因为它既是「一个任务」又直接代表 CPU 压力。

#### 三个访问器（易混）

| 访问器 | 位置 | 返回 |
|---|---|---|
| `task_load(p)` | [walt.h:861](../../kernel/kernel/sched/walt/walt.h#L861) | `wts->demand`（ns）|
| `task_util(p)` | [walt.h:430](../../kernel/kernel/sched/walt/walt.h#L430) | `wts->demand_scaled`（0-1024）|
| `task_util_est(p)` | [walt.h:634](../../kernel/kernel/sched/walt/walt.h#L634) | `wts->demand_scaled`（**同左**）|

后两者返回**同一个字段**：WALT 把原生的 `util_est` 语义
直接替换成了 `demand_scaled`。

#### 陷阱：PL 路看起来像 demand，其实不是

上表第 5 路 `pl` → `CPUFREQ_REASON_PL` 名字里带「预测需求」，
但它的来源与 `wts->demand` **无关**：

[update_task_pred_demand walt.c:1345](../../kernel/kernel/sched/walt/walt.c#L1345)

```c
curr_window_scaled = scale_time_to_util(wts->curr_window);   /* 忙时，不是 demand */
```

`pred_demand` 是**任务忙时直方图**的 16 桶预测值，
`demand` 是 sum 历史的峰值。两者是兄弟概念、各自独立计算，
`pred_demand` 只是名字里带 demand。
详见 [02-demand-prediction.md](02-demand-prediction.md)。

#### demand 实际流向哪里：placement

`wts->demand_scaled` 经
[fixup_cumulative_runnable_avg walt.c:4427](../../kernel/kernel/sched/walt/walt.c#L4427)
聚合成 rq 级的 `cumulative_runnable_avg_scaled`，消费方是：

| 消费点 | 位置 | 用途 |
|---|---|---|
| `cpu_util_next_walt()` | [walt_cfs.c:654](../../kernel/kernel/sched/walt/walt_cfs.c#L654) | EAS 能量计算的 util |
| 同上 | [walt_cfs.c:710](../../kernel/kernel/sched/walt/walt_cfs.c#L710) | `util += wts->demand` |
| `rq->misfit_task_load` | [walt.c:4746](../../kernel/kernel/sched/walt/walt.c#L4746) | misfit 判定 → upmigration |
| `rearrange_heavy()` | [walt.c:3854](../../kernel/kernel/sched/walt/walt.c#L3854) | heavy/pipeline 排序 |

**`freq_policy_load()` 不读 `cumulative_runnable_avg_scaled`** ——
这是「demand 不进调频主路径」的直接证据。

> **一句话**：**demand 决定「任务放哪」，`runnable_sum` 决定「CPU 跑多快」**，
> 两者只在 ksoftirqd 这一处交叉。

### 5.4 用户提示（SUH）的两个前提

`should_apply_suh_freq_boost`
[walt.c:581](../../kernel/kernel/sched/walt/walt.c#L581) 有三个与条件：

```c
if (sched_freq_aggr_en || !sysctl_sched_user_hint ||
				  !cluster->aggr_grp_load)
	return false;
return is_cluster_hosting_top_app(cluster);
```

注意第一个条件是 `sched_freq_aggr_en` **为真时直接返回 false**——
即 SUH boost 只在**没有**开启 freq aggregation 时生效。这与直觉相反：
开启更精细的聚合反而关闭了用户提示通道。另外，SUH 还有一个
**自动过期**机制 [walt.c:4053-4055](../../kernel/kernel/sched/walt/walt.c#L4053-L4055)：
窗口滚动时若已过 `sched_user_hint_reset_time` 就把 hint 清零，
防止上层忘记撤销导致频率永久虚高。

---

## 6. 阶段 4：时间 → util

### 6.1 唯一的换算公式

[scale_time_to_util walt.h:986](../../kernel/kernel/sched/walt/walt.h#L986)：

```c
static inline u64 scale_time_to_util(u64 d)
{
	do_div(d, walt_scale_demand_divisor);
	return d;
}
```

除数在 `walt_init_window_dep` 中定义
[walt.c:4336](../../kernel/kernel/sched/walt/walt.c#L4336)：

```c
walt_scale_demand_divisor = sched_ravg_window >> SCHED_CAPACITY_SHIFT;
```

`SCHED_CAPACITY_SHIFT` = 10，窗口 16 ms 时：

```
walt_scale_demand_divisor = 16,000,000 >> 10 = 15625
```

所以 **`util = ns / 15625`**，值域 `[0, 1024]`（满窗口恰好 1024）。

> **这是全文档最值得记住的一个常数**。看到任何一个 WALT 的 ns 数值，
> 除以 15625 就得到它对应的 util。反过来，util × 15625 = ns。

### 6.2 调用点

[__cpu_util_freq_walt walt.c:645](../../kernel/kernel/sched/walt/walt.c#L645)：

```c
util = scale_time_to_util(freq_policy_load(rq, reason));
wrq->util = util;          /* 回写到 rq，供别处读取 */
```

紧接着 [walt.c:661](../../kernel/kernel/sched/walt/walt.c#L661) 把 `nl` 也做同样换算：

```c
nl = scale_time_to_util(nl);
```

注意 `pl` **不需要**换算 [walt.c:656](../../kernel/kernel/sched/walt/walt.c#L656)：

```c
u64 pl = wrq->walt_stats.pred_demands_sum_scaled;
```

因为它**在直方图统计阶段就已经是 util 量纲**了（名字里的 `_scaled` 就是这个
意思，见 [03-glossary.md](../03-comparison/03-glossary.md) §0）。
证据是 `pred_demand_scaled` 本身是 `u16`
[walt.c:1323](../../kernel/kernel/sched/walt/walt.c#L1323)，
并且直接与同样是 util 量纲的 `curr_window_scaled` 比较
[walt.c:1346](../../kernel/kernel/sched/walt/walt.c#L1346)；
`fixup_cumulative_runnable_avg` [walt.c:307](../../kernel/kernel/sched/walt/walt.c#L307)
只是把它按 delta 累加 [walt.c:316-317](../../kernel/kernel/sched/walt/walt.c#L316-L317)，
**中间没有经过任何除以 15625 的换算**。

最后钳制到容量 [walt.c:673](../../kernel/kernel/sched/walt/walt.c#L673)：

```c
return (util >= capacity) ? capacity : util;
```

### 6.3 非对称容量兄弟调整

[cpu_util_freq_walt walt.c:680](../../kernel/kernel/sched/walt/walt.c#L680)
在 util 出来后还有一层：如果本 CPU 属于 `asym_cap_sibling_cpus`，
则把它与「兄弟 CPU」的 util 按 `sysctl_sched_asym_cap_sibling_freq_match_pct`
取加权最大 [walt.c:700](../../kernel/kernel/sched/walt/walt.c#L700)：

```c
util = ADJUSTED_ASYM_CAP_CPU_UTIL(util, util_other, mpct);
```

宏定义 [walt.c:676](../../kernel/kernel/sched/walt/walt.c#L676)：

```c
#define ADJUSTED_ASYM_CAP_CPU_UTIL(orig, other, x)	\
			(max(orig, mult_frac(other, x, 100)))
```

并且 `nl` / `pl` 也被同样处理 [walt.c:702-704](../../kernel/kernel/sched/walt/walt.c#L702-L704) ——
**三个量必须同步调整**，否则阶段 6 的 NWD 判据
（`nl >= cpu_util × 75%`）会因为量纲不一致而误判。
特殊地，`cpumask_last()` 的兄弟被强制 `mpct = 100`
[walt.c:697-698](../../kernel/kernel/sched/walt/walt.c#L697-L698)，
使该 CPU 完全采用兄弟的 util，避免两个兄弟互相引用形成循环。

---

## 7. 阶段 5：uclamp

[waltgov_get_util cpufreq_walt.c:276](../../kernel/kernel/sched/walt/cpufreq_walt.c#L276)：

```c
static unsigned long waltgov_get_util(struct waltgov_cpu *wg_cpu)
{
	struct rq *rq = cpu_rq(wg_cpu->cpu);
	unsigned long max = arch_scale_cpu_capacity(wg_cpu->cpu);
	unsigned long util;

	wg_cpu->max = max;
	wg_cpu->reasons = 0;
	util = cpu_util_freq_walt(wg_cpu->cpu, &wg_cpu->walt_load, &wg_cpu->reasons);
	return uclamp_rq_util_with(rq, util, NULL);
}
```

三个副作用值得注意：

1. `wg_cpu->max = arch_scale_cpu_capacity(cpu)` —— **不是**
   `capacity_orig_of()`。两者在非 overlap 场景下相等，但语义不同：
   阶段 4 的钳制用 `capacity_orig_of` [walt.c:642](../../kernel/kernel/sched/walt/walt.c#L642)，
   阶段 6 的归一化用 `arch_scale_cpu_capacity`。这里用的是后者。
2. `wg_cpu->reasons = 0` 在此清零，之后由 `cpu_util_freq_walt` 和
   `waltgov_walt_adjust` 逐步置位。所以 `reasons` 是**按 CPU** 记录的，
   而最终生效的是 `driving_cpu` 的那一份。
3. `uclamp_rq_util_with(rq, util, NULL)` 第三个参数传 `NULL`
   （没有具体任务），因此只应用 **rq 级**的 clamp 值
   （`uclamp_rq` 的 min/max），不应用任务级 clamp。

> **`uclamp` 在 WALT 里只钳制 util，不影响频率映射**。
> 频率映射只依赖钳制后的 util 和 `max`（阶段 8），
> 而 schedutil 的 uclamp 处理藏在 `effective_cpu_util()` 内部
> （core.c:7320 起，第 6 步）。这个位置差异是
> [02-cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md) §2.4 的主题。

---

## 8. 阶段 6：簇内取最大 + 六路 boost

### 8.1 跨 CPU 比较用交叉相乘

[waltgov_next_freq_shared cpufreq_walt.c:364](../../kernel/kernel/sched/walt/cpufreq_walt.c#L364)：

```c
for_each_cpu(j, policy->cpus) {
	struct waltgov_cpu *j_wg_cpu = &per_cpu(waltgov_cpu, j);
	unsigned long j_util, j_max, j_nl;

	j_util = j_wg_cpu->util;
	j_nl = j_wg_cpu->walt_load.nl;
	j_max = j_wg_cpu->max;
	if (boost) {
		j_util = mult_frac(j_util, boost + 100, 100);
		j_nl = mult_frac(j_nl, boost + 100, 100);
	}

	if (j_util * max >= j_max * util) {
		util = j_util;
		max = j_max;
		wg_policy->driving_cpu = j;
	}

	waltgov_walt_adjust(j_wg_cpu, j_util, j_nl, &util, &max);
}
```

**交叉相乘 `j_util * max >= j_max * util`** 比较的是
`j_util / j_max >= util / max`，即**归一化容量占比**，避免了除法。
`max` 初值为 1（不是 0），源码注释
[cpufreq_walt.c:376-382](../../kernel/kernel/sched/walt/cpufreq_walt.c#L376-L382)
解释了原因：若用 0，全 0 情况下 `max` 会停在 0，
之后 WALT 统计更新使 util 非零时，`fmax × 1.25 × util/0` 会算出无穷大，
导致频率无故跳到 `fmax`。

注意 `j_max` 是 `arch_scale_cpu_capacity(j)`，各 CPU 可能不同，
所以「取最大 util」实际是「取最大**占比**」。

### 8.2 boost 是逐 CPU 应用的，不是只对胜者

循环体最后一行对**每一个** CPU 都调用
[waltgov_walt_adjust cpufreq_walt.c:305](../../kernel/kernel/sched/walt/cpufreq_walt.c#L305)，
并且传的是**引用** `&util, &max`。因此某个 CPU 的 boost 值如果能超过
当前的全局 util，就会反超成为新的胜者，并把 `driving_cpu` 指到自己
[cpufreq_walt.c:295-303](../../kernel/kernel/sched/walt/cpufreq_walt.c#L295-L303)：

```c
static inline void max_and_reason(unsigned long *cur_util, unsigned long boost_util,
		struct waltgov_cpu *wg_cpu, unsigned int reason)
{
	if (boost_util && boost_util >= *cur_util) {
		*cur_util = boost_util;
		wg_cpu->reasons = reason;
		wg_cpu->wg_policy->driving_cpu = wg_cpu->cpu;
	}
}
```

**所以 `driving_cpu` 与「util 最大的 CPU」是两个不同的概念**——
后者是循环里 `j_util * max >= j_max * util` 选出的，
前者可能被 boost 改写。`reasons` 也同理：
最终生效的是 `driving_cpu` 的 `reasons`。

### 8.3 六个 boost 源

| 源 | 触发条件 | 注入值 | reason |
|---|---|---|---|
| ED（早期检测） | `walt_load.ed_active && sysctl_ed_boost_pct` | `cpu_util × (100+pct)/100` | `EARLY_DET` 0x4 |
| RTG | `walt_load.rtgb_active` | `wg_policy->rtg_boost_util` | `RTG_BOOST` 0x8 |
| HISPEED | `is_hiload && !is_migration` | `wg_policy->hispeed_util` | `HISPEED` 0x10 |
| NWD（新任务突发） | `is_hiload && nl >= cpu_util × 75/100` | `*max`（打满） | `NWD` 0x20 |
| PL（预测负载） | `wg_policy->tunables->pl` | `pl`（conservative 时 `×80/100`） | `PL` 0x2 |
| BTR（大任务轮转） | `walt_load.big_task_rotation` | `*max`（打满） | `BTR` 0x1 |

逐个说明：

- **NWD** [cpufreq_walt.c:332-333](../../kernel/kernel/sched/walt/cpufreq_walt.c#L332-L333)：
  `nl` 是**新任务**负载。判据 `nl >= mult_frac(cpu_util, NL_RATIO, 100)`，
  `NL_RATIO` 为 75 [cpufreq_walt.c:288](../../kernel/kernel/sched/walt/cpufreq_walt.c#L288)。
  含义：新任务占了当前 util 的四分之三以上 → 认为是突发，**直接打满**
  （注入 `*max` 而不是某个比例值）。**必须与 `is_hiload` 同时成立**，
  否则新增负载的小幅波动会频繁触发满频。
- **HISPEED** [cpufreq_walt.c:325-330](../../kernel/kernel/sched/walt/cpufreq_walt.c#L325-L330)：
  `is_hiload = cpu_util >= avg_cap × hispeed_load / 100`。
  `avg_cap` 是**本簇近期的平均容量**（见 [03-cpufreq.md](03-cpufreq.md) §3.2），
  用它而不是固定阈值，使得 boost 门限随频率自适应。
  `!is_migration` 的限制是因为迁移场景下 util 已经是跨簇的，
  再叠加 hispeed 会双重放大。
- **PL** [cpufreq_walt.c:335-339](../../kernel/kernel/sched/walt/cpufreq_walt.c#L335-L339)：
  `sysctl_sched_conservative_pl` 为真时把 `pl` 打八折（`×TARGET_LOAD/100`
  = `×80/100`），用一点准确性换省电。
- **ED** 有个**容易漏掉**的细节：它在
  [cpufreq_walt.c:341-342](../../kernel/kernel/sched/walt/cpufreq_walt.c#L341-L342)
  被**重复置位**一次：

  ```c
  if (employ_ed_boost)
  	wg_cpu->reasons |= CPUFREQ_REASON_EARLY_DET;
  ```

  这里用的是 `|=` 而非赋值。因为 `max_and_reason` 只在 boost 值**超过**
  当前 util 时才会置位 `reasons`；如果 ED 的 boost 值没超过，
  但 ED 条件确实成立，这一行保证 `EARLY_DET` 仍然会出现在 trace 里。
  **这是为了让观测数据完整，不是为了改变频率**。

### 8.4 `is_migration` 的来源

`is_migration = wg_cpu->flags & WALT_CPUFREQ_IC_MIGRATION`
[cpufreq_walt.c:310](../../kernel/kernel/sched/walt/cpufreq_walt.c#L310)，
即阶段 2 里 `wflag` 带上的标志，最终由
`waltgov_update_freq` 的 `wg_cpu->flags = flags`
[cpufreq_walt.c:415](../../kernel/kernel/sched/walt/cpufreq_walt.c#L415) 落地。
**注意它被读取的时机**：`waltgov_next_freq_shared` 只处理
**不带** `CONTINUE` 的那次回调（阶段 4.3），而 `CONTINUE` 的
`wg_cpu->flags` 是**该 CPU 自己**的。这意味着
`is_migration` 反映的是**发起最后那次回调的 CPU** 的状态，
不一定等于被 boost 的那个 `j_wg_cpu` 的状态。这是个细微的
`[待确认]` 点，见 [04-open-questions.md](../03-comparison/04-open-questions.md)。

---

## 9. 阶段 7：`walt_map_util_freq()` —— util 变频率

[walt_map_util_freq cpufreq_walt.c:209](../../kernel/kernel/sched/walt/cpufreq_walt.c#L209)

```c
#define TARGET_LOAD 80
static inline unsigned long walt_map_util_freq(unsigned long util,
					struct waltgov_policy *wg_policy,
					unsigned long cap, int cpu)
{
	unsigned long fmax = wg_policy->policy->cpuinfo.max_freq;
	unsigned int shift = wg_policy->tunables->target_load_shift;

	if (util >= wg_policy->tunables->target_load_thresh &&
	    cpu_util_rt(cpu_rq(cpu)) < (cap >> 2))
		return max(
			(fmax + (fmax >> shift)) * util,
			(fmax + (fmax >> 2)) * wg_policy->tunables->target_load_thresh
			)/cap;
	return (fmax + (fmax >> 2)) * util / cap;
}
```

### 9.1 普通分支：`×1.25`

```
freq = fmax × 1.25 × util / cap
```

`fmax + (fmax >> 2)` 就是 `fmax × 1.25`（`>>2` 是除以 4）。
这个 **1.25 是 WALT 的固定 headroom**，等价于 schedutil 里
`map_util_perf()` 的 1.25×（注意：schedutil 的 1.25 在
`map_util_perf()` 里，**不在** `map_util_freq()` 里，
而 WALT 把两者合并进了同一个函数）。

含义：让 CPU 跑在「比刚好够用高 25%」的频率上，留出调度余量。

### 9.2 高负载分支：`×1.0625`

条件 `util >= target_load_thresh`（默认 `DEFAULT_TARGET_LOAD_THRESH` = 1024，
[cpufreq_walt.c:293](../../kernel/kernel/sched/walt/cpufreq_walt.c#L293)）
且 **RT 负载低于容量的 1/4**。

高负载时 headroom 从 1.25 降到 `1 + 1/2^shift`；`shift` 默认 4，
即 **1.0625**（[cpufreq_walt.c:294](../../kernel/kernel/sched/walt/cpufreq_walt.c#L294)
`DEFAULT_TARGET_LOAD_SHIFT` = 4）。

**为什么要降低**：高负载下再乘 1.25 会立刻顶到 `fmax`，
使频率频繁在最高档位反复横跳（over-boost 抖动）。
降低 headroom 让频率能停在次高档稳定运行。

分支里 `max()` 的第二项 `fmax × 1.25 × target_load_thresh / cap`
是一个**下限保护**：即使 `util` 刚好等于阈值（此时第一项约等于
`fmax × 1.0625 × thresh / cap`，比第二项小），也保证输出不低于
「把阈值本身当作 1.25× 需求」算出的频率，避免在阈值附近出现频率**下跌**。

`cpu_util_rt(cpu_rq(cpu)) < (cap >> 2)` 这一项是给 RT 让路：
RT 压力大时说明 CPU 已被实时任务占用，此时再降 headroom 不合适，
于是回退到普通分支。

> **`[反直觉]` 默认参数下这个分支几乎不可达。**
> `util` 的上限是 `capacity_orig_of(cpu)`（阶段 4 的钳制，
> [walt.c:673](../../kernel/kernel/sched/walt/walt.c#L673)），
> 而默认阈值是 1024。所以要进这个分支，必须
> `capacity_orig == 1024` 且 `util == 1024`（完全打满）。
> 实际触发靠的是把 `target_load_thresh` 调低（这是 tunable）。
> 读源码时不要以为「高负载」是常态路径。

### 9.3 谁当 `cpu` 参数

`walt_map_util_freq` 的 `cpu` 参数**不是**当前 CPU，而是
`wg_driv_cpu->cpu`（[cpufreq_walt.c:243](../../kernel/kernel/sched/walt/cpufreq_walt.c#L243)
的 `per_cpu(waltgov_cpu, wg_policy->driving_cpu)`），
由 [cpufreq_walt.c:245](../../kernel/kernel/sched/walt/cpufreq_walt.c#L245) 传入：

```c
raw_freq = walt_map_util_freq(util, wg_policy, max, wg_driv_cpu->cpu);
```

它只用于 `cpu_util_rt()` 那一次检查——**RT 负载取自 driving CPU**。
这与 `util` / `max` 可能来自另一个 CPU，是又一处
「胜者不一致」的细节。

---

## 10. 阶段 8：整形、限速、写频率

### 10.1 adaptive 频率档

[cpufreq_walt.c:248-256](../../kernel/kernel/sched/walt/cpufreq_walt.c#L248-L256)：

```c
if (wg_policy->tunables->adaptive_high_freq) {
	if (raw_freq < get_adaptive_low_freq(wg_policy)) {
		freq = get_adaptive_low_freq(wg_policy);
		wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_LOW;
	} else if (raw_freq <= get_adaptive_high_freq(wg_policy)) {
		freq = get_adaptive_high_freq(wg_policy);
		wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_HIGH;
	}
}
```

把连续的 `raw_freq` **量化到两个档位**构成的台阶：

```
raw_freq <  low  →  输出 low
low ≤ raw ≤ high →  输出 high     ← 注意是「不超过 high 就抬到 high」
raw_freq >  high →  保持 raw_freq
```

中间那一条是**抬频**（`raw_freq ≤ high` 时输出 `high`，即输出 ≥ 输入），
不是降频。`get_adaptive_*_freq` 取 tunable 与 kernel 值的**较大者**
[cpufreq_walt.c:225-235](../../kernel/kernel/sched/walt/cpufreq_walt.c#L225-L235)。

**目的**：减少频率档位切换次数。低于 low 的一律用 low，
中间地带一律用 high，从而把大量微小的 util 波动吸收掉。
注意这里的 `reasons` 是**赋值**（`=`）而非 `|=`，会**覆盖**阶段 6
辛苦算出的 boost reason——所以 trace 里看到 `ADAPTIVE_*` 时，
不要以为没有 boost 发生。

### 10.2 频率缓存短路

[cpufreq_walt.c:262-264](../../kernel/kernel/sched/walt/cpufreq_walt.c#L262-L264)：

```c
if (wg_policy->cached_raw_freq && freq == wg_policy->cached_raw_freq &&
	!wg_policy->need_freq_update)
	return 0;
```

缓存的是 **`raw_freq`（未 resolve 的）**，比较的却是**整形后的 `freq`**
（`freq` 在 [cpufreq_walt.c:246](../../kernel/kernel/sched/walt/cpufreq_walt.c#L246)
被初始化为 `raw_freq`，随后可能被 adaptive 覆盖）。两者不同：
`cached_raw_freq` 在 `waltgov_update_next_freq` 里被赋为 `raw_freq`
[cpufreq_walt.c:136](../../kernel/kernel/sched/walt/cpufreq_walt.c#L136)，
即 **adaptive 之前**的值；而第 262 行比较的 `freq` 是**adaptive 之后**的值。
由于 `raw_freq` 和 `freq` 在 adaptive 修改过时会不同，
这个比较在 adaptive 生效的场景下可能**永远不相等**，
缓存短路失效。`[待确认]`——这可能是刻意（adaptive 说明状态已变），
也可能是笔误。

`return 0` 是「无需变更」的信号，由调用方
[cpufreq_walt.c:441-442](../../kernel/kernel/sched/walt/cpufreq_walt.c#L441-L442)
接住后 `goto out`，**不写频率**。

### 10.3 解析到真实 OPP

[cpufreq_walt.c:268](../../kernel/kernel/sched/walt/cpufreq_walt.c#L268)：

```c
final_freq = cpufreq_driver_resolve_freq(policy, freq);
```

`freq` 是算出来的连续值，`cpufreq_driver_resolve_freq` 把它吸附到
**驱动实际支持**的频率点（OPP 表 / 频率表），
并应用 `policy->min` / `policy->max` 的钳制。**这是第一次出现
硬件约束**——前面所有计算都在「理想连续频率」域里进行。

### 10.4 升降限速

[waltgov_update_next_freq cpufreq_walt.c:124](../../kernel/kernel/sched/walt/cpufreq_walt.c#L124)
是**唯一**决定「这个频率要不要真的生效」的地方：

```c
if (wg_policy->next_freq == next_freq)
	return false;

if (waltgov_up_down_rate_limit(wg_policy, time, next_freq)) {
	wg_policy->cached_raw_freq = 0;      /* ← 清缓存 */
	return false;
}

wg_policy->cached_raw_freq = raw_freq;
wg_policy->next_freq = next_freq;
wg_policy->last_freq_update_time = time;
return true;
```

三层关卡：

1. **同频跳过**：目标频率与当前 `next_freq` 相同 → 不重复写。
2. **方向限速** [cpufreq_walt.c:106](../../kernel/kernel/sched/walt/cpufreq_walt.c#L106)：
   升频看 `up_rate_delay_ns`，降频看 `down_rate_delay_ns`（两者独立）。
3. **清缓存**：限速拒绝时把 `cached_raw_freq` 清 0
   [cpufreq_walt.c:132](../../kernel/kernel/sched/walt/cpufreq_walt.c#L132)，
   而不是留着旧值——这样下次进来时第 262 行的缓存短路
   因 `cached_raw_freq == 0` 而不成立，保证被限速的请求
   **下一轮一定会被重新评估**。这是个容易看漏的细节：
   限速是「推迟」而不是「丢弃」。

另外 `waltgov_should_update_freq`
[cpufreq_walt.c:85](../../kernel/kernel/sched/walt/cpufreq_walt.c#L85)
在更外层还有一道 `min_rate_limit_ns` 门槛，且
`limits_changed`（`policy->min/max` 被改）会**强制**通过
[cpufreq_walt.c:89-93](../../kernel/kernel/sched/walt/cpufreq_walt.c#L89-L93)。

### 10.5 两条写频率路径

[cpufreq_walt.c:444-447](../../kernel/kernel/sched/walt/cpufreq_walt.c#L444-L447)：

```c
if (wg_policy->policy->fast_switch_enabled)
	waltgov_fast_switch(wg_policy, time, next_freq);
else
	waltgov_deferred_update(wg_policy, time, next_freq);
```

| | `waltgov_fast_switch` | `waltgov_deferred_update` |
|---|---|---|
| 位置 | [cpufreq_walt.c:193](../../kernel/kernel/sched/walt/cpufreq_walt.c#L193) | [cpufreq_walt.c:202](../../kernel/kernel/sched/walt/cpufreq_walt.c#L202) |
| 上下文 | 调用者上下文，**同步** | `irq_work` → `kthread_work`，**异步** |
| 写频率 | `cpufreq_driver_fast_switch(policy, next_freq)` 立即 | `waltgov_work` 里 `__cpufreq_driver_target(policy, wg_policy->next_freq, CPUFREQ_RELATION_L)` |
| 中间数据 | 直接用参数 `next_freq` | **重新读** `wg_policy->next_freq` |

异步路径经 [waltgov_irq_work cpufreq_walt.c:471](../../kernel/kernel/sched/walt/cpufreq_walt.c#L471)
排队到 kthread，最终在 [waltgov_work cpufreq_walt.c:454](../../kernel/kernel/sched/walt/cpufreq_walt.c#L454)
执行。注意它读的是**共享变量** `wg_policy->next_freq`
[cpufreq_walt.c:461](../../kernel/kernel/sched/walt/cpufreq_walt.c#L461) 而非闭包参数，
所以如果 kthread 被延迟期间又来了新的调频请求，写下去的是**最新**的频率——
这是有意的合并（coalescing），不是 bug。且用
`CPUFREQ_RELATION_L`（向下取整到不高于目标）配合
`__cpufreq_driver_target`。

两条路径都会先调 `waltgov_track_cycles` 累计周期数
（[cpufreq_walt.c:198](../../kernel/kernel/sched/walt/cpufreq_walt.c#L198)、
[462](../../kernel/kernel/sched/walt/cpufreq_walt.c#L462)），
为 `avg_cap` 提供输入——即**频率决策的副产品反过来喂养下一次决策的门限**
（阶段 8.3 的 HISPEED 用到 `avg_cap`）。这是个闭环。

---

## 11. 完整数值算例

设：中间簇，`policy->cpuinfo.max_freq = 2,400,000` kHz，
该簇 `max_possible_freq = 2,400,000` kHz，
4 个 CPU 全部在线，`arch_scale_cpu_capacity = capacity_orig = 1024`，
`sched_ravg_window = 16,000,000` ns，`sched_freq_aggr_en = false`，
无 uclamp 约束，`adaptive_high_freq = 1,800,000`，`avg_cap = 768`，
`hispeed_load = 90`，`target_load_thresh = 1024`，`boost = 0`，
PL 关闭，无 RTG / ED / BTR。

**阶段 0**：当前频率 1,800,000 kHz。

```
task_exec_scale = 1,800,000 × 1024 / 2,400,000 = 768
```

某任务在本窗口跑了 16,000,000 ns（满窗口）：

```
scaled_delta = 16,000,000 × 768 >> 10 = 12,000,000 ns
```

**阶段 1**：窗口滚动，`prev_runnable_sum = 9,000,000`（设实际用量 75%），
`grp_time.prev_runnable_sum = 0`（无 RTG）。

**阶段 2**：`cluster->aggr_grp_load = 0`，发起回调，
本 CPU 是 4 个中的第 4 个 → 不带 `CONTINUE`。

**阶段 3**：`sched_freq_aggr_en = 0`，走 else 分支：

```
load = 9,000,000 + 0 = 9,000,000 ns
```

（ksoftirqd 未运行、顶任务负载更小、无 SUH → 均不改写）

**阶段 4**：

```
util = 9,000,000 / 15625 = 576
```

576 < 1024（容量）→ 不钳制。非 asym sibling → 跳过兄弟调整。

**阶段 5**：无 uclamp 约束 → `util = 576`。

**阶段 6**：设本 CPU 的占比最大 → `util = 576, max = 1024, driving_cpu = 本 CPU`。

- `is_hiload`：`576 >= 768 × 90 / 100 = 691`？**否** → 无 HISPEED、无 NWD
- ED / RTG / PL / BTR 均不触发

→ `util = 576, max = 1024`

**阶段 7**：`util(576) >= target_load_thresh(1024)`？**否** → 普通分支：

```
raw_freq = (2,400,000 + 600,000) × 576 / 1024
         = 3,000,000 × 576 / 1024
         = 1,687,500 kHz
```

**阶段 8**：

```
adaptive: 1,687,500 <= 1,800,000  →  freq = 1,800,000，reason = ADAPTIVE_HIGH
cache:    设 cached_raw_freq 不同 → 不短路
resolve:  cpufreq_driver_resolve_freq(policy, 1,800,000) → 1,800,000
限速:     允许 → cached_raw_freq = 1,687,500, next_freq = 1,800,000
```

**最终频率 = 1,800,000 kHz**，比 util 对应的「刚好够用」（1,687,500）高
6.7%，差额来自 adaptive 档位抬升；若不启用 adaptive，
输出会是 1,687,500。

**回头验证归一化的自洽性**：若 CPU 真的跑在 1,800,000 kHz 且满负载，
阶段 0 会算出 util = 1024 × 1,800,000/2,400,000 = 768；
而本例负载是 75%（9,000,000 / 12,000,000），768 × 0.75 = 576 ✓。

---

## 12. 中间数据速查表

| 变量 | 类型/单位 | 写入点 | 读出点 |
|---|---|---|---|
| `task_exec_scale` | 无量纲，0-1024 | [walt.c:2215](../../kernel/kernel/sched/walt/walt.c#L2215) | `scale_exec_time` [walt.c:1570](../../kernel/kernel/sched/walt/walt.c#L1570) |
| `curr_runnable_sum` | ns（已归一化） | [walt.c:1750](../../kernel/kernel/sched/walt/walt.c#L1750) | `rollover_cpu_window` |
| `prev_runnable_sum` | ns（已归一化） | [walt.c:1620](../../kernel/kernel/sched/walt/walt.c#L1620) | `freq_policy_load` [walt.c:599](../../kernel/kernel/sched/walt/walt.c#L599) |
| `grp_time.prev_runnable_sum` | ns（已归一化） | [walt.c:1622](../../kernel/kernel/sched/walt/walt.c#L1622) | `__walt_irq_work_locked` [walt.c:4023](../../kernel/kernel/sched/walt/walt.c#L4023) |
| `cluster->aggr_grp_load` | ns（已归一化） | [walt.c:4033](../../kernel/kernel/sched/walt/walt.c#L4033) | `freq_policy_load` [walt.c:594](../../kernel/kernel/sched/walt/walt.c#L594) |
| `wrq->prev_top` | 索引 0..`NUM_LOAD_INDICES-1` | `rollover_top_tasks` [walt.c:1471](../../kernel/kernel/sched/walt/walt.c#L1471) | `top_task_load` [walt.c:560](../../kernel/kernel/sched/walt/walt.c#L560) |
| `reason` | 位掩码 `CPUFREQ_REASON_*` | `freq_policy_load` | trace、`wg_cpu->reasons` |
| `walt_scale_demand_divisor` | ns→util 除数，15625 | [walt.c:4336](../../kernel/kernel/sched/walt/walt.c#L4336) | `scale_time_to_util` [walt.h:992](../../kernel/kernel/sched/walt/walt.h#L992) |
| `wrq->util` | 0-1024 | [walt.c:651](../../kernel/kernel/sched/walt/walt.c#L651) | 别处读取 |
| `walt_load` (`nl`/`pl`/`ws`/`rtgb_active`/`ed_active`/`big_task_rotation`) | 混合 | [walt.c:662-670](../../kernel/kernel/sched/walt/walt.c#L662-L670) | `waltgov_walt_adjust` |
| `wg_cpu->util` | 0-1024（uclamp 后） | [cpufreq_walt.c:414](../../kernel/kernel/sched/walt/cpufreq_walt.c#L414) | `waltgov_next_freq_shared` [cpufreq_walt.c:383](../../kernel/kernel/sched/walt/cpufreq_walt.c#L383) |
| `wg_cpu->max` | 容量 0-1024 | [cpufreq_walt.c:282](../../kernel/kernel/sched/walt/cpufreq_walt.c#L282) | 交叉相乘 [cpufreq_walt.c:391](../../kernel/kernel/sched/walt/cpufreq_walt.c#L391) |
| `wg_cpu->reasons` | 位掩码 | [cpufreq_walt.c:283](../../kernel/kernel/sched/walt/cpufreq_walt.c#L283) 清零 | trace |
| `driving_cpu` | CPU 号 | `max_and_reason` [cpufreq_walt.c:301](../../kernel/kernel/sched/walt/cpufreq_walt.c#L301) | `get_next_freq` [cpufreq_walt.c:243](../../kernel/kernel/sched/walt/cpufreq_walt.c#L243) |
| `wg_policy->avg_cap` | 容量 0-1024 | `waltgov_calc_avg_cap` [cpufreq_walt.c:188](../../kernel/kernel/sched/walt/cpufreq_walt.c#L188) | `is_hiload` [cpufreq_walt.c:325](../../kernel/kernel/sched/walt/cpufreq_walt.c#L325) |
| `raw_freq` | kHz | `walt_map_util_freq` 返回 | 缓存比较 [cpufreq_walt.c:262](../../kernel/kernel/sched/walt/cpufreq_walt.c#L262) |
| `cached_raw_freq` | kHz | [cpufreq_walt.c:136](../../kernel/kernel/sched/walt/cpufreq_walt.c#L136) | 缓存短路 |
| `next_freq` | kHz | [cpufreq_walt.c:137](../../kernel/kernel/sched/walt/cpufreq_walt.c#L137) | `waltgov_work` [cpufreq_walt.c:461](../../kernel/kernel/sched/walt/cpufreq_walt.c#L461) |

---

## 13. 反直觉点汇总

1. **util 与当前频率无关**（阶段 0 归一化的结果），
   满负载 CPU 的 util = `1024 × f / f_max`。
2. **调频读 `prev_*` 不读 `curr_*`** —— 有一个窗口的固有延迟。
3. **四路负载源取最大而非求和** —— 因为它们是同一份时间的重叠视角。
4. **`CONTINUE` 不是「跳过这个 CPU」**，而是「先记账，先别汇总」：
   `wg_cpu->util` 在检查之前就已经算好了。
5. **`driving_cpu` ≠ util 最大的 CPU** —— boost 可以改写胜者。
6. **`walt_map_util_freq` 的 `cpu` 参数是 driving CPU**，
   而 `util`/`max` 可能来自另一个 CPU。
7. **默认参数下高负载分支（×1.0625）几乎不可达** —— 需 `util ≥ 1024`。
8. **adaptive 档位会覆盖 boost 的 `reasons`**，trace 里看不到真实 boost。
9. **限速是「推迟」不是「丢弃」** —— 被拒时清 `cached_raw_freq`，
   保证下轮重新评估。
10. **`is_migration` 可能与被 boost 的 CPU 不一致**（阶段 8.4）。

---

## 14. 相关文档

- [03-cpufreq.md](03-cpufreq.md) —— 本链条的**文件归属**视图
- [01-window-model.md](01-window-model.md) —— 阶段 0/1 的窗口模型细节
- [02-demand-prediction.md](02-demand-prediction.md) —— 阶段 3 中 `pl` 的来源（16 桶直方图）
- [08-boost.md](08-boost.md) —— 阶段 6 六路 boost 的完整背景
- [02-cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md) —— 与 schedutil 的逐函数对照
- [04-data-flow.md](../00-overview/04-data-flow.md) —— 事件驱动视角
- [04-open-questions.md](../03-comparison/04-open-questions.md) —— 本文标记的 `[待确认]` 项
