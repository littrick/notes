# RTG、共置与簇偏好

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

前两篇（[window-model.md](01-window-model.md)、[demand-prediction.md](02-demand-prediction.md)）
讲的是**单任务**的记账。但手机上的延迟问题从来不是单个线程的问题：
一个 app 的主线程、`RenderThread`、`binder` 线程池、`hwuiTask` 是**一批**
互相唤醒、互相等待的线程。把它们拆开各自优化，会出现「主线程上了大核、
渲染线程还在小核排队」这种看起来每个任务都放了、实际上整组还是慢的局面。

WALT 为此引入 **RTG（Related Thread Group，相关线程组）**：把一批任务
绑定成一个逻辑单元，然后做两件事——

1. **单独记账**：这一组获得了多少算力，记在 `grp_time` 里，而不是混进
   CPU 的总忙时。因为「CPU 很忙」和「我的组很饿」是两个不同的问题。
2. **整组放置**：决定这一组的重心放在哪个簇（`grp->skip_min`，即本文说的
   「偏好簇」），并据此把成员任务往同一个方向推。

围绕这套机制还有两个派生的放置通道：**pipeline**（用户显式指定的低延迟
任务）和 **heavy**（系统自动挑出的最重任务），它们给任务分配一个
`pipeline_cpu`，在放置时走快速路径。

字段定义见 [03-data-structures.md](../00-overview/03-data-structures.md)，
hook 注册见 [02-integration-model.md](../00-overview/02-integration-model.md)。

---

## 1. 名词对照：文档名 vs 源码名

**先看这一节，否则后面的名字会对不上。** 几个常见的想当然的名字在源码里
**并不存在**，读代码时不要去找：

