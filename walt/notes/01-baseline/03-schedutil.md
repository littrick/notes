# schedutil 调频框架（baseline）

> **源码**：[cpufreq_schedutil.c](../../kernel/kernel/sched/cpufreq_schedutil.c)、[core.c](../../kernel/kernel/sched/core.c)、[cpufreq.h](../../kernel/include/linux/sched/cpufreq.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本篇是 baseline 系列第二篇。WALT 提供的是**另一个 governor**
（`cpufreq_walt.c`，见 [../02-walt/cpufreq.md](../02-walt/03-cpufreq.md)），
但两者共用同一套内核框架：`effective_cpu_util()` 与
`cpufreq_update_util_data` 回调注册。**不理解这套框架就读不懂 WALT 调频的相对位置。**

---

## 1. 数据流总览

```
调度器事件（enqueue/dequeue/RT/DL 变化）
   └─ cpufreq_update_util(rq, flags)          sched.h:2896
        └─ 取出 per-CPU 回调 data->func
             └─ sugov_update_single_freq() / sugov_update_shared() ...
                  ├─ sugov_get_util()   ← effective_cpu_util()
                  ├─ sugov_iowait_apply()
                  └─ get_next_freq()    ← map_util_perf + map_util_freq
                       └─ cpufreq_driver_fast_switch()   或
                          sugov_deferred_update() → irq_work → kthread
```

> **WALT 的替换点**：WALT **不**替换 `data->func`，而是注册自己的 `waltgov`
> 回调，由 `walt_irq_work` 在窗口滚动时调用。它**完全绕开**下面这条
> schedutil 的 util 链：`waltgov` 用 `cpu_util_freq_walt()`
> [walt.c:680](../../kernel/kernel/sched/walt/walt.c#L680) 自己算 util，
> **不调用** `effective_cpu_util()`。
> 见 [../02-walt/03-cpufreq.md](../02-walt/03-cpufreq.md) 与
> [../02-walt/11-freq-pipeline.md](../02-walt/11-freq-pipeline.md)。

---

## 2. 回调注册机制

| 函数 | 位置 | 作用 |
|---|---|---|
| per-CPU 回调指针 | [cpufreq.c:12](../../kernel/kernel/sched/cpufreq.c#L12) | `cpufreq_update_util_data` |
| `cpufreq_add_update_util_hook()` | [cpufreq.c:33](../../kernel/kernel/sched/cpufreq.c#L33) | `rcu_assign_pointer` 安装回调 |
| `cpufreq_remove_update_util_hook()` | [cpufreq.c:58](../../kernel/kernel/sched/cpufreq.c#L58) | 清空指针 |
| `cpufreq_this_cpu_can_update()` | [cpufreq.c:73](../../kernel/kernel/sched/cpufreq.c#L73) | 是否允许本 CPU 发起更新 |

### 2.1 调度器侧的调用点

[sched.h:2896-2904](../../kernel/kernel/sched/sched.h#L2896-L2904)：

```c
static inline void cpufreq_update_util(struct rq *rq, unsigned int flags)
{
        struct update_util_data *data;

        data = rcu_dereference_sched(*per_cpu_ptr(&cpufreq_update_util_data,
                                                  cpu_of(rq)));
        if (data)
                data->func(data, rq_clock(rq), flags);
}
```

**在 `rq->lock` 保护下执行**（RCU-sched 读侧）——这个约束很重要，
它使得回调里的许多操作不需要额外加锁，也使得回调**必须短**。

### 2.2 全部调度器调用点

| 位置 | 触发场景 |
|---|---|
| [fair.c:3285](../../kernel/kernel/sched/fair.c#L3285) | `cfs_rq_util_change()`，CFS util 变化 |
| [fair.c:5836](../../kernel/kernel/sched/fair.c#L5836) | enqueue/唤醒路径，带 `SCHED_CPUFREQ_IOWAIT` |
| [fair.c:8650](../../kernel/kernel/sched/fair.c#L8650) | `_nohz_idle_balance()` 阻塞负载衰减 |
| [rt.c:572](../../kernel/kernel/sched/rt.c#L572) | `dequeue_top_rt_rq()` |
| [rt.c:1100](../../kernel/kernel/sched/rt.c#L1100) | RT 入队 |
| [deadline.c:167](../../kernel/kernel/sched/deadline.c#L167) | `__add_running_bw()` |
| [deadline.c:181](../../kernel/kernel/sched/deadline.c#L181) | `__sub_running_bw()` |

`SCHED_CPUFREQ_IOWAIT` 定义在
[cpufreq.h:11](../../kernel/include/linux/sched/cpufreq.h#L11)（`1U << 0`）。

---

## 3. `effective_cpu_util()`：baseline 的 util 汇总点

[core.c:7320](../../kernel/kernel/sched/core.c#L7320)。**这是 schedutil 与 EAS
共用的计算核心**。

> **它与 WALT 没有交接面。** 本节描述的是 baseline 的计算链；
> WALT 的 `walt` governor **不经过这里**，而是调用自己的
> `cpu_util_freq_walt()`。两者是**平行**的两套 util 计算，
> 不是「一套替换另一套」。详见 §3.4。

函数签名（声明于 [sched.h:3094](../../kernel/kernel/sched/sched.h#L3094)）：

```c
unsigned long effective_cpu_util(int cpu, unsigned long util_cfs,
                                 unsigned long max, enum cpu_util_type type,
                                 struct task_struct *p);
```

`enum cpu_util_type { FREQUENCY_UTIL, ENERGY_UTIL }`
[sched.h:3089](../../kernel/kernel/sched/sched.h#L3089)。

### 3.1 计算顺序（八步）

```
1. 【vendor hook 短路点，本树未生效】android_rvh_effective_cpu_util  :7328-7330
     trace_android_rvh_effective_cpu_util(cpu, util_cfs, max, type, p, &new_util);
     if (new_util != ULONG_MAX) return new_util;      ← 无注册者，永不触发

2. RT 饱和快速路径                                          :7332-7335
     if (!uclamp_is_used() && type == FREQUENCY_UTIL && rt_rq_is_runnable(&rq->rt))
             return max;

3. IRQ 饱和检查                                             :7342-7344
     irq = cpu_util_irq(rq);
     if (unlikely(irq >= max)) return max;

4. CFS + RT 相加                                            :7358
     util = util_cfs + cpu_util_rt(rq);
     if (type == FREQUENCY_UTIL) util = uclamp_rq_util_with(rq, util, p);  :7359

5. DL 饱和检查                                              :7362-7374
     dl_util = cpu_util_dl(rq);
     if (util + dl_util >= max) return max;
     if (type == ENERGY_UTIL) util += dl_util;               :7380

6. IRQ/steal 时间缩放                                       :7392-7393
     util = scale_irq_capacity(util, irq, max);   /* U' = (max-irq)/max * U */
     util += irq;

7. DL 带宽补偿（仅调频）                                     :7405-7406
     if (type == FREQUENCY_UTIL) util += cpu_bw_dl(rq);

8. return min(max, util);                                   :7408
```

### 3.2 第 1 步：一个未生效的 vendor hook 短路点 [关键]

```c
unsigned long new_util = ULONG_MAX;

trace_android_rvh_effective_cpu_util(cpu, util_cfs, max, type, p, &new_util);
if (new_util != ULONG_MAX)
        return new_util;
```

**哨兵值 `ULONG_MAX` 表示「没有 vendor 接管」**。这是一个干净的
「改写输出变量 + 原生提前返回」式接管点——**如果**有人注册它的话。

> **【反直觉】本树中没有任何代码注册这个 hook。** 全仓库搜索
> `register_trace_android_rvh_effective_cpu_util` 结果为空。
> 该 hook 的状态是「**已声明、已导出、已调用、未注册**」：
>
> | 环节 | 位置 | 是否存在 |
> |---|---|---|
> | DECLARE | `kernel/include/trace/hooks/sched.h:402` | ✅ |
> | EXPORT | [vendor_hooks.c:121](../../kernel/kernel/sched/vendor_hooks.c#L121) | ✅ |
> | CALL | [core.c:7328](../../kernel/kernel/sched/core.c#L7328) | ✅ |
> | REGISTER | —— | ❌ **无** |
>
> 因此 `new_util` 恒为 `ULONG_MAX`，**第 2-8 步全部正常执行**，
> `effective_cpu_util()` 的函数体是**活代码**。
> 读这个函数时应当假设它在完整跑。

`[待确认]`：符号是 `EXPORT_TRACEPOINT_SYMBOL_GPL` 导出的，
一个**树外** vendor 模块（如各家的 perf/boost 模块）仍可在运行时注册它，
届时短路会生效。所以它是「设计上存在、本树未启用」的接管点。
已登记于 [04-open-questions.md](../03-comparison/04-open-questions.md)。

### 3.3 WALT 到底怎么拿到 util 的

WALT **不使用**上面这个 hook，而是走自己的路：
`waltgov_get_util()` [cpufreq_walt.c:276](../../kernel/kernel/sched/walt/cpufreq_walt.c#L276)
调用 `cpu_util_freq_walt()`。该函数在**全树只有一个调用者**：

```
cpu_util_freq_walt()                        walt.c:680
   ↑ 唯一调用者
waltgov_get_util()                          cpufreq_walt.c:284
   ↑
waltgov_update_freq()                       cpufreq_walt.c:414
```

所以正确的描述是：**WALT 与 `effective_cpu_util()` 是两条平行的 util 链**，
而不是「WALT 把 `effective_cpu_util()` 短路掉了」。

WALT 侧的 uclamp 也确实独立实现，但位置在 **governor 里**而非
`effective_cpu_util()` 内部：
`waltgov_get_util` 最后一行 `uclamp_rq_util_with(rq, util, NULL)`
[cpufreq_walt.c:285](../../kernel/kernel/sched/walt/cpufreq_walt.c#L285)。
这是个**真实差异**，只是原因不是「短路」。

### 3.4 辅助 util 函数

全部是 [sched.h](../../kernel/kernel/sched/sched.h) 里的内联函数：

| 函数 | 位置 | 计算 |
|---|---|---|
| `cpu_util_cfs(rq)` | :3108 | `max(rq->cfs.avg.util_avg, util_est.enqueued)`（`UTIL_EST` 开启时）|
| `cpu_util_rt(rq)` | :3120 | `rq->avg_rt.util_avg` |
| `cpu_util_dl(rq)` | :3103 | `rq->avg_dl.util_avg` |
| `cpu_bw_dl(rq)` | :3098 | `(rq->dl.running_bw * 1024) >> BW_SHIFT` |
| `cpu_util_irq(rq)` | :3127 | `rq->avg_irq.util_avg`（`CONFIG_HAVE_SCHED_AVG_IRQ`）|
| `scale_irq_capacity()` | :3132 | `util * (max - irq) / max` |
| `uclamp_rq_util_with()` | :2946 | 按 UCLAMP_MIN/MAX 夹取 |
| `uclamp_is_used()` | :2995 | `static_branch_likely(&sched_uclamp_used)` |

> **`cpu_util_cfs` 与 WALT 的对照**：WALT 的 `__cpu_util_freq_walt()`
> [walt.c:638](../../kernel/kernel/sched/walt/walt.c#L638) 做的是同一件事
> （把「忙时」变成 0..1024 的 util），但**数据源是窗口模型的计数器**
> 而非 PELT 的 `util_avg`。这是整个 WALT 替换的最核心一行。

`cpu_bw_dl()` 用的是 DL 的**预留带宽**，`cpu_util_dl()` 用的是**实测 util**。
两者不同：前者是保证，后者是实际消耗。

---

## 4. `sugov_get_util()`

[cpufreq_schedutil.c:201](../../kernel/kernel/sched/cpufreq_schedutil.c#L201)：

```c
static void sugov_get_util(struct sugov_cpu *sg_cpu)
{
        struct rq *rq = cpu_rq(sg_cpu->cpu);
        unsigned long max = arch_scale_cpu_capacity(sg_cpu->cpu);

        sg_cpu->max = max;
        sg_cpu->bw_dl = cpu_bw_dl(rq);
        sg_cpu->util = effective_cpu_util(sg_cpu->cpu, cpu_util_cfs(rq), max,
                                          FREQUENCY_UTIL, NULL);
}
```

注意 `p` 传 **NULL**——即调频时不知道「是谁」在跑，uclamp 只能按
**rq 聚合值**处理，无法按任务夹取。

---

## 5. 频率映射：`map_util_perf` + `map_util_freq`

### 5.1 两个内联函数

[cpufreq.h:26-35](../../kernel/include/linux/sched/cpufreq.h#L26-L35)：

```c
static inline unsigned long map_util_freq(unsigned long util,
                                        unsigned long freq, unsigned long cap)
{
        return freq * util / cap;
}

static inline unsigned long map_util_perf(unsigned long util)
{
        return util + (util >> 2);
}
```

> **【修正】本内核树没有 `sched_util_freq_margin`。**
> 1.25 倍的余量由 `map_util_perf()` 实现——**是在 `util` 上加 25%**，
> 不是「加一个常数余量」。已 grep 全树确认该符号不存在
> （它是后续内核版本才引入的）。

### 5.2 `get_next_freq()`

[cpufreq_schedutil.c:177-199](../../kernel/kernel/sched/cpufreq_schedutil.c#L177-L199)：

```c
unsigned int freq = arch_scale_freq_invariant() ?
                        policy->cpuinfo.max_freq : policy->cur;
unsigned long next_freq = 0;

util = map_util_perf(util);                                     /* ← 1.25× */
trace_android_vh_map_util_freq(util, freq, max, &next_freq);
trace_android_vh_map_util_freq_new(util, freq, max, &next_freq, policy,
                &sg_policy->need_freq_update);
if (next_freq)
        freq = next_freq;                                       /* vendor 覆盖 */
else
        freq = map_util_freq(util, freq, max);

if (freq == sg_policy->cached_raw_freq && !sg_policy->need_freq_update)
        return sg_policy->next_freq;                            /* 缓存短路 */

sg_policy->cached_raw_freq = freq;
return cpufreq_driver_resolve_freq(policy, freq);
```

**有效公式**：

```
next_freq = base_freq × (util + util/4) / max
          = base_freq × 1.25 × util / max
```

`base_freq` 取 `cpuinfo.max_freq`（若架构频率不变，即 `arch_scale_freq_invariant()`
为真）否则取 `policy->cur`。

> 【反直觉】为什么基准频率要分两种？如果架构的 `util` 已经做了频率归一化
> （invariant），那么 `util/max` 就是个**比率**，直接乘以 `max_freq` 即可；
> 否则 `util` 是绝对量，只能相对**当前**频率缩放。

`cpufreq_driver_resolve_freq()` 把目标频率规整到硬件支持的 OPP，
并套用 policy 的 min/max 限制。

### 5.3 与 WALT 的关系

WALT 的 governor 有自己的映射路径（`waltgov_walt_adjust()`），
**不调用 `get_next_freq()`**。两者只在 `effective_cpu_util()`
这一层共享（且被 WALT 用 hook 短路）。详见
[../03-comparison/cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md)。

---

## 6. 三种 update 回调

`sugov_start()` [cpufreq_schedutil.c:791](../../kernel/kernel/sched/cpufreq_schedutil.c#L791)
按 policy 类型选回调（:814-819）：

```c
if (policy_is_shared(policy))
        uu = sugov_update_shared;
else if (policy->fast_switch_enabled && cpufreq_driver_has_adjust_perf())
        uu = sugov_update_single_perf;
else
        uu = sugov_update_single_freq;
```

| 回调 | 位置 | 场景 |
|---|---|---|
| `sugov_update_single_freq()` | [:374](../../kernel/kernel/sched/cpufreq_schedutil.c#L374) | 单 CPU policy，设频率 |
| `sugov_update_single_perf()` | [:414](../../kernel/kernel/sched/cpufreq_schedutil.c#L414) | 单 CPU policy，硬件 `adjust_perf` |
| `sugov_update_shared()` | [:472](../../kernel/kernel/sched/cpufreq_schedutil.c#L472) | 多 CPU 共享 policy |

### 6.1 `sugov_update_shared()` 的取值方式

`policy_is_shared()` 为真时，`sugov_next_freq_shared()`
[:446](../../kernel/kernel/sched/cpufreq_schedutil.c#L446) 遍历 `policy->cpus`，
对每个 CPU 调 `sugov_get_util()` + `sugov_iowait_apply()`，
**取 `util/max` 比值最大的那个 CPU** 作为整簇的目标。

> **这正是 WALT 要替代的地方**。WALT 的 `freq_policy_load()` 用
> `WALT_CPUFREQ_CONTINUE` 标志做跨 CPU 聚合，把负载**相加**（而非取最大），
> 因为它把「任务需求」而非「CPU 占用率」当作输入。
> 见 [../02-walt/cpufreq.md](../02-walt/03-cpufreq.md)。

### 6.2 慢路径：irq_work + kthread

```
sugov_deferred_update()   [:147]  → irq_work_queue()
   └─ sugov_irq_work()    [:526]  → kthread_queue_work()
        └─ sugov_work()   [:500]  → __cpufreq_driver_target()
```

kthread 是 **SCHED_DEADLINE** 任务，名字 `sugov:%d`，
带宽 1ms runtime / 10ms period（[:626-628](../../kernel/kernel/sched/cpufreq_schedutil.c#L626-L628)）。

> 【反直觉】调频线程本身是 DL 任务——它会**抢占 CFS**，
> 且它的 `running_bw` 会反过来影响 `effective_cpu_util()` 的第 5 步。
> 高速切换（fast switch）路径则无此开销，直接在当前上下文里写寄存器。

---

## 7. iowait boost

常量 `IOWAIT_BOOST_MIN = SCHED_CAPACITY_SCALE / 8 = 128`
[cpufreq_schedutil.c:17](../../kernel/kernel/sched/cpufreq_schedutil.c#L17)。

| 函数 | 位置 | 行为 |
|---|---|---|
| `sugov_iowait_reset()` | [:223](../../kernel/kernel/sched/cpufreq_schedutil.c#L223) | 空闲 ≥ 1 tick 则重置 |
| `sugov_iowait_boost()` | [:252](../../kernel/kernel/sched/cpufreq_schedutil.c#L252) | **翻倍**，上限 1024 |
| `sugov_iowait_apply()` | [:299](../../kernel/kernel/sched/cpufreq_schedutil.c#L299) | **减半**，低于 128 归零 |

行为总结：

- 起始 **128**
- 每个 tick 内的连续 iowait 唤醒**翻倍**（`iowait_boost_pending` 保证
  每个请求最多翻一次），上限 **1024**
- 每次非 iowait 更新**减半**
- CPU 空闲满一个 tick → 重置

最后一步把 boost 从 0..1024 换算到本 CPU 容量尺度
（[:328](../../kernel/kernel/sched/cpufreq_schedutil.c#L328)）：

```c
boost = (iowait_boost * sg_cpu->max) >> SCHED_CAPACITY_SHIFT;
boost = uclamp_rq_util_with(cpu_rq(cpu), boost, NULL);
if (sg_cpu->util < boost)
        sg_cpu->util = boost;
```

> **WALT 侧**：WALT 有独立的 `wrq->iowait_boost` 逻辑
> （`sysctl_sched_iowait_boost_enable` 等）。两者**不会同时生效**，
> 但原因不是「短路」——`sugov_iowait_apply()` 是 **schedutil 的回调**
> 才会走的代码，而 WALT 用的是另一套回调（`waltgov_callback`），
> 压根不会调用它。这是**两条 governor 实现互斥**，与 util 计算无关。
> 见 [../02-walt/03-cpufreq.md](../02-walt/03-cpufreq.md)、[08-boost.md](../02-walt/08-boost.md)。

### 7.1 本树**没有**的 tunable

已 grep 全树确认为**不存在**（是后续 Android 内核才加的）：

| 符号 | 状态 |
|---|---|
| `sched_util_freq_margin` | 不存在——1.25× 由 `map_util_perf()` 实现 |
| `iowait_boost_enable` | 不存在——iowait boost 无条件启用 |
| `sugov_iowait_boost_max` | 不存在——上限硬编码 1024 |
| `sugov_rate_limit_us` | 不存在——tunable 名是 `rate_limit_us` |

---

## 8. 限速与 tunable

### 8.1 `sugov_should_update_freq()`

[cpufreq_schedutil.c:71](../../kernel/kernel/sched/cpufreq_schedutil.c#L71)：

```c
if (!cpufreq_this_cpu_can_update(sg_policy->policy))
        return false;                                  /* :90 拒绝远程 CPU */
if (READ_ONCE(sg_policy->limits_changed)) {            /* :93 限制变更强制更新 */
        sg_policy->limits_changed = false;
        sg_policy->need_freq_update = true;
        smp_mb();
        return true;
}
delta_ns = time - sg_policy->last_freq_update_time;
return delta_ns >= sg_policy->freq_update_delay_ns;    /* :110 速率限制 */
```

`ignore_dl_rate_limit()` [:351](../../kernel/kernel/sched/cpufreq_schedutil.c#L351)：
DL 带宽增长时置 `limits_changed` 以**绕过限速**（DL 增带宽是紧急事件）。

### 8.2 唯一的 tunable：`rate_limit_us`

[cpufreq_schedutil.c:570](../../kernel/kernel/sched/cpufreq_schedutil.c#L570)。
唯一的 sysfs governor 属性，默认值来自 `cpufreq_policy_transition_delay_us(policy)`。

写入时对所有 policy 生效（[:562-565](../../kernel/kernel/sched/cpufreq_schedutil.c#L562-L565)）：

```c
tunables->rate_limit_us = rate_limit_us;
list_for_each_entry(sg_policy, &attr_set->policy_list, tunables_hook)
        sg_policy->freq_update_delay_ns = rate_limit_us * NSEC_PER_USEC;
```

---

## 9. Android vendor hook 接入点

schedutil 路径上预留的 hook，以及它们在**本树中是否真的被使用**：

| hook | 位置 | 能力 | 本树注册者 |
|---|---|---|---|
| `android_rvh_effective_cpu_util` | [core.c:7328](../../kernel/kernel/sched/core.c#L7328) | 完全替换 util | **无** ❌ |
| `android_vh_map_util_freq` | [cpufreq_schedutil.c:186](../../kernel/kernel/sched/cpufreq_schedutil.c#L186) | 覆盖目标频率 | 见下 |
| `android_vh_map_util_freq_new` | [:187](../../kernel/kernel/sched/cpufreq_schedutil.c#L187) | 同上 + 可设 `need_freq_update` | 见下 |
| `android_rvh_set_sugov_update` | [:137](../../kernel/kernel/sched/cpufreq_schedutil.c#L137) | 否决一次更新 | 见下 |
| `android_rvh_set_iowait` | [fair.c:5833](../../kernel/kernel/sched/fair.c#L5833) | 覆盖 iowait 判定 | 见下 |
| `android_vh_set_sugov_sched_attr` | [:637](../../kernel/kernel/sched/cpufreq_schedutil.c#L637) | 改 sugov DL 线程属性 | 见下 |

> **WALT 用这些 hook 吗？** 完整的注册清单见
> [integration-model.md §5](../00-overview/02-integration-model.md#5-完整-hook-表)。
>
> - **util 侧：不用。** `android_rvh_effective_cpu_util` 全树无注册者，
>   WALT 用 `cpu_util_freq_walt()` 另起一条平行链（见 §3.2、§3.3）。
> - **频率映射侧：不用。** WALT 有自己的 governor，
>   频率在 `walt_map_util_freq()` 里算，不经过 `map_util_freq()`。
>
> 这两个「不用」合起来说明：**WALT 对调频的接管是结构性的**
> （换掉整个 governor），而不是靠 hook 修补 schedutil。

---

## 10. 相关文档

- WALT 的 governor → [../02-walt/cpufreq.md](../02-walt/03-cpufreq.md)
- 两者的逐函数差异 → [../03-comparison/cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md)
- PELT（`util_avg` 的来源）→ [pelt.md](02-pelt.md)
- 调度框架 → [sched-framework.md](01-sched-framework.md)
