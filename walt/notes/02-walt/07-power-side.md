# core_ctl 上下线、walt_halt 与 hotplug 的区别

> **源码**：[core_ctl.c](../../kernel/kernel/sched/walt/core_ctl.c)、[walt_halt.c](../../kernel/kernel/sched/walt/walt_halt.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

---

## 0. 一句话结论 [反直觉]

**在 sm8550 / 5.15 这一版上，core_ctl 已经不再对 CPU 做 hotplug 了。**

`core_ctl.c` 里没有任何 `cpu_up()` / `cpu_down()` / `cpu_device_down()`；
全文唯一一处 `cpu_online()` 出现在 sysfs 的打印里
[show_global_state() core_ctl.c:332](../../kernel/kernel/sched/walt/core_ctl.c#L332)。
它现在是个**策略层**，执行机构是 [walt_halt.c](../../kernel/kernel/sched/walt/walt_halt.c)
提供的 **halt / pause**：

- **halt** = 把 CPU 挂起，**CPU 仍然 online**：(a) 用 `cpu_halt_mask` 拒绝新任务落上来，
  (b) 把 rq 上已有的任务迁移走（drain）；
- **hotplug** = 真正的 `cpu_down()`，CPU 从 `cpu_online_mask` 消失。

> `core_ctl.c` 里到处是 `offline_delay_ms`、`cpus_paused_by_us` 这类名字，但都是
> **历史遗留命名**——4.x 的 core_ctl 确实调 `cpu_up()/cpu_down()`。别被名字带走。[推测]

> **对任务简报的三处更正**（均已 `grep -rn` 全树确认不存在）：
> `walt_halt_lb_*` API、`struct core_ctl_cluster`、`core_ctl_do_offline()` /
> `core_ctl_do_online()`、字段 `nr_need_cpus` / `nr_run` 在本版源码中都不存在。
> 实际名字是 `struct cluster_data`
> [core_ctl.c:26](../../kernel/kernel/sched/walt/core_ctl.c#L26)、
> `do_core_ctl()`、字段 `need_cpus` / `nrrun`、局部变量 `nr_need`
> [compute_cluster_nr_need() core_ctl.c:529](../../kernel/kernel/sched/walt/core_ctl.c#L529)。
> halt 与 load balancer 之间**没有专用 API**，是 LB 内部直接查 `cpu_halted()`（§3.7）。

---

## 1. 为什么需要 core_ctl

WALT 调频决定"CPU 跑多快"，**跑几个 CPU** 是另一个自由度。轻负载下多留一个 online 核
的代价不是线性的：nohz tick / RCU 回调 / timer migration 继续运转、簇级电源域无法塌缩、
长尾任务被摊薄到每个核都跑在低效负载点。core_ctl 因此是**窗口边界上的核数决策**——
这也解释了它"只能在窗口滚动时调用"的硬契约（§4.2）。core_ctl 在主线 Linux 里不存在，
是 QTI/CAF 的 out-of-tree 逻辑，随 `CONFIG_SCHED_WALT` 编进 `sched-walt.ko`
（[Makefile](../../kernel/kernel/sched/walt/Makefile)）。

---

## 2. 三个概念的边界

| 维度 | hotplug（`cpu_up/cpu_down`） | halt / pause（`walt_halt.c`） | core_ctl（`core_ctl.c`） |
|---|---|---|---|
| 本质 | 电源 / 生命周期管理 | **放置过滤 + 任务驱逐** | 策略：halt 不 halt、halt 几个 |
| `cpu_online_mask` | 清除 | **保持** | 无关 |
| 执行者 | `kernel/cpu.c` | `walt_halt_cpus()` [walt_halt.c:380](../../kernel/kernel/sched/walt/walt_halt.c#L380) | `do_core_ctl()` [core_ctl.c:1378](../../kernel/kernel/sched/walt/core_ctl.c#L1378) |
| 停止范围 | `stop_machine()` 全局 | 只 `stop_one_cpu()` 目标核 [walt_halt.c:214](../../kernel/kernel/sched/walt/walt_halt.c#L214) | — |
| 任务去向 | 全局迁移 + RCU 协调 | drain：`migrate_tasks()` [walt_halt.c:76](../../kernel/kernel/sched/walt/walt_halt.c#L76) | — |
| 拒绝新任务的机制 | `cpu_online()` / `cpu_active()` | 4 个 vendor hook（§3.6） | — |
| 用户可见 | `/sys/.../cpuN/online` | `.../cpuN/core_ctl/global_state` 的 `Paused:` | 同左 |
| 其它使用者 | thermal、用户态 | thermal、hyp、core_ctl（见下） | — |

**core_ctl 和 thermal / hyp 共用同一套 halt 机制**，只是 `enum pause_reason` 的 bit 不同
[include/linux/sched/walt.h:14](../../kernel/include/linux/sched/walt.h#L14)：

```c
enum pause_reason {
	PAUSE_CORE_CTL	= 0x01,
	PAUSE_THERMAL	= 0x02,
	PAUSE_HYP	= 0x04,
};
```

调用方：[thermal_pause.c:111](../../kernel/drivers/thermal/qcom/thermal_pause.c#L111)、
[hyp_core_ctl.c:119](../../kernel/drivers/soc/qcom/hyp_core_ctl.c#L119)。

---

## 3. walt_halt：halt 到底是什么

整个文件被 `#ifdef CONFIG_HOTPLUG_CPU` 包住
[walt_halt.c:13](../../kernel/kernel/sched/walt/walt_halt.c#L13) /
[:613](../../kernel/kernel/sched/walt/walt_halt.c#L613)。**[推测]** arm64 上该配置默认为 y；
反过来若关掉，`__cpu_halt_mask` 无定义而 `core_ctl.c` 无条件引用 `cpu_halted()`，
会直接链接失败——所以它对 WALT 是**事实必需**的。

### 3.1 halt 的四步

`halt_cpus()` [walt_halt.c:276](../../kernel/kernel/sched/walt/walt_halt.c#L276) 只做两件事：

```c
/* halt_cpus() */
cpumask_set_cpu(cpu, cpu_halt_mask);   /* walt_halt.c:298 */  ← 同步，立刻生效
wmb();
halt_cpu_state->last_halt = start_time; /* walt_halt.c:303 */
...
cpumask_or(&drain_data.cpus_to_drain, ..., cpus);  /* walt_halt.c:308 */
wake_up_process(walt_drain_thread);                /* walt_halt.c:311 */
```

即**置 mask（同步）+ 把"拉走任务"丢给 drain 线程（异步）**。
mask 一旦置上，后续放置决策就绕开这个 CPU（§3.6）；已有任务稍后被 drain。

`cpu_halt_mask` 是全局 cpumask
[walt.h:1007-1009](../../kernel/kernel/sched/walt/walt.h#L1007-L1009)：

```c
extern struct cpumask __cpu_halt_mask;
#define cpu_halt_mask ((struct cpumask *)&__cpu_halt_mask)
#define cpu_halted(cpu) cpumask_test_cpu((cpu), cpu_halt_mask)
```

反向 `start_cpus()` [walt_halt.c:323](../../kernel/kernel/sched/walt/walt_halt.c#L323)
清 mask / 清 `last_halt`，再**踢一次 newidle balance**，让本核主动去别的核拉任务：

```c
/* start_cpus() */
cpumask_clear_cpu(cpu, cpu_halt_mask);        /* walt_halt.c:338 */
walt_smp_call_newidle_balance(cpu);           /* walt_halt.c:343 */
```

`walt_smp_call_newidle_balance()` [walt_lb.c:1013](../../kernel/kernel/sched/walt/walt_lb.c#L1013)
用 `smp_call_function_single_async()`，因此在硬中断上下文里调用也是安全的。

### 3.2 drain：任务怎么被搬走

drain 由独立 kthread `halt_drain_rqs` 完成
`try_drain_rqs()` [walt_halt.c:240](../../kernel/kernel/sched/walt/walt_halt.c#L240)，
kthread 在 `walt_halt_init()` [walt_halt.c:584](../../kernel/kernel/sched/walt/walt_halt.c#L584)
创建并设成 `SCHED_FIFO` / `MAX_RT_PRIO-1`。单核路径 `cpu_drain_rq()`：

```c
if (!cpu_online(cpu))  return 0;        /* walt_halt.c:207 */
if (available_idle_cpu(cpu)) return 0;  /* walt_halt.c:210 —— 已经空了就不折腾 */
return stop_one_cpu(cpu, drain_rq_cpu_stop, NULL);  /* walt_halt.c:214 */
```

`stop_one_cpu()` 会 **schedule**，必须在进程上下文跑——这正是 drain 得交给 kthread、
不能在 `core_ctl_check()` 里直接做的原因（§4.4）。

`drain_rq_cpu_stop()` [walt_halt.c:181](../../kernel/kernel/sched/walt/walt_halt.c#L181)
在 stop 上下文里调 `migrate_tasks()` [walt_halt.c:76](../../kernel/kernel/sched/walt/walt_halt.c#L76)：
**复用 `cpu_down()` 的迁移骨架，但不依赖全局 stop_machine**，只停这一个 CPU；
pinned 的 per-cpu kthread 会被摘下暂存
（`detach_one_task_core()` [walt_halt.c:40](../../kernel/kernel/sched/walt/walt_halt.c#L40)）再挂回。
收尾处有一道断言，用来抓"drain 之后任务又被塞回来"：

```c
/* drain_rq_cpu_stop() */
wrq->enqueue_counter = 0;
__balance_callbacks(rq);
if (wrq->enqueue_counter)
	WALT_BUG(..., "cpu: %d task was re-enqueued", cpu_of(rq)); /* walt_halt.c:198 */
```

### 3.3 没有 idle injection [反直觉] · halt ≠ offline 的收益

简报里猜"halt = 任务迁移 + idle injection"。**源码里没有任何 idle injection**——
`kernel/kernel/sched/walt/` 下 `grep -rni "idle_inject"` 零命中。halt 之后 CPU 靠
**自然空闲**进入低功耗态：drain 把任务搬空、mask 又不让新任务落上来，rq 自然
`nr_running == 0`，然后走正常 cpuidle。配合
[android_rvh_get_nohz_timer_target() walt_halt.c:455](../../kernel/kernel/sched/walt/walt_halt.c#L455)
（nohz tick 目标避开 halted 核）才能稳定停在深 idle。

所以 halt 与 hotplug **功耗收益接近、延迟差一个数量级**：halt 不销毁调度域、不重启
RCU 状态、不重建 cpufreq policy，恢复时只是一次 newidle balance。最直接的证据是
`walt_find_and_choose_cluster_packing_cpu()`
[walt.h:1023](../../kernel/kernel/sched/walt/walt.h#L1023) —— 它用
`cpu_active_mask & ~cpu_halt_mask` 选核，说明 **halted 的核仍然是 `active` 的**。

### 3.4 `reason` 位图 = 引用计数 [反直觉]

`halt_state->reason` 不是"当前原因"，而是**所有仍在请求 halt 的子系统 bit 的并集**：

```c
/* update_reasons() */
if (halt) halt_cpu_state->reason |=  reason;   /* walt_halt.c:360 */
else      halt_cpu_state->reason &= ~reason;   /* walt_halt.c:362 */
```

`walt_halt_cpus()` [walt_halt.c:380](../../kernel/kernel/sched/walt/walt_halt.c#L380)
因此是：① `update_halt_cpus(cpus)` 把**已 halted** 的核从入参 mask 剔除（判据 `reason != 0`
[walt_halt.c:374](../../kernel/kernel/sched/walt/walt_halt.c#L374)）；② 若剔空 → 直接返回，
但**仍 `update_reasons(..., true, reason)` 记账**
[walt_halt.c:394](../../kernel/kernel/sched/walt/walt_halt.c#L394)；③ 否则 `halt_cpus()` 后记账。

反向 `walt_start_cpus()` [walt_halt.c:420](../../kernel/kernel/sched/walt/walt_halt.c#L420)
顺序相反且语义微妙：**先清自己的 bit，再看还有没有别人**
（[:428](../../kernel/kernel/sched/walt/walt_halt.c#L428) → [:431](../../kernel/kernel/sched/walt/walt_halt.c#L431)），
即**只有别的子系统也不再需要时才真解除 halt**；出错则把 bit 加回
[walt_halt.c:439](../../kernel/kernel/sched/walt/walt_halt.c#L439)。
这层引用计数是必需的：thermal 与 core_ctl 完全可能同时要求停用一个核。

### 3.5 第一个 CPU 不可 halt

```c
/* halt_cpus(), walt_halt.c:288-293 */
if ((cpumask_empty(system_32bit_el0_cpumask()) &&
	(cpu == cpumask_first(cpu_possible_mask))) ||
    (cpu == cpumask_first(system_32bit_el0_cpumask()))) {
	ret = -EINVAL;
	goto out;
}
```

理由：32 位任务只能在 32 位 capable 的核上跑；大小核全对称时也必须留一个核。
`for_each_cpu()` 升序，第一个 CPU 命中即 `goto out`，不会留下半 halt 状态。
`walt_halt_init()` 里还有一条**防 hotplug 而非 halt** 的互补设置
[walt_halt.c:600-603](../../kernel/kernel/sched/walt/walt_halt.c#L600-L603)：
给第一个 possible CPU 置 `offline_disabled`。这个标志又被 core_ctl 读走，
成为"永远不要选它去 pause"的依据（§4.10）。

### 3.6 halt 影响放置：四个 hook

halt 不修改任何原生 mask，而是靠 4 个 vendor hook 让调度器**绕开**这些核
（注册点 [walt_halt.c:605-609](../../kernel/kernel/sched/walt/walt_halt.c#L605-L609)，
与原生调用点的完整对照见 [integration-model.md §5.5](../00-overview/02-integration-model.md)）：

| 回调 | 作用 |
|---|---|
| [android_rvh_is_cpu_allowed() walt_halt.c:563](../../kernel/kernel/sched/walt/walt_halt.c#L563) | halted 核一律 `*allowed = false`（例外：32 位任务 execve 中） |
| [android_rvh_set_cpus_allowed_by_task() walt_halt.c:516](../../kernel/kernel/sched/walt/walt_halt.c#L516) | `set_cpus_allowed` 目标落在 halted 上时重新选核 |
| [android_rvh_rto_next_cpu() walt_halt.c:544](../../kernel/kernel/sched/walt/walt_halt.c#L544) | RT 溢出时避开 halted 核 |
| [android_rvh_get_nohz_timer_target() walt_halt.c:455](../../kernel/kernel/sched/walt/walt_halt.c#L455) | nohz tick 目标避开 halted 核（保深 idle） |

### 3.7 与 load balancer 的交互（无专用 API）

**没有 `walt_halt_lb_*`**（`grep -rn "walt_halt_lb\|halt_lb"` 零命中）。交互只有三处内联判断：

- 不许往 halted 核 detach 任务：[walt_lb.c:264](../../kernel/kernel/sched/walt/walt_lb.c#L264)
  （`if (cpu_halted(dst_cpu)) return false;`）；
- halted 核自己不进 newidle balance：
  [walt_lb.c:838](../../kernel/kernel/sched/walt/walt_lb.c#L838)；
- 解除 halt 后补一次 newidle balance：`walt_smp_newidle_balance()`
  [walt_lb.c:996](../../kernel/kernel/sched/walt/walt_lb.c#L996)，注释写得很直白：
  `/* run newidle balance as a result of an unhalt operation */`。

### 3.8 400µs 宽限窗口

halt 置 mask 与"任务已在 enqueue 路径上"之间有极窄竞态。WALT 不消除它，而是容忍 + 事后报 BUG：

```c
/* walt_halt_check_last(), walt_halt.c:226 */
if (last_halt != 0 && sched_clock() - last_halt > WALT_HALT_CHECK_THRESHOLD_NS)
	return false;
```

`WALT_HALT_CHECK_THRESHOLD_NS` = 400000
[walt_halt.c:32](../../kernel/kernel/sched/walt/walt_halt.c#L32)。`last_halt` 在 halt 时写入、
start 时清零，所以这个函数在问"这核是不是刚（400µs 内）被 halt 过"。唯一调用点在 enqueue 路径：

```c
/* walt.c:4638 */
if (cpu_halted(cpu_of(rq)) && !(p->flags & PF_KTHREAD) && !walt_halt_check_last(cpu_of(rq)))
	WALT_BUG(WALT_BUG_NONCRITICAL, p, "Non Kthread Started on halted cpu_of(rq)=%d ...");
```

即**400µs 内落上来的非 kthread 任务只记一笔、不判错**。trace 里大量出现它就不是竞态，
而是放置逻辑真的漏了。

---

## 4. core_ctl：策略层

### 4.1 数据结构

- `struct cluster_data` [core_ctl.c:26](../../kernel/kernel/sched/walt/core_ctl.c#L26)：每簇一份，
  静态数组 `cluster_state[MAX_CLUSTERS]` [core_ctl.c:69](../../kernel/kernel/sched/walt/core_ctl.c#L69)，
  `MAX_CLUSTERS=3`、`MAX_CPUS_PER_CLUSTER=6`
  [include/linux/sched/walt.h:22-23](../../kernel/include/linux/sched/walt.h#L22-L23)。
- `struct cpu_data` [core_ctl.c:58](../../kernel/kernel/sched/walt/core_ctl.c#L58)：`DEFINE_PER_CPU`，
  字段 `is_busy` / `busy_pct` / `not_preferred` / `disabled`。
- 每簇一个 `lru` 链表串起自己的 CPU；**每次被 pause/resume 的核都被
  `move_cpu_lru()` [core_ctl.c:1170](../../kernel/kernel/sched/walt/core_ctl.c#L1170) 移到链尾**，
  于是链表顺序 = LRU 顺序，从头扫就是"最久没动过的先动"。
- 全局 `cpus_paused_by_us` [core_ctl.c:24](../../kernel/kernel/sched/walt/core_ctl.c#L24) 记录
  "本模块声称 halt 的核"，与 halt 层的 `reason` 位图配对（§4.11）。

### 4.2 入口 `core_ctl_check()` 与同窗口去重

调用点在 `walt_irq_work()` 的非迁移分支末尾 [walt.c:4256](../../kernel/kernel/sched/walt/walt.c#L4256)：

```c
/* walt_irq_work(), walt.c:4250-4257 */
if (!is_migration) {
	wrq = (struct walt_rq *) this_rq()->android_vendor_data1;
	find_heaviest_topapp(wrq->window_start);
	rearrange_heavy(wrq->window_start);
	rearrange_pipeline_preferred_cpus(wrq->window_start);
	core_ctl_check(wrq->window_start);
}
```

函数体第一件事是**同窗口去重**
[core_ctl_check() core_ctl.c:1141](../../kernel/kernel/sched/walt/core_ctl.c#L1141)
（`core_ctl_check_timestamp` 定义于 [core_ctl.c:1043](../../kernel/kernel/sched/walt/core_ctl.c#L1043)）：

```c
if (window_start == core_ctl_check_timestamp)
	return;
core_ctl_check_timestamp = window_start;
```

原因写在紧邻的契约注释里
[core_ctl.c:1121-1128](../../kernel/kernel/sched/walt/core_ctl.c#L1121-L1128)：

> `sched_get_nr_running_avg` will wipe out previous statistics and update it to the
> values computed since the last call. `core_ctl_check` assumes that the statistics
> are stable, hence window based. Therefore `core_ctl_check` must only be called
> from window rollover, or `walt_irq_work` for not migration.

`sched_get_nr_running_avg()` [sched_avg.c:71](../../kernel/kernel/sched/walt/sched_avg.c#L71)
是**读并清零**的 [sched_avg.c:118-119](../../kernel/kernel/sched/walt/sched_avg.c#L118-L119)。
窗口滚动本身有全局去重（`run_walt_irq_work_rollover()` 的 `atomic64_cmpxchg`
[walt.c:2279](../../kernel/kernel/sched/walt/walt.c#L2279)），但 `core_ctl_check()` 是 public
符号——**窗口中途的第二次调用会把统计清空**，下次读到的是"窗口剩余部分"。更多细节见
[data-flow.md §5](../00-overview/04-data-flow.md)。

### 4.3 `core_ctl_check()` 的四段

```c
/* core_ctl_check(), core_ctl.c:1146-1166 */
spin_lock_irqsave(&state_lock, flags);
for_each_possible_cpu(cpu) {
	c = &per_cpu(cpu_state, cpu);
	cluster = c->cluster;
	if (!cluster || !cluster->inited) continue;
	c->busy_pct = sched_get_cpu_util_pct(cpu);   /* ① 采忙度 */
}
spin_unlock_irqrestore(&state_lock, flags);

update_running_avg();                            /* ② 采 nr 类统计 */
for_each_cluster(cluster, index)
	wakeup |= (eval_need(cluster) || eval_need_32bit(cluster));  /* ③ 决策 */
if (wakeup)
	do_core_ctl();                               /* ④ 执行 */
core_ctl_call_notifier();                        /* ⑤ 广播 */
```

①②顺序有意义：`sched_get_nr_running_avg()` 必须在所有 `busy_pct` 采完之后调，
否则它自己取 `rq->__lock` 会与采样交错。

### 4.4 两个执行者 [反直觉]

`do_core_ctl()` 有**两条**入口：

| 触发者 | 路径 | 上下文 |
|---|---|---|
| 窗口滚动 | `core_ctl_check()` → `do_core_ctl()` [core_ctl.c:1165](../../kernel/kernel/sched/walt/core_ctl.c#L1165) | **irq_work / 硬中断**，同步 |
| sysfs 写、boost 变化 | `apply_need()` → `wake_up_core_ctl_thread()` [core_ctl.c:1024](../../kernel/kernel/sched/walt/core_ctl.c#L1024) | kthread `core_ctl` |

**常态路径根本不经过 core_ctl 自己的 kthread**。`try_core_ctl()`
[core_ctl.c:1419](../../kernel/kernel/sched/walt/core_ctl.c#L1419) 只在 `core_ctl_pending`
被置位时才醒——那是 sysfs 写 `min_cpus`/`max_cpus`/`busy_up_thres` 或
`core_ctl_set_boost()` 走的路。它敢在硬中断里跑，是因为**只做决策和置 mask**，
会睡眠的动作都推给别人：`walt_halt_cpus()` 只 `cpumask_set_cpu()` + `wake_up_process()`；
`walt_smp_call_newidle_balance()` 是 async IPI；真正 schedule 的 `stop_one_cpu()` 在
drain kthread 里。`walt_irq_work()` 自己的注释也写明了："Process a workqueue call
scheduled, while running in a **hard irq protected context**"
[walt.c:4208](../../kernel/kernel/sched/walt/walt.c#L4208)。两条路径共用 `state_lock` 与
`core_ctl_pending`，所以并发安全；但也因此**可能重复调 `eval_need()`**，故每簇另有
`offline_delay_ms` 去抖。[推测]：sysfs 写 `min_cpus` 不必等下一个窗口才生效，
但窗口滚动那次一定会覆盖它。

### 4.5 决策变量总览

| 字段 | 写入者 | 含义 |
|---|---|---|
| `active_cpus` | `get_active_cpu_count()` [core_ctl.c:862](../../kernel/kernel/sched/walt/core_ctl.c#L862) | `cpu_mask & ~cpu_halt_mask` 的权重 = 当前真正可用的核数 |
| `need_cpus` | `eval_need()` | 决策出的"本簇需要几个核" |
| `need_32bit_cpus` / `last_need_32bit_cpus` | `update_running_avg()` [core_ctl.c:776-777](../../kernel/kernel/sched/walt/core_ctl.c#L776-L777) | 32 位大任务需求的当前值 / 上一窗口值 |
| `nrrun` | `update_running_avg()` | 本簇 nr 需求 + 上一簇 misfit（§4.7） |
| `max_nr` | `compute_cluster_max_nr()` [core_ctl.c:597](../../kernel/kernel/sched/walt/core_ctl.c#L597) | 本簇单核在一个窗口内见过的**最大瞬时 nr_running** |
| `nr_prev_assist` | `prev_cluster_nr_need_assist()` [core_ctl.c:648](../../kernel/kernel/sched/walt/core_ctl.c#L648) | 上一簇核不够时本簇要"支援"几个核 |
| `strict_nrrun` | `compute_cluster_nr_strict_need()` [core_ctl.c:721](../../kernel/kernel/sched/walt/core_ctl.c#L721) | little 簇的硬下界 |
| `need_ts` | `eval_need()` | 需求变化时间戳，配 `offline_delay_ms` 去抖 |
| `boost` | `core_ctl_set_boost()` [core_ctl.c:1045](../../kernel/kernel/sched/walt/core_ctl.c#L1045) | >0 强制全核 |
| `enable` | sysfs | false 时 `apply_limits()` 返回 `num_cpus`（§4.9） |

### 4.6 `eval_need()`：要不要现在动手

```c
/* eval_need(), core_ctl.c:904-951 */
if (cluster->boost || !cluster->enable) {
	need_cpus = cluster->max_cpus;                     /* 短路：全开 */
} else {
	thres_idx = cluster->active_cpus ? cluster->active_cpus - 1 : 0;
	list_for_each_entry(c, &cluster->lru, sib) {
		if (c->busy_pct >= cluster->busy_up_thres[thres_idx] ||
		    sched_cpu_high_irqload(c->cpu))
			c->is_busy = true;
		else if (c->busy_pct < cluster->busy_down_thres[thres_idx])
			c->is_busy = false;
		need_cpus += c->is_busy;
	}
	need_cpus = apply_task_need(cluster, need_cpus);
}
new_need = apply_limits(cluster, need_cpus);
...
if (new_need > cluster->active_cpus) {
	adj_now = true;                       /* 加核：立刻，不等 */
} else {
	if (new_need == last_need && new_need == cluster->active_cpus) {
		cluster->need_ts = now;       /* 稳态：只刷新时间戳 */
		goto unlock;
	}
	adj_now = (now - cluster->need_ts) >= cluster->offline_delay_ms;  /* 减核：等 */
}
if (adj_now) {
	adj_possible = adjustment_possible(cluster, new_need);
	cluster->need_ts = now;
	cluster->need_cpus = new_need;
}
```

四个要点：

1. **`is_busy` 是带迟滞的状态位**，不是每轮重算：越过 `busy_up_thres[thres_idx]` 才置位、
   掉到 `busy_down_thres[thres_idx]` 以下才清零，中间保持原值。`thres_idx` 取决于当前
   active 核数——**阈值表是按核数索引的曲线**。
2. `sched_cpu_high_irqload()` [walt.h:715](../../kernel/kernel/sched/walt/walt.h#L715) 是第二个
   "忙"的来源：IRQ 负载超过窗口 95%（`walt_cpu_high_irqload`
   [walt.c:4344](../../kernel/kernel/sched/walt/walt.c#L4344)）也算忙，即使 CFS 统计看着空闲。
   目的是防止"IRQ 打满但看起来 idle 的核被关掉"。
3. **加核立即、减核延迟**是核心不对称性。`offline_delay_ms` 默认 **100ms**
   [core_ctl.c:1496](../../kernel/kernel/sched/walt/core_ctl.c#L1496)。
4. 稳态分支那次 `need_ts = now` 很容易看漏：[反直觉] 它让"长期不动"的簇 `need_ts` 一直新鲜，
   **避免稳定负载下的周期性抖动**；只有需求真变化过，计时器才从那一刻重新起步。

`adjustment_possible()` [core_ctl.c:882](../../kernel/kernel/sched/walt/core_ctl.c#L882)
是执行能力检查——加核**必须真的还有 paused 的核可用**：

```c
return (need < cluster->active_cpus ||
	(need > cluster->active_cpus && cluster_paused_cpus(cluster)));
```

`cluster_paused_cpus()` [core_ctl.c:304](../../kernel/kernel/sched/walt/core_ctl.c#L304)
= `cpu_mask & cpus_paused_by_us`，算的是**本模块**暂停的核，
thermal 借走的不算 core_ctl 的备用池。

### 4.7 `update_running_avg()`：从 WALT 统计算需求

```c
/* update_running_avg(), core_ctl.c:760-792 */
nr_stats = sched_get_nr_running_avg();
...
nr_need          = compute_cluster_nr_need(index);
prev_misfit_need = compute_prev_cluster_misfit_need(index);
cluster->nrrun   = nr_need + prev_misfit_need;
cluster->max_nr  = compute_cluster_max_nr(index);
cluster->nr_prev_assist = prev_cluster_nr_need_assist(index);
cluster->last_need_32bit_cpus = cluster->need_32bit_cpus;
cluster->need_32bit_cpus = compute_cluster_nr_need_32bit(index);
cluster->nr_prev_assist_32bit = prev_cluster_nr_need_assist_32bit(index);
cluster->strict_nrrun = compute_cluster_nr_strict_need(index);
big_avg += cluster_real_big_tasks(index);
...
last_nr_big = big_avg;                       /* core_ctl.c:791 */
walt_rotation_checkpoint(big_avg);           /* core_ctl.c:792 */
```

| 函数 | 语义 |
|---|---|
| `compute_cluster_nr_need()` [core_ctl.c:529](../../kernel/kernel/sched/walt/core_ctl.c#L529) | **[反直觉]** 从本簇起**一路累加到最高容量簇**。注释的 4+4 例子：大簇 2 个任务 + 小簇 4 个任务 → 小簇 `nr_need` = 6，因为小簇要为跑在大簇上的任务留位 |
| `compute_prev_cluster_misfit_need()` [core_ctl.c:576](../../kernel/kernel/sched/walt/core_ctl.c#L576) | 上一簇里"放不下、该往本簇迁"的任务数（`nr_stats[cpu].nr_misfit` [core_ctl.c:592](../../kernel/kernel/sched/walt/core_ctl.c#L592)）；`index == 0` 时为 0 |
| `compute_cluster_max_nr()` [core_ctl.c:597](../../kernel/kernel/sched/walt/core_ctl.c#L597) | 簇内 `nr_max` 的最大值 |
| `prev_cluster_nr_need_assist()` [core_ctl.c:648](../../kernel/kernel/sched/walt/core_ctl.c#L648) | `上一簇总 nr + 上一簇 misfit − 上一簇 active_cpus`，即"上一簇缺几个核" |
| `compute_cluster_nr_strict_need()` [core_ctl.c:721](../../kernel/kernel/sched/walt/core_ctl.c#L721) | **只对 little 簇实现**（`index != 0 \|\| num_clusters < 2` 直接返回 0）。用 `nr_scaled`（放大 100 倍）把大簇溢出量算成 little 簇的硬下界 |
| `cluster_real_big_tasks()` [core_ctl.c:609](../../kernel/kernel/sched/walt/core_ctl.c#L609) | 给 big-task rotation 用（§5.4） |

### 4.8 `apply_task_need()`：任务维度的四条修正

`apply_task_need()` [core_ctl.c:797](../../kernel/kernel/sched/walt/core_ctl.c#L797)
在忙度统计之外追加：

```c
if (cluster->nrrun >= cluster->task_thres) return cluster->num_cpus;   /* 801 全开 */
if (cluster->nr_prev_assist >= cluster->nr_prev_assist_thresh)         /* 808 */
	new_need = new_need + cluster->nr_prev_assist;
if (cluster->nrrun > new_need)  new_need = new_need + 1;               /* 812 */
if (cluster->max_nr > MAX_NR_THRESHOLD) new_need = new_need + 1;       /* 820 */
if (new_need < cluster->strict_nrrun) new_need = cluster->strict_nrrun;/* 828 */
```

`MAX_NR_THRESHOLD` = 4 [core_ctl.c:795](../../kernel/kernel/sched/walt/core_ctl.c#L795)。
`task_thres` 与 `nr_prev_assist_thresh` 的**默认值都是 `UINT_MAX`**
[core_ctl.c:1497-1498](../../kernel/kernel/sched/walt/core_ctl.c#L1497-L1498)，即这两条规则
**默认关闭**，要靠 vendor init 脚本写 sysfs（`store_task_thres()` 还要求 `val >= num_cpus`
[core_ctl.c:164](../../kernel/kernel/sched/walt/core_ctl.c#L164)）。

### 4.9 `apply_limits()` 与 `min_cpus` / `max_cpus` / `enable` / `boost`

```c
/* apply_limits(), core_ctl.c:836-843 */
if (!cluster->enable)
	return cluster->num_cpus;
return min(max(cluster->min_cpus, need_cpus), cluster->max_cpus);
```

[反直觉] **`enable = 0` 的含义是"恢复全核"，不是"停止工作"**——core_ctl 是省核逻辑，
关掉它等价于放弃省核。`boost (>0)` 在 `eval_need()` 里走同一条"全开"短路。
两者默认值在 `cluster_init()` 里是 `1` 与 `num_cpus`
[core_ctl.c:1491-1492](../../kernel/kernel/sched/walt/core_ctl.c#L1491-L1492)，
sysfs 写入被 `min(val, num_cpus)` 夹住
[core_ctl.c:107](../../kernel/kernel/sched/walt/core_ctl.c#L107) /
[:126](../../kernel/kernel/sched/walt/core_ctl.c#L126)。

`try_to_pause()` 还有第二道保护：即使已挑好要 pause 的核，若
`active_cpus - nr_pending <= max_cpus` 也直接放弃
[core_ctl.c:1223](../../kernel/kernel/sched/walt/core_ctl.c#L1223)；另有一个 `again:` 回跳，
用于**在仍然超标时宁愿去 pause 忙核**
[core_ctl.c:1246-1249](../../kernel/kernel/sched/walt/core_ctl.c#L1246-L1249)。
即 `max_cpus` 是**硬上限**，`is_busy` 只是软约束。

### 4.10 `try_to_pause()` / `try_to_resume()`：选哪些核

`try_to_pause()` [core_ctl.c:1176](../../kernel/kernel/sched/walt/core_ctl.c#L1176)
按 `lru` 顺序挑核，跳过：`c->disabled`（即 `offline_disabled`，§3.5，第一个 CPU 永远在此）、
`!is_active(c)`（`is_active()` [core_ctl.c:870](../../kernel/kernel/sched/walt/core_ctl.c#L870)
要求 `cpu_active() && !cpu_halted()`）、`c->is_busy`；若 `nr_not_preferred_cpus > 0`，
非 not_preferred 的核也跳过
[core_ctl.c:1209](../../kernel/kernel/sched/walt/core_ctl.c#L1209)。

`not_preferred` 是一组**手工标注的"优先牺牲"核**，由 `store_not_preferred()`
[core_ctl.c:366](../../kernel/kernel/sched/walt/core_ctl.c#L366) 设置（掩码写法会跳过
`cpu_possible_mask` 里不存在的位）。典型用法是把功耗最差的核标出来优先关掉。

`try_to_resume()` [core_ctl.c:1297](../../kernel/kernel/sched/walt/core_ctl.c#L1297) 调
`__try_to_resume()` [core_ctl.c:1254](../../kernel/kernel/sched/walt/core_ctl.c#L1254)
**两轮**：第一轮只挑 not_preferred，数目不够时第二轮
`force_use_non_preferred = true` 才放开
[core_ctl.c:1316-1317](../../kernel/kernel/sched/walt/core_ctl.c#L1316-L1317)；且只恢复
`cpus_paused_by_us` 里的核 [core_ctl.c:1274](../../kernel/kernel/sched/walt/core_ctl.c#L1274)，
thermal 借走的不碰。

`do_core_ctl()` [core_ctl.c:1378](../../kernel/kernel/sched/walt/core_ctl.c#L1378)
把动作先**收集到两个 mask**，最后统一提交：

```c
/* do_core_ctl(), core_ctl.c:1397-1408 */
if (adjustment_possible(cluster, need) || adjustment_possible_32bit(cluster, need_32bit)) {
	if (cluster->active_cpus > need + need_32bit)
		try_to_pause(cluster, need, need_32bit, &cpus_to_pause);
	else if (cluster->active_cpus < need + need_32bit)
		try_to_resume(cluster, need, need_32bit, &cpus_to_unpause);
}
...
core_ctl_pause_cpus(&cpus_to_pause);
core_ctl_resume_cpus(&cpus_to_unpause);
```

**先统一 pause/resume，再更新 `active_cpus`**
[core_ctl.c:1413-1416](../../kernel/kernel/sched/walt/core_ctl.c#L1413-L1416)，
避免遍历中看到"半执行"状态。

### 4.11 `core_ctl_pause_cpus()` 的引用计数纪律

`walt_halt_cpus()` 会**修改入参 mask**（剔除已 halted 的核）。若 core_ctl 直接拿它当账本，
`cpus_paused_by_us` 就会与 `reason` 位图错位。所以两个包装都先存一份：

```c
/* core_ctl_pause_cpus(), core_ctl.c:1336-1345 */
cpumask_andnot(cpus_to_pause, cpus_to_pause, &cpus_paused_by_us);
cpumask_copy(&saved_cpus, cpus_to_pause);
if (walt_halt_cpus(cpus_to_pause, PAUSE_CORE_CTL) < 0)
	pr_debug(...);
else
	cpumask_or(&cpus_paused_by_us, &cpus_paused_by_us, &saved_cpus);
```

`core_ctl_resume_cpus()` [core_ctl.c:1360](../../kernel/kernel/sched/walt/core_ctl.c#L1360)
对称：先 `cpumask_and(..., &cpus_paused_by_us)` **只保留自己 pause 过的核**，
成功后 `cpumask_andnot` 清掉。函数上方的长注释
[core_ctl.c:1320-1330](../../kernel/kernel/sched/walt/core_ctl.c#L1320-L1330) 专门解释这一点——
它引用的是 `walt_pause.c`（旧文件名），与今天的 `walt_halt.c` 是同一套东西。

### 4.12 sysfs 接口

`cluster_init()` [core_ctl.c:1457](../../kernel/kernel/sched/walt/core_ctl.c#L1457)
把 kobject 挂在 **CPU device** 下 [core_ctl.c:1520-1521](../../kernel/kernel/sched/walt/core_ctl.c#L1520-L1521)，
所以每簇（以 `first_cpu` 为代表）一份，路径
`/sys/devices/system/cpu/cpu<first_cpu>/core_ctl/`：

| 属性 | 权限 | 说明 |
|---|---|---|
| `min_cpus` / `max_cpus` | rw | 上下限（§4.9） |
| `offline_delay_ms` | rw | 减核去抖，默认 100 |
| `busy_up_thres` / `busy_down_thres` | rw | 每核一个阈值；写 1 个值即广播给全簇 [store_busy_up_thres() core_ctl.c:199](../../kernel/kernel/sched/walt/core_ctl.c#L199) |
| `task_thres` / `nr_prev_assist_thresh` | rw | 默认 `UINT_MAX`，即关闭 |
| `not_preferred` | rw | 手工标注优先牺牲的核 |
| `enable` | rw | §4.9 |
| `need_cpus` / `active_cpus` | ro | 决策结果 |
| `global_state` | ro | **最有用**：逐核打印 Online / Paused / Busy% / Is busy / Nr running / Need CPUs… [show_global_state() core_ctl.c:312](../../kernel/kernel/sched/walt/core_ctl.c#L312) |

`global_state` 里 `Online:` 来自 `cpu_online()`
[core_ctl.c:332](../../kernel/kernel/sched/walt/core_ctl.c#L332)、`Paused:` 来自
`cpu_halted()` [core_ctl.c:335](../../kernel/kernel/sched/walt/core_ctl.c#L335)。
**两个字段并排打印，是"halt ≠ offline"最好用的自证**：halt 的核 `Online` 仍是 1。

### 4.13 boost 与 notifier

`core_ctl_set_boost()` [core_ctl.c:1045](../../kernel/kernel/sched/walt/core_ctl.c#L1045)
是**计数器**而非布尔：所有簇的 `boost` 一起 ++/--，`--` 时若已为 0 返回 `-EINVAL`；
状态变化时才 `apply_need()` 全部簇。调用者三处：heavy task isolation
[walt.c:3701](../../kernel/kernel/sched/walt/walt.c#L3701) /
[:3760](../../kernel/kernel/sched/walt/walt.c#L3760)、pipeline 任务出现/消失
[walt.c:3973](../../kernel/kernel/sched/walt/walt.c#L3973)、boost sysfs
[boost.c:98](../../kernel/kernel/sched/walt/boost.c#L98)。

`core_ctl_call_notifier()` [core_ctl.c:1097](../../kernel/kernel/sched/walt/core_ctl.c#L1097)
在每次 `core_ctl_check()` 末尾调用，**先探 notifier 链是否为空**（空则直接返回），
避免白算 `walt_fill_ta_data()`：

```c
/* core_ctl_call_notifier(), core_ctl.c:1113-1118 */
ndata.nr_big = last_nr_big;
walt_fill_ta_data(&ndata);            /* walt.c:4273 —— 填 coloc / ta / cap 百分比 */
atomic_notifier_call_chain(&core_ctl_notifier, 0, &ndata);
```

数据结构 `struct core_ctl_notif_data`
[include/linux/sched/walt.h:25](../../kernel/include/linux/sched/walt.h#L25)；
注册者是 msm_performance
[msm_performance.c:926](../../kernel/drivers/soc/qcom/msm_performance.c#L926)，
用途是把 top-app 负载报给用户态调参。

---

## 5. core_ctl 读到的 WALT 数据

### 5.1 `struct sched_avg_stats` 与"读并清零"

数组是**全局单份**：`struct sched_avg_stats stats[WALT_NR_CPUS]`
[sched_avg.c:37](../../kernel/kernel/sched/walt/sched_avg.c#L37)，
`core_ctl.c` 用全局指针 `nr_stats` 持有
[core_ctl.c:508](../../kernel/kernel/sched/walt/core_ctl.c#L508) /
[:760](../../kernel/kernel/sched/walt/core_ctl.c#L760) /
[:1532](../../kernel/kernel/sched/walt/core_ctl.c#L1532)。

```c
/* walt.h:370 */
struct sched_avg_stats {
	int nr;          /* 平均 nr_running * 100 */
	int nr_misfit;   /* 平均 misfit(big) 任务数 * 100 */
	int nr_max;      /* 窗口内见过的最大瞬时 nr_running */
	int nr_scaled;   /* 未除 100 的 nr，用于 little 簇的严格需求 */
};
```

`nr` 的计算有个易忽略的偏移
[sched_avg.c:106-111](../../kernel/kernel/sched/walt/sched_avg.c#L106-L111)：

```c
stats[cpu].nr = (int)div64_u64((tmp_nr + NR_THRESHOLD_PCT), 100);
```

`NR_THRESHOLD_PCT` = 40 [sched_avg.c:34](../../kernel/kernel/sched/walt/sched_avg.c#L34)，
即**加 0.4 再截断**——补偿"任务至少跑满 85% 窗口"的过估，效果是 `nr` 向上取整的门槛更低。

底层计数器 `per_cpu(nr, cpu)` 由 `sched_update_nr_prod()`
[sched_avg.c:271](../../kernel/kernel/sched/walt/sched_avg.c#L271) 维护，被 enqueue /
dequeue / misfit 变化三处调用
[walt.c:4654](../../kernel/kernel/sched/walt/walt.c#L4654) /
[:4708](../../kernel/kernel/sched/walt/walt.c#L4708) /
[:4752](../../kernel/kernel/sched/walt/walt.c#L4752)。它做的是**时间加权**
（`nr_running * diff`）而非简单采样——这正是 `div64_u64(tmp_nr * 100, period)` 的来历，
也是"窗口内统计是脏的"的根因。

### 5.2 `busy_pct` 来自 `sched_get_cpu_util_pct()`

```c
/* sched_get_cpu_util_pct(), sched_avg.c:300-316 */
util = wrq->prev_runnable_sum + wrq->grp_time.prev_runnable_sum;
util = scale_time_to_util(util);
util = (util >= capacity) ? capacity : util;
busy = div64_ul((util * 100), capacity);
```

**注意用的是 `prev_` 而不是 `curr_`**，即上一个窗口的利用率。这解释了
`core_ctl_check()` 为什么必须在窗口滚动时调用：刚滚完，`prev_runnable_sum`
才是刚结束那个完整窗口的值。

### 5.3 大任务的判据 [反直觉]

[data-structures.md §3](../00-overview/03-data-structures.md) 写的
"`nr_big_tasks` = `demand_scaled > 0.5 * max_task_load()` 的任务数" 在**这一版不成立**：

- `max_task_load()` [walt.h:728](../../kernel/kernel/sched/walt/walt.h#L728) 确实存在
  （返回 `sched_ravg_window`），但**全树零调用者**（`grep -rn "max_task_load"` 只命中定义本身），
  是死代码。
- `nr_big_tasks` 真正的驱动源是每任务的 `wts->misfit` 布尔量：`inc_rq_walt_stats()`
  [walt.c:4479](../../kernel/kernel/sched/walt/walt.c#L4479) / `dec_rq_walt_stats()`
  [walt.c:4492](../../kernel/kernel/sched/walt/walt.c#L4492) /
  `android_rvh_update_misfit_status()` [walt.c:4755](../../kernel/kernel/sched/walt/walt.c#L4755)
  调 `adjust_misfit_task_accounting()`
  [walt.c:4442](../../kernel/kernel/sched/walt/walt.c#L4442)，后者按 `is_compat_thread()`
  分别记进 `nr_big_tasks` 或 `nr_32bit_big_tasks`。
- `wts->misfit` 的判据是 `!task_fits_max(p, rq->cpu)`
  [walt.c:4743-4746](../../kernel/kernel/sched/walt/walt.c#L4743-L4746) 配
  `task_fits_max()` [walt.h:810](../../kernel/kernel/sched/walt/walt.h#L810)——即
  **"考虑 boost / uclamp / margin 之后放不进当前容量点"**。

所以新核上 big task 从"需求绝对值超过半个窗口"变成**"相对当前容量放不下"**，
`capacity_orig_of()` 与 margin 都参与——这意味着 `nr_big_tasks` 是**依赖 CPU** 的，
同一任务在不同核上判定可能不同。两个便捷读取函数：

```c
/* walt.c:515 / walt.c:522 */
walt_big_tasks(cpu)       = nr_big_tasks + nr_32bit_big_tasks;
walt_big_64bit_tasks(cpu) = nr_big_tasks;
```

但 `core_ctl` **不直接读这两个函数**——它读 `nr_stats[].nr_misfit`
（经 `sched_get_nr_running_avg()` 窗口平均），见
`compute_prev_cluster_misfit_need()` [core_ctl.c:592](../../kernel/kernel/sched/walt/core_ctl.c#L592)。

### 5.4 `last_nr_big` 与 big-task rotation

`cluster_real_big_tasks()` [core_ctl.c:609](../../kernel/kernel/sched/walt/core_ctl.c#L609)
把各簇的"真·大任务"数加总成 `big_avg`，写进 static `last_nr_big`
[core_ctl.c:91](../../kernel/kernel/sched/walt/core_ctl.c#L91) /
[:791](../../kernel/kernel/sched/walt/core_ctl.c#L791)，用途两个：

1. 经 notifier 报给 msm_performance（§4.13）；
2. 喂给 `walt_rotation_checkpoint(big_avg)`
   [core_ctl.c:792](../../kernel/kernel/sched/walt/core_ctl.c#L792) →
   [walt.c:4260](../../kernel/kernel/sched/walt/walt.c#L4260)：

```c
/* walt_rotation_checkpoint(), walt.c:4265-4270 */
if (!sysctl_sched_walt_rotate_big_tasks || sched_boost_type != NO_BOOST) {
	walt_rotation_enabled = 0;
	return;
}
walt_rotation_enabled = nr_big >= num_possible_cpus();
```

即**大任务多到"每个核上都有一个"时开启 big task rotation**，此后 LB 会主动让这些任务
在簇间轮转、轮流享受大核（`walt_lb.c` 里所有 `walt_rotation_enabled` 分支，如
[walt_lb.c:670](../../kernel/kernel/sched/walt/walt_lb.c#L670)）。

[反直觉] 这是一条 **core_ctl → LB 的反向依赖**：core_ctl 是核数的消费者，
却又是 rotation 的开关提供者；两者在同一个 `walt_irq_work` 调用里、`core_ctl_check()`
内部完成交接。

### 5.5 `core_ctl_check` 的完整调用链

```
窗口滚动（tick 路径，每窗口仅一次）
 └─ run_walt_irq_work_rollover()              walt.c:2271
      └─ atomic64_cmpxchg(...)                walt.c:2279   ← 全局去重
           └─ irq_work_queue()（硬中断上下文）
                └─ walt_irq_work()            walt.c:4213
                     ├─ __walt_irq_work_locked()          walt.c:3992
                     └─ if (!is_migration) core_ctl_check(wrq->window_start)
                                                          walt.c:4256
                          └─ core_ctl_check()             core_ctl.c:1129
                               ├─ 同窗口去重 (core_ctl_check_timestamp)
                               ├─ sched_get_cpu_util_pct() → busy_pct
                               ├─ update_running_avg() → sched_get_nr_running_avg()
                               │    → 六个 compute_* 函数 → walt_rotation_checkpoint()
                               ├─ eval_need() / eval_need_32bit() × 每簇
                               ├─ do_core_ctl()
                               │    ├─ try_to_pause()/try_to_resume() → 两个 mask
                               │    ├─ core_ctl_pause_cpus()  → walt_halt_cpus()  → cpu_halt_mask
                               │    └─ core_ctl_resume_cpus() → walt_start_cpus() → 清 mask + newidle IPI
                               └─ core_ctl_call_notifier() → walt_fill_ta_data()
                               （walt_halt 的 drain kthread 随后异步搬走任务）
```

---

## 6. 一个可疑之处 [待确认]

`eval_need_32bit()` [core_ctl.c:1012](../../kernel/kernel/sched/walt/core_ctl.c#L1012)
在 `adj_now` 分支里写的是 **`cluster->need_cpus`**，而不是它自己读的
`last_need = cluster->last_need_32bit_cpus`
[core_ctl.c:983](../../kernel/kernel/sched/walt/core_ctl.c#L983)：

```c
/* eval_need_32bit(), core_ctl.c:983-1013 */
last_need = cluster->last_need_32bit_cpus;
need_cpus = cluster->need_32bit_cpus + cluster->nr_prev_assist_32bit;
...
if (adj_now) {
	adj_possible = adjustment_possible_32bit(cluster, new_need);
	cluster->need_ts = now;
	cluster->need_cpus = new_need;      /* ← 写的是 64 位需求字段 */
}
```

对比 `eval_need()` 的同一位置是 `cluster->need_cpus = new_need;`（那里是对的）
[core_ctl.c:950](../../kernel/kernel/sched/walt/core_ctl.c#L950)。
这里**读 `last_need_32bit_cpus` 却写 `need_cpus`**，字段不对称，看着像复制粘贴漏改；
后果是 `do_core_ctl()` 里 `need = apply_limits(cluster, cluster->need_cpus)`
[core_ctl.c:1390](../../kernel/kernel/sched/walt/core_ctl.c#L1390) 可能被 32 位需求污染。
需要实验确认是否真有可见影响：**观测 32 位大任务活跃时 `core_ctl/need_cpus` 是否异常跳变**。

---

## 7. 观测手段

| 手段 | 位置 | 看什么 |
|---|---|---|
| `core_ctl/global_state` | sysfs | Online vs Paused 的差异（§4.12） |
| `core_ctl_eval_need` | [trace.h:484](../../kernel/kernel/sched/walt/trace.h#L484) | `last_need` / `new_need` / `adj_now` / `adj_possible` |
| `core_ctl_update_nr_need` | [trace.h:589](../../kernel/kernel/sched/walt/trace.h#L589) | `nr_need` / `prev_misfit_need` / `nrrun` / `max_nr` / `nr_prev_assist` |
| `core_ctl_set_busy` | [trace.h:550](../../kernel/kernel/sched/walt/trace.h#L550) | 每核 `is_busy` 翻转 + busy_pct |
| `core_ctl_set_boost` | [trace.h:574](../../kernel/kernel/sched/walt/trace.h#L574) | boost 计数变化，可定位"谁把核全开了" |
| `core_ctl_notif_data` | [trace.h:619](../../kernel/kernel/sched/walt/trace.h#L619) | 报给 msm_performance 的 nr_big / coloc / ta_util |
| `halt_cpus` | [trace.h:1437](../../kernel/kernel/sched/walt/trace.h#L1437) | `req_cpus` / `halt_cpus` / 耗时(µs) / success |
| `sched_get_nr_running_avg` | [trace.h:649](../../kernel/kernel/sched/walt/trace.h#L649) | `nr` / `nr_misfit` / `nr_max` / `nr_scaled` |

`halt_cpus` 的 `time` 字段（`div64_u64(sched_clock() - start_time, 1000)`
[trace.h:1453](../../kernel/kernel/sched/walt/trace.h#L1453)）只覆盖
`halt_cpus()/start_cpus()` 本身（置 mask + 唤醒），**不含 drain 时间**——drain 是异步的，
要看它对调度的影响得结合 `walt_newidle_balance` / `sched_switch` 一起分析。[推测]

---

## 8. 遗留问题

1. **[待确认]** `eval_need_32bit()` 写错字段（§6），疑为上游 bug。
2. **[待确认]** `sched_avg.c` 中 `nr_big_prod_sum` 的累加用 `walt_big_tasks()`（含 32 位）
   [sched_avg.c:293](../../kernel/kernel/sched/walt/sched_avg.c#L293)，
   而收尾补算用 `walt_big_64bit_tasks()`（不含 32 位）
   [sched_avg.c:98](../../kernel/kernel/sched/walt/sched_avg.c#L98)。两者不对称，
   32 位任务活跃时 `nr_misfit` 可能有偏差。
3. **[待确认]** `core_ctl.c` 与 `walt_halt.c` 都**没有**检查 `is_reserved()`
   （hyp_core_ctl 保留的核），而 `walt_lb.c` 有
   [walt_lb.c:841](../../kernel/kernel/sched/walt/walt_lb.c#L841)。
   core_ctl 是否会去 halt 一个 hyp 保留核，需实验确认。
4. **[已修订]** [data-structures.md §3](../00-overview/03-data-structures.md) 中
   `nr_big_tasks` 的括注对应更早的内核；本版判据见 §5.3。
   **2026-09-17 已同步修正该文档**，并追加了 `max_task_load()` 为死代码的说明。
5. **[推测]** arm64 上 `CONFIG_HOTPLUG_CPU` 默认 y（`arch/arm64/configs/` 下无显式设置）。
   若关闭，`walt_halt.c` 整个为空，`__cpu_halt_mask` 未定义会导致链接失败。
6. **[已登记]** 上述第 1–3 条 `[待确认]` 已录入
   [04-open-questions.md](../03-comparison/04-open-questions.md)
   （Q-03 `eval_need_32bit()` 字段错写、`nr_big_prod_sum` 不对称、`is_reserved()` 缺失）。

---

## 相关文档

- [04-data-flow.md](../00-overview/04-data-flow.md)——`core_ctl_check` 调用点的时序位置
- [03-data-structures.md](../00-overview/03-data-structures.md)——`walt_sched_stats`、`walt_rq` 字段含义
- [02-integration-model.md](../00-overview/02-integration-model.md)——walt_halt 的 4 个 hook 对照表
- [window-model.md](01-window-model.md)——窗口滚动的触发与"统计只在滚动后稳定"的根因
- [demand-prediction.md](02-demand-prediction.md)——同源的窗口统计
- [03-schedutil.md](../01-baseline/03-schedutil.md)——主线调频侧如何做类似的"忙度"判断
- [02-pelt.md](../01-baseline/02-pelt.md)——与 `sched_get_cpu_util_pct()` 的口径对比
- [04-open-questions.md](../03-comparison/04-open-questions.md)——待确认问题登记（本文件 §8 的条目已录入）