| 你可能期待的名字 | 源码中的实际名字 / 位置 |
|---|---|
| `wts->preferred_cluster` | **不存在**。偏好簇是组属性 `grp->skip_min` [walt.h:891](../../kernel/kernel/sched/walt/walt.h#L891) |
| `walt_set_preferred_cluster()` | **不存在**。是 `set_preferred_cluster()` [walt.c:2980](../../kernel/kernel/sched/walt/walt.c#L2980) 与 `_set_preferred_cluster()` [walt.c:2917](../../kernel/kernel/sched/walt/walt.c#L2917) |
| `wts->is_heavy` / `is_heavy_task()` | **不存在**。用 `heavy_wts[]` 数组 [walt.c:3589](../../kernel/kernel/sched/walt/walt.c#L3589) + `wts->low_latency` 的 `WALT_LOW_LATENCY_HEAVY` 位 |
| `wts->mvp_task` | **不存在**。是 `wts->mvp_list` / `wts->mvp_prio`，队列头在 `wrq->mvp_tasks` [walt.h:132](../../kernel/kernel/sched/walt/walt.h#L132) |
| `set_rtg()` / `walt_rtg_*` 系统调用 | **不存在**。入口是 `sched_set_group_id()` [walt.c:3199](../../kernel/kernel/sched/walt/walt.c#L3199)，由 procfs 的 `sched_group_id` 节点驱动 [sysctl.c:312](../../kernel/kernel/sched/walt/sysctl.c#L312) |

> **[反直觉]** 任务级没有任何「偏好簇」字段。偏好簇是**整组一个**的开关，
> 表达形式只是「要不要跳过最小簇」这一个布尔。这是 RTG 设计的核心简化：
> 不给每个任务记簇偏好，只给组记一个方向。

---

## 2. RTG 的数据结构

### 2.1 `struct walt_related_thread_group`

定义在 [include/linux/sched/walt.h:46](../../kernel/include/linux/sched/walt.h#L46)。
字段含义见 [data-structures.md §7](../00-overview/03-data-structures.md#7-struct-walt_related_thread_grouprtg)，
这里只强调三个与本文逻辑直接相关的：

- `skip_min`（[:51](../../kernel/include/linux/sched/walt.h#L51)）——唯一的方向开关
- `last_update` / `downmigrate_ts` / `start_ktime_ts`——三个迟滞用的时间戳，
  见 §4.3
- `tasks`——成员链表，成员通过 `wts->grp_list` 挂上来

组的容器是全局数组 `related_thread_groups[MAX_NUM_CGROUP_COLOC_ID]`
[walt.c:2871](../../kernel/kernel/sched/walt/walt.c#L2871)，
`MAX_NUM_CGROUP_COLOC_ID = 20` [walt.c:47](../../kernel/kernel/sched/walt/walt.c#L47)。
组在启动时**一次性预分配**（`alloc_related_thread_groups()`
[walt.c:3021](../../kernel/kernel/sched/walt/walt.c#L3021)），
运行期只做引用，不分配/释放。id 0 是「不在任何组」的哨兵，
id 1 是 `DEFAULT_CGROUP_COLOC_ID`（保留给 cgroup 共置路径）。

### 2.2 `struct walt_task_group`：cgroup 侧的开关

定义在 [walt.h:357](../../kernel/kernel/sched/walt/walt.h#L357)，
内联在 `task_group->android_vendor_data1`：

```c
struct walt_task_group {
	bool colocate;                                    /* walt.h:362 */
	bool sched_boost_enable[MAX_NUM_BOOST_TYPE];
};
```

`colocate` 是**唯一**决定「这个 cgroup 的线程要不要进 RTG」的开关。
初始化时按 cgroup 名字写死 [boost.c:35](../../kernel/kernel/sched/walt/boost.c#L35)：

| cgroup | `colocate` | 函数 |
|---|---|---|
| `top-app` | **true** | `walt_init_topapp_tg()` [boost.c:35](../../kernel/kernel/sched/walt/boost.c#L35) |
| `foreground` | false | `walt_init_foreground_tg()` [boost.c:48](../../kernel/kernel/sched/walt/boost.c#L48) |
| 其它 | false | `walt_init_tg()` [boost.c:22](../../kernel/kernel/sched/walt/boost.c#L22) |

> **[反直觉]** 手机上绝大多数共置都来自**这一行写死的 `top-app`**。
> 换句话说「共置」在实践中约等于「top-app 的线程」。`find_heaviest_topapp()`
> 这个名字里的 `topapp` 也是这个来源。

---

## 3. 任务如何加入 RTG

有两条入口，最终都落到 `__sched_set_group_id()` [walt.c:3165](../../kernel/kernel/sched/walt/walt.c#L3165)。

### 3.1 路径 A：cgroup attach（主要路径）

```
cgroup 挂载/切换
  → android_rvh_cpu_cgroup_attach()   walt.c:3287
       grp_id = wtg->colocate ? DEFAULT_CGROUP_COLOC_ID : 0;
       __sched_set_group_id(task, grp_id)
```

`android_rvh_cpu_cgroup_attach()` [walt.c:3287](../../kernel/kernel/sched/walt/walt.c#L3287)
是唯一的 cgroup 驱动点。注意它判的是 `colocate` 而不是 cgroup 名——
名字只在 `walt_init_topapp_tg()` 里用一次。

新 fork 的任务走另一条捷径 `add_new_task_to_grp()`
[walt.c:3128](../../kernel/kernel/sched/walt/walt.c#L3128)：
因为父线程已在组里，子线程直接继承，**不经过** `__sched_set_group_id()`
（省掉 `task_rq_lock`）。注释说明了这个偏好：
「The children inherits the group id from the parent」
[walt.c:2866](../../kernel/kernel/sched/walt/walt.c#L2866)。

### 3.2 路径 B：procfs 显式设置

`/proc/<pid>/sched_group_id` 写一个 1..19 的 id，走 `sched_set_group_id()`
[walt.c:3199](../../kernel/kernel/sched/walt/walt.c#L3199)。读回用
`sched_get_group_id()` [walt.c:3208](../../kernel/kernel/sched/walt/walt.c#L3208)。

`__sched_set_group_id()` 有一条重要的语义约束：

```c
/* Switching from one group to another directly is not permitted */   /* walt.c:3178 */
if ((!wts->grp && !group_id) || (wts->grp && group_id))
	goto done;
```

即：**只能「无组 → 有组」或「有组 → 无组」**，不能从组 A 直接切到组 B。
必须先写 0 退出，再写新 id。`sched_set_group_id()` 还额外拒绝
`DEFAULT_CGROUP_COLOC_ID`（id 1 只给 cgroup 路径用）[walt.c:3201](../../kernel/kernel/sched/walt/walt.c#L3201)。

### 3.3 加入/离开时的忙时搬运

`add_task_to_group()` [walt.c:3075](../../kernel/kernel/sched/walt/walt.c#L3075)
和 `remove_task_from_group()` [walt.c:3042](../../kernel/kernel/sched/walt/walt.c#L3042)
在 `rq` 锁下调用 `transfer_busy_time()` [walt.c:3344](../../kernel/kernel/sched/walt/walt.c#L3344)，
把任务已经产生的忙时在「rq 主计数器」和「`grp_time`」之间搬过去。为什么必须搬，
见下一节。

任务退出时 `walt_task_dead()` [walt.c:2419](../../kernel/kernel/sched/walt/walt.c#L2419)
统一清理：退组、摘 pipeline、摘 heavy。

---

## 4. `grp_time`：RTG 忙时为什么走另一套计数器

### 4.1 关键的那次指针切换

`update_cpu_busy_time()` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678)
开头先把四个累加目标指向 **rq 的主计数器**：

```c
u64 *curr_runnable_sum = &wrq->curr_runnable_sum;          /* walt.c:1689 */
```

然后在滚动窗口之前，如果任务属于某个组，**把指针整体改指到 `grp_time`**：

```c
grp = wts->grp;                                            /* walt.c:1719 */
if (grp) {
	struct group_cpu_time *cpu_time = &wrq->grp_time;   /* walt.c:1721 */
	curr_runnable_sum = &cpu_time->curr_runnable_sum;
	prev_runnable_sum = &cpu_time->prev_runnable_sum;
	nt_curr_runnable_sum = &cpu_time->nt_curr_runnable_sum;
	nt_prev_runnable_sum = &cpu_time->nt_prev_runnable_sum;
}
```

这是整个 RTG 机制的**记账基石**。函数体后续十几处 `*curr_runnable_sum += delta`
完全不知道自己写到了哪套计数器里——`[反直觉]` 如果只读函数体后半段，
你根本看不出记账目标被换过。**读 `update_cpu_busy_time()` 一定要连着第 1719 行往下读。**

`struct group_cpu_time` [walt.h:85](../../kernel/kernel/sched/walt/walt.h#L85)
与 rq 主计数器**同构**（`curr/prev` × `nt_/非 nt_` 四个 `u64`），
所以指针替换在类型上是无缝的。账目细节见
[window-model.md §8](01-window-model.md)。

### 4.2 为什么要分两套账

两套计数器回答两个不同的问题：

| 计数器 | 回答的问题 | 消费者 |
|---|---|---|
| `wrq->curr/prev_runnable_sum` | 这个 **CPU** 有多忙 | 负载均衡、`walt_update_task_ravg` 的 CPU 侧统计 |
| `wrq->grp_time.curr/prev_runnable_sum` | 这个 **CPU 上某个 RTG** 拿到了多少算力 | 调频、共置判定 |

如果混在一起，会出现两种错误：

- **调频上**：一个 RTG 的线程被拆到 4 个 CPU 上，每个 CPU 都觉得自己只忙了 25%，
  于是都不提频——组整体反而变慢。分开记账后，`aggr_grp_load` 能把**整个簇**上
  该组的负载加起来，再一次性给频率。
- **共置上**：`skip_min` 的判定需要「这一组总共要多少算力」，
  这在 rq 级计数器里根本取不到。

聚合发生在 `__walt_irq_work_locked()`：

```c
/* update aggr_grp_load for all clusters, all cpus */   /* walt.c:4021 */
aggr_grp_load += wrq->grp_time.prev_runnable_sum;
...
cluster->aggr_grp_load = aggr_grp_load;                 /* walt.c:4033 */
```

消费方是 `freq_policy_load()` [walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590)：

```c
else
	load = wrq->prev_runnable_sum +
				wrq->grp_time.prev_runnable_sum;   /* walt.c:604 */
```

以及 `cpufreq_walt` 的 RTG boost：`is_rtgb_boost` 时把频率拉到底线
`waltgov_walt_adjust()` [cpufreq_walt.c:323](../../kernel/kernel/sched/walt/cpufreq_walt.c#L323)。
`rtgb_active` 就是 `grp->skip_min`（`is_rtgb_active()`
[walt.c:3522](../../kernel/kernel/sched/walt/walt.c#L3522)），
经 `walt_load->rtgb_active` [walt.c:665](../../kernel/kernel/sched/walt/walt.c#L665) 传到 governor。

### 4.3 进/出组的搬运语义 `[反直觉]`

`transfer_busy_time()` [walt.c:3344](../../kernel/kernel/sched/walt/walt.c#L3344)
用 `ADD_TASK`/`REM_TASK` 决定搬运方向（`RQ_TO_GROUP` / `GROUP_TO_RQ`）。
出组时它是**把整组的历史压到一个 CPU 上**：

```c
for_each_possible_cpu(i) {
	wts->curr_window_cpu[i] = 0;                       /* walt.c:3498 */
	wts->prev_window_cpu[i] = 0;
}
...
wts->curr_window_cpu[cpu] = wts->curr_window;              /* walt.c:3516 */
wts->prev_window_cpu[cpu] = wts->prev_window;
```

源码自己的注释解释了取舍：出组时这样「sub-optimal」，但省掉了组内跨簇迁移
的修正开销。也就是说——**一个刚退组的任务，它的历史负载会全部记在当前 CPU 上**，
这个 CPU 的 `prev_runnable_sum` 会瞬时抬高一截。调频曲线的毛刺可能来自这里。

### 4.4 迁移路径上的同一套切换

任务在组内跨 CPU 迁移时，`migrate_busy_time_subtraction()`
[walt.c:1021](../../kernel/kernel/sched/walt/walt.c#L1021) 与
`migrate_busy_time_addition()` [walt.c:1111](../../kernel/kernel/sched/walt/walt.c#L1111)
同样在 `wts->grp` 非空时改指 `src_wrq->grp_time` / `dest_wrq->grp_time`
[walt.c:1077](../../kernel/kernel/sched/walt/walt.c#L1077)。
所以「`grp_time` 是 RTG 专用」这条规则在**所有**记账路径上都成立，
不是 `update_cpu_busy_time()` 的特例。

---

## 5. colocation：共置

### 5.1 `coloc_demand` 是 5 窗口平均，不是策略 demand `[反直觉]`

`update_history()` [walt.c:1984](../../kernel/kernel/sched/walt/walt.c#L1984)
先按 `sysctl_sched_window_stats_policy` 算出 `demand`（可能取 max、avg 或
`max(avg, runtime)`），然后**无条件**把 `coloc_demand` 设成 5 窗口平均：

```c
wts->coloc_demand = div64_u64(sum, RAVG_HIST_SIZE);   /* walt.c:2051 */
```

其中 `sum` 是 `sum_history[0..4]` 的累加。所以：

- `demand` / `demand_scaled` = 策略选择的结果，偏**峰值**（默认
  `WINDOW_STATS_MAX_RECENT_AVG`）
- `coloc_demand` = 恒定的**平滑平均**，不受策略影响

**为什么故意不一致**：共置决策是「这一组整体会不会把大核吃满」的判断，
用峰值会让 `skip_min` 频繁抖动；而 placement / 调频需要保守（宁可高估）。
两类信号的时间常数本来就该不同。详见
[window-model.md §9.4](01-window-model.md)。

初始化时 `coloc_demand` 也被赋成初始负载
[walt.c:2391](../../kernel/kernel/sched/walt/walt.c#L2391)，
所以新线程的第一窗口就有非零的共置贡献。

### 5.2 `unfilter`：把小任务从共置统计里剔出去

同一个函数里紧跟的一段：

```c
if (demand_scaled > sysctl_sched_min_task_util_for_colocation)   /* walt.c:2053 */
	wts->unfilter = sysctl_sched_task_unfilter_period;
else if (wts->unfilter)
	wts->unfilter = max_t(int, 0, wts->unfilter - wrq->prev_window_size);
```

- 阈值 `sysctl_sched_min_task_util_for_colocation` 默认 **35**
  [sysctl.c:69](../../kernel/kernel/sched/walt/sysctl.c#L69)（满刻度 1024，
  即约 3.4% 的单窗口忙时）
- 満足阈值 → `unfilter` 被**充能**到 `sysctl_sched_task_unfilter_period`
  （默认 100ms [sysctl.c:1120](../../kernel/kernel/sched/walt/sysctl.c#L1120)）
- 不满足 → 每个窗口**减去一个窗口的长度**，线性衰减到 0

`unfilter` 的语义是「这个任务最近够重，别把它当过客」。
它是一个**时间戳式的定时器**，不是一个布尔——所以「刚变轻」的任务
在 100ms 内仍然算数，避免忙闲交替的长任务在共置统计里被反复剔除。

### 5.3 谁消费 `unfilter`

`unfilter` 只在两处生效，两处都把「组」和「这个任务够重」做与运算：

```c
/* walt_cfs.c:144 */
return (sched_boost_type != CONSERVATIVE_BOOST) &&
	walt_get_rtg_status(p) && (wts->unfilter ||
	walt_pipeline_low_latency_task(p));
```

`walt_task_skip_min_cpu()` [walt_cfs.c:144](../../kernel/kernel/sched/walt/walt_cfs.c#L144)
——注意即使任务在 `skip_min` 的组里，如果它自己 `unfilter == 0` 且不是
pipeline 任务，它**仍然会被允许放到小核**。共置是组的目标，落不落到具体任务
上还要看任务自身的分量。

另一处是 `scale_exec_time()`，见 §8。

### 5.4 `walt_should_kick_upmigrate()`：把任务从小核踢走

```c
if (is_suh_max() && rtg && rtg->id == DEFAULT_CGROUP_COLOC_ID &&
			rtg->skip_min && wts->unfilter)
	return is_min_cluster_cpu(cpu);                     /* walt.h:751 */
```

`walt_should_kick_upmigrate()` [walt.h:747](../../kernel/kernel/sched/walt/walt.h#L747)
的四个条件**全部**要满足：用户提示拉满（`sysctl_sched_user_hint == 1000`）、
属于**默认共置组**（id 1）、组 `skip_min`、任务 `unfilter`。
然后只在**当前在小核**时返回 true。

消费点：`task_fits_max()` [walt.h:810](../../kernel/kernel/sched/walt/walt.h#L810)
的 `is_min_cluster_cpu(cpu)` 分支：

```c
if (task_boost_policy(p) == SCHED_BOOST_ON_BIG ||
		task_boost > 0 ||
		walt_uclamp_boosted(p) ||
		walt_should_kick_upmigrate(p, cpu))
	return false;                                       /* walt.h:822 */
```

返回 false 意味着「这个 max 容量的判断不成立」→ 负载均衡会把任务往更高容量
的 CPU 推。

> **[反直觉]** 这个函数名里的 `suh`（`is_suh_max()`
> [walt.h:741](../../kernel/kernel/sched/walt/walt.h#L741)）意味着**它默认不生效**——
> `sysctl_sched_user_hint` 默认不是 1000。也就是说 `skip_min` 的**主动踢核**
> 是用户态（perf HAL / powerhint）拉高提示后才打开的增强路径；
> 常态下 `skip_min` 只通过阈值式放置（§5.5）起作用。

### 5.5 `skip_min` 如何影响常规放置

`skip_min` 有两条常规通路：

1. **起始簇抬高**。`walt_get_indicies()` 里
   [walt_cfs.c:250](../../kernel/kernel/sched/walt/walt_cfs.c#L250)：

   ```c
   if (is_uclamp_boosted || per_task_boost ||
	task_boost_policy(p) == SCHED_BOOST_ON_BIG ||
	walt_task_skip_min_cpu(p)) {
	*energy_eval_needed = false;
	*order_index = 1;
   ```
   即 `skip_min` 的任务把搜索起点从簇 0 抬到簇 1——**不从小核开始找**。

2. **RTG 专属的容量余量**。`task_fits_capacity()`
   [walt.h:778](../../kernel/kernel/sched/walt/walt.h#L778) 里，RTG 成员
   在跨越到小核/中间簇时会用更保守的 margin
   （`sysctl_sched_early_down[0]` / `[1]`、`sysctl_sched_early_up[...]`）：

   ```c
   if (task_in_related_thread_group(p)) {
	if (is_min_cluster_cpu(cpu))
		margin = max(margin, sysctl_sched_early_down[0]);
	else if (!is_max_cluster_cpu(cpu))
		margin = max(margin, sysctl_sched_early_down[1]);
   }
   ```
   效果是 RTG 成员**更难被判为「放得下」**，从而更倾向留在/移到高容量簇。

此外 `walt_get_rtg_status(p)` 还出现在 `walt_get_indicies()` 的 region2
抑制条件里 [walt_cfs.c:301](../../kernel/kernel/sched/walt/walt_cfs.c#L301)
——`skip_min` 的任务不做省电的 region2 探索。

### 5.6 上报给 core_ctl

共置组的总需求会喂给 `core_ctl`，用于决定开几个大核：

```c
total_demand += wts->coloc_demand;                    /* walt.c:4298 */
...
data->coloc_load_pct = div64_u64(total_demand * 1024 * 100,
		       (u64)sched_ravg_window * scale);  /* walt.c:4314 */
```

`walt_fill_ta_data()` [walt.c:4273](../../kernel/kernel/sched/walt/walt.c#L4273)
把总需求**归一化到最小容量核**再转成百分比。统计时跳过 5 个窗口没活动的成员
（`mark_start < wallclock - sched_ravg_window * RAVG_HIST_SIZE`）。

---

## 6. 偏好簇：`skip_min` 的决策与迟滞

### 6.1 决策入口

`_set_preferred_cluster()` [walt.c:2917](../../kernel/kernel/sched/walt/walt.c#L2917)
是唯一的决策函数。它做三件事：

1. **快速否决**：组空 → `skip_min = false`；非 HMP（小核+大核这种非对称
   拓扑不存在）→ `skip_min = false`。
2. **节流**：`wallclock - grp->last_update < sched_ravg_window / 10`（2ms）
   直接返回 [walt.c:2944](../../kernel/kernel/sched/walt/walt.c#L2944)。
   注释点明了原因——多个相关任务同时唤醒会引发**并发调用**，没必要重复算。
3. **累加组需求**，然后交给 `update_best_cluster()`。

累加循环 [walt.c:2947](../../kernel/kernel/sched/walt/walt.c#L2947)：

```c
list_for_each_entry(wts, &grp->tasks, grp_list) {
	p = wts_to_ts(wts);
	if (task_boost_policy(p) == SCHED_BOOST_ON_BIG) {
		group_boost = true;
		break;
	}
	if (wts->mark_start < wallclock -
	    (sched_ravg_window * RAVG_HIST_SIZE))
		continue;
	combined_demand += wts->coloc_demand;              /* walt.c:2958 */
	...
}
```

注意两点：用的是 `coloc_demand`（平滑值，§5.1）；超过阈值就提前 break，
不必算完。

### 6.2 `update_best_cluster()`：双向阈值 + 双重迟滞

`update_best_cluster()` [walt.c:2876](../../kernel/kernel/sched/walt/walt.c#L2876)：

```c
if (boost) { grp->skip_min = false; return; }   /* 组在 boost，留在小核就好 */

if (is_suh_max())
	combined_demand = sched_group_upmigrate;

if (!grp->skip_min) {
	if (combined_demand >= sched_group_upmigrate)
		grp->skip_min = true;               /* 进入：即时，无迟滞 */
	return;
}
if (combined_demand < sched_group_downmigrate) {
	/* 退出：最多三重门 */
	...
}
```

阈值是两个**绝对值**（纳秒级等效运行时间），不是百分比：

```c
static unsigned int __read_mostly sched_group_upmigrate = 20000000;    /* walt.c:2451 */
static unsigned int __read_mostly sched_group_downmigrate = 19000000;  /* walt.c:2458 */
```

运行时由 `walt_update_group_thresholds()`
[walt.c:2460](../../kernel/kernel/sched/walt/walt.c#L2460) 重算：以最小簇的
容量把一个窗口长度折算成等效运行时间，再乘百分比
（`sysctl_sched_group_upmigrate_pct` 默认 100、
`downmigrate_pct` 默认 95，[sysctl.c:1114](../../kernel/kernel/sched/walt/sysctl.c#L1114) /
[:1116](../../kernel/kernel/sched/walt/sysctl.c#L1116)）。

**不对称的迟滞** `[反直觉]`：

| 方向 | 条件 |
|---|---|
| 进 `skip_min`（上迁） | `combined_demand >= upmigrate` —— **一步到位，立即生效** |
| 出 `skip_min`（下迁） | `combined_demand < downmigrate` **且** 组已存在 ≥ `sysctl_sched_hyst_min_coloc_ns`（默认 80ms [sysctl.c:77](../../kernel/kernel/sched/walt/sysctl.c#L77)）**且** 需求持续偏低超过 `sysctl_sched_coloc_downmigrate_ns` 才真正退出 |

即：**「上大核」是立刻的，「下小核」要经过两道时间门**。合理——上迁慢了会掉帧，
下迁快了会反复抖动。第一道时间门（`start_ktime_ts`）的意义是防止一个刚建立的
组在还没热身时就被降级；`downmigrate_ts` 则记录需求首次跌破阈值的时间，
只有持续超过 `sysctl_sched_coloc_downmigrate_ns` 才降。

`is_suh_max()` 时直接把 `combined_demand` 顶到上迁阈值——**用户提示拉满时
无条件 `skip_min`**。

### 6.3 什么时候重新决策

`update_preferred_cluster()` [walt.c:2987](../../kernel/kernel/sched/walt/walt.c#L2987)
是一个节流判定，两个触发条件任一成立就返回 1：

- `abs(new_load - old_load) > sched_ravg_window / 4`（负载变化超过 1/4 窗口）
- `walt_sched_clock() - grp->last_update > sched_ravg_window`（距上次超过一个窗口）

调用点只有两个：

| 调用点 | 位置 | 说明 |
|---|---|---|
| 唤醒路径 | [walt.c:4782](../../kernel/kernel/sched/walt/walt.c#L4782) | `android_rvh_try_to_wake_up` |
| tick 路径 | [walt.c:4826](../../kernel/kernel/sched/walt/walt.c#L4826) | `android_vh_scheduler_tick` |

两处都是同一个模式：

```c
grp = task_related_thread_group(p);
if (update_preferred_cluster(grp, p, old_load, false))
	set_preferred_cluster(grp);
```

`set_preferred_cluster()` [walt.c:2980](../../kernel/kernel/sched/walt/walt.c#L2980)
只是「拿 `grp->lock` + 调 `_set_preferred_cluster()`」的包装。
唤醒路径传 `from_tick = false`，tick 路径传 `true`
（`true` 且 `is_suh_max()` 时直接返回 1，即强制重算）。

`add_task_to_group()` [walt.c:3093](../../kernel/kernel/sched/walt/walt.c#L3093)
和 `remove_task_from_group()` [walt.c:3054](../../kernel/kernel/sched/walt/walt.c#L3054)
也各调一次——成员变化会立刻重算方向。

---

## 7. pipeline 与 heavy

这两者共享同一个 `pipeline_cpu` 字段 [include/linux/sched/walt.h:143](../../kernel/include/linux/sched/walt.h#L143)
和同一条放置快速路径，但**驱动来源不同、且互斥**。

### 7.1 `low_latency` 位图：低延迟的三个来源

[walt.h:55-60](../../kernel/kernel/sched/walt/walt.h#L55-L60)：

```c
#define WALT_LOW_LATENCY_PROCFS		BIT(0)
#define WALT_LOW_LATENCY_BINDER		BIT(1)
#define WALT_LOW_LATENCY_PIPELINE	BIT(2)
#define WALT_LOW_LATENCY_HEAVY		BIT(3)

#define WALT_LOW_LATENCY_MASK		(WALT_LOW_LATENCY_PIPELINE|WALT_LOW_LATENCY_HEAVY)
```

`walt_low_latency_task()` [walt.h:451](../../kernel/kernel/sched/walt/walt.h#L451)
把 PROCFS/BINDER 两个来源**再乘一个 util 门槛**
（`sysctl_walt_low_latency_task_threshold`，默认 0 = 关闭）；
而 `walt_pipeline_low_latency_task()`
[walt.h:481](../../kernel/kernel/sched/walt/walt.h#L481) 对 PIPELINE|HEAVY 位
**不看 util，无条件返回**。

> **[反直觉]** 「低延迟任务」有两档语义：binder/procfs 设的位是**带条件的**
> （任务还得够轻），pipeline/heavy 设的位是**无条件的**。写代码判断时用错
> 函数（`walt_low_latency_task` vs `walt_pipeline_low_latency_task`）
> 会导致行为差异，而这在阅读时非常容易混。

### 7.2 pipeline：用户显式指定

设置入口是 procfs 的 `sched_pipeline` 节点
[sysctl.c:338](../../kernel/kernel/sched/walt/sysctl.c#L338)：

```c
case PIPELINE:
	rq = task_rq_lock(task, &rf);
	...
	if (val) {
		ret = add_pipeline(wts);
		...
		wts->low_latency |= WALT_LOW_LATENCY_PIPELINE;
	} else {
		wts->low_latency &= ~WALT_LOW_LATENCY_PIPELINE;
		remove_pipeline(wts);
	}
```

`add_pipeline()` [walt.c:3591](../../kernel/kernel/sched/walt/walt.c#L3591)
把 `wts` 塞进 `pipeline_wts[]` [walt.c:3585](../../kernel/kernel/sched/walt/walt.c#L3585)
的空位，容量是 `nr_big_cpus`（**非最小簇**的 CPU 总数
[walt.c:2794](../../kernel/kernel/sched/walt/walt.c#L2794)）；满了返回 `-ENOSPC`。
`remove_pipeline()` [walt.c:3621](../../kernel/kernel/sched/walt/walt.c#L3621)
只清位、不析构。

放置侧的快速路径在 `walt_find_energy_efficient_cpu()`
[walt_cfs.c:933](../../kernel/kernel/sched/walt/walt_cfs.c#L933)
的入口处（[:977](../../kernel/kernel/sched/walt/walt_cfs.c#L977)）：

```c
if ((wts->low_latency & WALT_LOW_LATENCY_MASK) &&
		(pipeline_cpu != -1) &&
		walt_task_skip_min_cpu(p) &&
		cpumask_test_cpu(pipeline_cpu, p->cpus_ptr) &&
		cpu_active(pipeline_cpu) && !cpu_halted(pipeline_cpu) &&
		!ignore_cluster[cpu_cluster(pipeline_cpu)->id]) {
	if (!walt_pipeline_low_latency_task(cpu_rq(pipeline_cpu)->curr)) {
		best_energy_cpu = pipeline_cpu;
		fbt_env.fastpath = PIPELINE_FASTPATH;
		goto out;
	}
}
```

**8 个条件全部满足才走快速路径**。注意其中包含
`walt_task_skip_min_cpu(p)`——pipeline 任务的快速路径**仍然要求它所在的组
`skip_min` 且有 `unfilter`**。也就是说 pipeline 单独设是不够的，
组的共置状态也得到位。这解释了为什么单独设 `pipeline=1` 有时看不出效果。

### 7.3 heavy：系统自动挑

没有 `is_heavy_task()`。heavy 是一张全局表
`heavy_wts[WALT_NR_CPUS]` [walt.c:3589](../../kernel/kernel/sched/walt/walt.c#L3589)
加一个 `low_latency` 标志位。

**挑选**：`find_heaviest_topapp()` [walt.c:3669](../../kernel/kernel/sched/walt/walt.c#L3669)

```c
grp = lookup_related_thread_group(DEFAULT_CGROUP_COLOC_ID);
if (!grp || !grp->skip_min || !sched_heavy_nr) {       /* walt.c:3688 */
	/* 清空 heavy_wts[]，关闭 core_ctl boost */
	return;
}
```

前提是**默认共置组处于 `skip_min`** 且 `sysctl_sched_heavy_nr`（默认 0 =
关闭 [sysctl.c:85](../../kernel/kernel/sched/walt/sysctl.c#L85)）非零。
然后：

1. 100ms 节流（`last_rearrange_ns + 100 * MSEC_TO_NSEC`）
   [walt.c:3683](../../kernel/kernel/sched/walt/walt.c#L3683)
2. 遍历 `grp->tasks`，按 `demand_scaled` 取前 `sched_heavy_nr` 名
   （插入排序，`mark_start` 早于 2 个窗口的直接跳过）
3. 给新入选者分配 `pipeline_cpu`：从 `last_available_big_cpus`
   （`cpu_online_mask` 去掉最小簇、去掉 `cpu_halt_mask`）取第一个
   [walt.c:3789](../../kernel/kernel/sched/walt/walt.c#L3789)；
   取不到就从 heavy 里剔除
4. 给上一步的所有入选者置 `WALT_LOW_LATENCY_HEAVY` 位
5. 顺带 `core_ctl_set_boost(true)` / `(false)`——有 heavy 列表时**关掉
   core_ctl 的省电降核**。这就是「isolation boost」。

**轮换**：`rearrange_heavy()` [walt.c:3809](../../kernel/kernel/sched/walt/walt.c#L3809)。
要求 `have_heavy_list > 2` 且 `sysctl_sched_heavy_nr > 2`，找出
`is_max_cluster_cpu()` 的那个（`prime_wts`）和其余里 `demand_scaled` 最大的
（`other_wts`），如果 prime 的 `demand` 反而更小就**交换两者的 `pipeline_cpu`**：

```c
cpu = other_wts->pipeline_cpu;
other_wts->pipeline_cpu = prime_wts->pipeline_cpu;
prime_wts->pipeline_cpu = cpu;                          /* walt.c:3868 */
```

这是「轮换」的实质：**让最重的任务拿 prime 簇的那个 CPU**。
注释写明「assumes just one prime」[walt.c:3851](../../kernel/kernel/sched/walt/walt.c#L3851)
——这个实现假设只有一个 prime 簇，sm8550 的 1×X3 + 4×A715 + 3×A510 正好符合。

**清理**：`remove_heavy()` [walt.c:3644](../../kernel/kernel/sched/walt/walt.c#L3644)
清 `WALT_LOW_LATENCY_HEAVY` 位并腾出表项；
`walt_task_dead()` [walt.c:2419](../../kernel/kernel/sched/walt/walt.c#L2419)
在任务退出时兜底调用（heavy 分支在 [:2428](../../kernel/kernel/sched/walt/walt.c#L2428)）。

### 7.4 三者的关系与两个坑

```
                    grp->skip_min
                          |
        +-----------------+-----------------+
        |                                   |
   find_heaviest_topapp()            add_pipeline() (procfs)
        |                                   |
   heavy_wts[]  --WALT_LOW_LATENCY_HEAVY--> pipeline_wts[]
        |                                   |
        +---------> wts->pipeline_cpu <------+
                          |
              walt_cfs.c PIPELINE_FASTPATH
```

**坑 1：heavy 与 pipeline 的重排互斥** `[反直觉]`。
`rearrange_pipeline_preferred_cpus()` [walt.c:3879](../../kernel/kernel/sched/walt/walt.c#L3879)
的第一行就是：

```c
if (sysctl_sched_heavy_nr)
	return;                                             /* walt.c:3892 */
```

即**只要开了 heavy，pipeline 的 CPU 重排就完全不跑**。这是「heavy 是 pipeline
的自动化版本」的有意设计：`sched_heavy_nr = 0` 时用人工指定的 pipeline，
非零时用自动挑的 heavy。两条路径不要同时开。

**坑 2：`rearrange_heavy()` 用 `demand`，`find_heaviest_topapp()` 用
`demand_scaled`** `[反直觉]`。前者是纳秒等效时间，比较在
[walt.c:3863](../../kernel/kernel/sched/walt/walt.c#L3863)；后者是 1024
满刻度的比例值，比较在 [walt.c:3729](../../kernel/kernel/sched/walt/walt.c#L3729)。
同簇内两者单调性一致，跨簇时理论上可能有分歧（见 §12）。

**坑 3：三个重排都在 `walt_irq_work()` [walt.c:4213](../../kernel/kernel/sched/walt/walt.c#L4213)
里顺序调用**（调用点在 [walt.c:4253](../../kernel/kernel/sched/walt/walt.c#L4253)），
且**只在非迁移（窗口滚动）路径**执行：

```c
if (!is_migration) {
	wrq = (struct walt_rq *) this_rq()->android_vendor_data1;
	find_heaviest_topapp(wrq->window_start);
	rearrange_heavy(wrq->window_start);
	rearrange_pipeline_preferred_cpus(wrq->window_start);
	core_ctl_check(wrq->window_start);
}
```

所以本质上是**每窗口（默认 16/20ms）一次**的批处理，加上 `find_heaviest_topapp`
内部 100ms 的额外节流。任务的 `pipeline_cpu` 在两次重排之间是稳定的。

### 7.5 pipeline_cpu 影响不到 load balance

`pipeline_cpu` 只在**唤醒放置**（`walt_find_energy_efficient_cpu`）时作为偏好
使用。负载均衡里的 `walt_lb_can_migrate_task()` 对 pipeline 任务是**禁止下迁**
[walt_lb.c:253](../../kernel/kernel/sched/walt/walt_lb.c#L253)：

```c
if (walt_pipeline_low_latency_task(p))
	return false;
if (!force && walt_get_rtg_status(p))
	return false;
```

即低延迟任务只会被均衡**往上或平移**，不会被均衡压到更小的簇。

### 7.6 MVP：另一个维度（运行顺序，非放置）

前面几节讲的都是**放置**（去哪个 CPU）。放置之外还有一层**运行顺序**：
`walt_cfs` 维护 MVP 列表 `wrq->mvp_tasks` [walt.h:132](../../kernel/kernel/sched/walt/walt.h#L132)，
MVP 任务可抢占非 MVP。pipeline/heavy 任务在其中拿**最高**优先级
`WALT_LL_PIPE_MVP`，而 RTG 成员拿的 `WALT_RTG_MVP` 是**最低**的
[walt.h:971-974](../../kernel/kernel/sched/walt/walt.h#L971-L974)：

> **[反直觉]** RTG 成员的 MVP 优先级 (`WALT_RTG_MVP = 0`) 是四种里的**最低**。
> RTG 的权利用的不是更高优先级，而是**更长的连续时间片**——源码注释明确写了
> binder 的 MVP「3ms 内失效」，而 RTG 高优先级任务可以跑得更久
> [walt_cfs.c:1219-1225](../../kernel/kernel/sched/walt/walt_cfs.c#L1219-L1225)。
> 且 `task_rtg_high_prio()` [walt.h:868](../../kernel/kernel/sched/walt/walt.h#L868)
> 的判据 `p->prio <= sysctl_walt_rtg_cfs_boost_prio` 默认 99 = **关闭**
> [sysctl.c:72](../../kernel/kernel/sched/walt/sysctl.c#L72)。

MVP 的完整机制（优先级取值、时间片、抢占与降级）见 [rt-mvp.md](09-rt-mvp.md)。

---

## 8. `load_boost` / `boosted_task_load` 与 `scale_exec_time()`

### 8.1 放大点

```c
static inline u64 scale_exec_time(u64 delta, struct rq *rq, struct walt_task_struct *wts)
{
	delta = (delta * wrq->task_exec_scale) >> SCHED_CAPACITY_SHIFT;

	if (wts->load_boost && wts->grp && wts->grp->skip_min)
		delta = (delta * (1024 + wts->boosted_task_load) >> 10);

	return delta;
}
```
[walt.c:1566](../../kernel/kernel/sched/walt/walt.c#L1566)

三个条件**与**在一起：`load_boost` 非零 **且** 任务在某个组里 **且**
该组的 `skip_min` 为真。

两个字段由 procfs 的 `task_load_boost` 节点设置
[sysctl.c:363](../../kernel/kernel/sched/walt/sysctl.c#L363)：

```c
if (pid_and_val[1] < -90 || pid_and_val[1] > 90) { ret = -EINVAL; goto put_task; }
wts->load_boost = val;
if (val)
	wts->boosted_task_load = mult_frac((int64_t)1024, (int64_t)val, 100);
else
	wts->boosted_task_load = 0;
```

取值 **[-90, +90]**，映射成 **[1024-921, 1024+921] = [103, 1945]** 的乘数。
初始化时两者都是 0 [walt.c:2344](../../kernel/kernel/sched/walt/walt.c#L2344)。

### 8.2 为什么挂在 `skip_min` 上

`load_boost` 是用户态声称「这个任务比它看起来更重要」的手段：一个只有 5%
占用但一卡就掉帧的合成线程，可以给它 `load_boost=50`，让 WALT 当 7.5% 算。

挂 `skip_min` 条件是**防止滥用**：只有已经在大核上跑的组才允许放大，否则
只是往小核队列里灌虚高负载。**它是「共置组的上迁加成」，不是通用权重**。

### 8.3 影响面 `[反直觉]`

`scale_exec_time()` 有 13 个调用点（[walt.c:1749](../../kernel/kernel/sched/walt/walt.c#L1749)
起的 `update_cpu_busy_time()` 全部路径 + `add_to_task_demand()` [walt.c:2065](../../kernel/kernel/sched/walt/walt.c#L2065)
+ `update_task_demand()` 的整窗口补记 [walt.c:2175](../../kernel/kernel/sched/walt/walt.c#L2175)）。
所以 boost 同时放大：

- `wts->curr_window` / `prev_window`（任务侧）
- `wrq->curr/prev_runnable_sum` 或 `wrq->grp_time.*`（取决于 §4.1 的指针）
- 经 `update_history()` 进入 `sum_history[]` → 抬高 `demand` / `demand_scaled`
- 经 `add_to_task_demand()` 抬高 `wts->sum` → 抬高 `coloc_demand`

**`wts->sum` 有上限**：`add_to_task_demand()` 里
`if (wts->sum > sched_ravg_window) wts->sum = sched_ravg_window;`
[walt.c:2071](../../kernel/kernel/sched/walt/walt.c#L2071)。
所以极端 boost 下 `coloc_demand` 会饱和在一个窗口——共置判定不会被
`load_boost=90` 直接顶到失控。

> **[反直觉]** 这个放大**完全不改变实际执行时间**，也不改变
> `arch_scale_cpu_capacity` 下的实时利用率。它只是一次**记账层面的放大**，
> 所以它的效果只体现在「放哪个簇」「调多大频」两个下游决策上。
> 调实时利用率指标（如 `cpu_util`）看不到任何变化，容易误判为「boost 没生效」。

---

## 9. 数据流总览

```
cgroup(top-app) attach                     procfs sched_group_id
      |                                          |
      +--> __sched_set_group_id() <--------------+
                |
                v
        wts->grp = grp            <--- add_task_to_group() / add_new_task_to_grp()
                |
                +--> update_cpu_busy_time(): 指针改指 wrq->grp_time   (4.1)
                |           |
                |           +--> cluster->aggr_grp_load               (4.2)
                |                     |
                |                     +--> freq_policy_load() --> 调频
                |                     +--> cpufreq_walt RTG_BOOST
                |
                +--> update_history(): coloc_demand = 5 窗口平均     (5.1)
                |           |
                |           +--> unfilter 充能/衰减                   (5.2)
                |
                +--> _set_preferred_cluster()                        (6.1)
                            |
                            +--> update_best_cluster() --> grp->skip_min
                                        |
                +-----------------------+-----------------------+
                |                       |                       |
        walt_task_skip_min_cpu()  walt_should_kick_upmigrate()  grp_time 调频
        (起始簇抬高)              (从小核踢走, 需 suh_max)        (4.2)
                |
                +--> 唤醒放置 walt_find_energy_efficient_cpu()
                            |
                +-----------+-----------+
                |                       |
        PIPELINE_FASTPATH        pipeline_cpu 分配
        (wts->pipeline_cpu)      (find_heaviest_topapp / add_pipeline)
```

---

## 10. 调试与观测

**trace 事件**（[trace.h](../../kernel/kernel/sched/walt/trace.h)）：

| 事件 | 位置 | 用途 |
|---|---|---|
| `sched_set_preferred_cluster` | [trace.h:323](../../kernel/kernel/sched/walt/trace.h#L323) | 打印 `group_id / total_demand / skip_min / prev_skip_min / start_ktime_ts / min_coloc_ns / downmigrate_ts`，**排查共置方向翻转的首选** |
| `sched_update_history` | [trace.h:70](../../kernel/kernel/sched/walt/trace.h#L70) | 带 `coloc_demand` 字段，看单任务的共置贡献 |
| `sched_cgroup_attach` | [trace.h:1389](../../kernel/kernel/sched/walt/trace.h#L1389) | 打印 attach 时的 `grp_id` 与返回值 |
| `sched_task_util` | [trace.h:1113](../../kernel/kernel/sched/walt/trace.h#L1113) | 放置路径，含 `is_rtg / rtg_skip_min / load_boost / pipeline_cpu` |

**procfs / sysctl 旋钮**（都在 `/proc/sys/kernel/`）：

| 节点 | 默认 | 位置 |
|---|---|---|
| `sched_group_upmigrate` | 上迁阈值（%） | [sysctl.c:606](../../kernel/kernel/sched/walt/sysctl.c#L606) |
| `sched_group_downmigrate` | 下迁阈值（%） | [sysctl.c:614](../../kernel/kernel/sched/walt/sysctl.c#L614) |
| `sched_min_task_util_for_colocation` | 35 | [sysctl.c:678](../../kernel/kernel/sched/walt/sysctl.c#L678) |
| `sched_task_unfilter_period` | 100000000 (100ms) | [sysctl.c:702](../../kernel/kernel/sched/walt/sysctl.c#L702) |
| `sched_hyst_min_coloc_ns` | 80000000 (80ms) | [sysctl.c:881](../../kernel/kernel/sched/walt/sysctl.c#L881) |
| `sched_heavy_nr` | 0（关闭） | [sysctl.c:1056](../../kernel/kernel/sched/walt/sysctl.c#L1056) |
| `walt_rtg_cfs_boost_prio` | 99（关闭） | [sysctl.c:827](../../kernel/kernel/sched/walt/sysctl.c#L827) |
| `task_load_boost` | 0，范围 [-90, 90] | [sysctl.c:968](../../kernel/kernel/sched/walt/sysctl.c#L968) |
| `sched_low_latency` / `sched_pipeline` | 0 | [sysctl.c:954](../../kernel/kernel/sched/walt/sysctl.c#L954) / [:961](../../kernel/kernel/sched/walt/sysctl.c#L961) |

**排查要点**（按「先看哪一层」排序）：

1. 组到底建起来没有 → `sched_cgroup_attach` + `/proc/<pid>/sched_group_id`
2. 组的方向对不对 → `sched_set_preferred_cluster` 的 `skip_min`
3. 任务有没有被 `unfilter` 门槛挡住 → `sched_update_history` 的 `coloc_demand` 与
   `sched_task_util` 的 `unfilter` 字段
4. 快速路径为什么没走 → `sched_task_util` 的 `fastpath` 与 `pipeline_cpu`
5. 组负载有没有反映到频率 → `grp_time` 相关计数与 `CPUFREQ_REASON_RTG_BOOST`

---

## 11. 与相邻机制的区别（易混清单）

| 机制 | 作用域 | 影响什么 | 本文出处 |
|---|---|---|---|
| RTG / `grp_time` | 一组任务 | 频率与簇方向 | §2–§4 |
| `skip_min` | 一个组 | 不从小核开始找 / 小核踢出 | §5–§6 |
| `pipeline_cpu` | 一个任务 | 唤醒放置走快速路径 | §7.2、§7.3 |
| `mvp_prio` | 一个任务 | 运行时抢占顺序（不是放置） | §7.6 |
| `load_boost` | 一个任务 | 记账放大（不是权重） | §8 |
| `walt_rotation_enabled` | 系统 | 大核轮转 / 负载均衡方向 | 见 [walt_lb.c:526](../../kernel/kernel/sched/walt/walt_lb.c#L526)，由 `core_ctl` 驱动 [core_ctl.c:792](../../kernel/kernel/sched/walt/core_ctl.c#L792) |

> **`walt_rotation` 与 heavy 无关**。前者是「大核之间轮转任务」以散热/公平，
> 由 `walt_rotation_checkpoint()` [walt.c:4260](../../kernel/kernel/sched/walt/walt.c#L4260)
> 在 `core_ctl` 里设置；后者是「挑出共置组里最重的几个」。两者名字里都有
> 「轮转/重载」的意味，极易混淆。

---

## 12. 遗留问题

以下条目的前三项已登记到 [04-open-questions.md](../03-comparison/04-open-questions.md)（2026-09-17 核对）。

- `rearrange_heavy()` 用 `wts->demand`，`find_heaviest_topapp()` 用
  `wts->demand_scaled`（§7.4 坑 2）。**`[待确认]`**：异频/异容量场景下两者
  排序是否可能不一致，以及这是否有意为之。
- `sysctl_sched_coloc_downmigrate_ns` 在 `sysctl.c` 声明时未给初值，
  默认 0 会让 `update_best_cluster()` 走「立即下迁」分支。
  **`[待确认]`**：是否由用户态初始化脚本设置。
- `get_rtgb_active_time()` [walt.c:3530](../../kernel/kernel/sched/walt/walt.c#L3530)
  的消费方未逐一追踪。**`[待确认]`**。
- `walt_task_group->sched_boost_enable` 与 RTG 的交互（组 boost 会把
  `skip_min` 强制清零，§6.2 第一条分支）耦合较深，本文只覆盖 RTG 侧。
  **`[推测]`**：完整的 boost 语义归入 [boost.md](08-boost.md)。

---

## 13. 相关文档

- 字段定义 → [03-data-structures.md](../00-overview/03-data-structures.md)
- hook 注册与调用点 → [02-integration-model.md](../00-overview/02-integration-model.md)
- 窗口模型 / `grp_time` 记账细节 → [window-model.md](01-window-model.md)
- 需求预测（`pred_demand` 与 `demand` 的区别）→ [demand-prediction.md](02-demand-prediction.md)
- 放置算法 → [placement.md](04-placement.md)
- 调频（`grp_time` / `aggr_grp_load` 的消费）→ [cpufreq.md](03-cpufreq.md)
- MVP 运行时优先级 → [rt-mvp.md](09-rt-mvp.md)
- boost 类型模型 → [boost.md](08-boost.md)
- core_ctl 与共置上报 → [power-side.md](07-power-side.md)
- 负载均衡（`pipeline_cpu` 的均衡侧约束）→ [load-balance.md](05-load-balance.md)
- 未决问题登记 → [04-open-questions.md](../03-comparison/04-open-questions.md)
- 与 baseline EAS 的对比 → [04-placement-eas.md](../01-baseline/04-placement-eas.md)
