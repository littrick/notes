# 调频逐函数差异：schedutil vs WALT

> **最后核对**：2026-09-17
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **基线**：[03-schedutil.md](../01-baseline/03-schedutil.md)
> **WALT 侧**：[03-cpufreq.md](../02-walt/03-cpufreq.md)
> **对照表**（更高层）→ [base-vs-walt.md](01-base-vs-walt.md)

本文按**调用链顺序**逐函数对照。每节格式：
`基线函数` → `WALT 函数` → **差异要点**。

---

## 0. 总览：一条链 vs 两条链

```
【基线 schedutil】
调度事件 → cpufreq_update_util()             sched.h:2896
             → sugov_update_single_freq()      cpufreq_schedutil.c:374
             → sugov_get_util()                :201
                  └─ effective_cpu_util()      core.c:7320
             → sugov_iowait_boost/apply()      :252 / :299
             → get_next_freq()                 :177
                  ├─ map_util_perf()           cpufreq.h:32
                  └─ map_util_freq()           cpufreq.h:26
             → sugov_update_next_freq()        :115
                  └─ sugov_should_update_freq() :71

【WALT】
窗口滚动 → walt_irq_work()
             → waltgov_run_callback()          walt.h:402
             → waltgov_update_freq()           cpufreq_walt.c:403
             → waltgov_get_util()              :276
                  ├─ cpu_util_freq_walt()      walt.c:680   ← 跨文件！
                  │    └─ freq_policy_load()   walt.c:590
                  └─ uclamp_rq_util_with()
             → waltgov_next_freq_shared()      :364
                  ├─ per CPU: waltgov_walt_adjust()  :305   ← 6 个 boost 源
                  └─ get_next_freq()           :237
                       └─ walt_map_util_freq() :209
             → waltgov_update_next_freq()      :124
                  └─ waltgov_up_down_rate_limit()  :106
```

**最根本的差异**：基线的 `effective_cpu_util()` 是**调度器内部调用**，
WALT 的 `cpu_util_freq_walt()` 是**从 governor 反向调用回调度器**——
这就是为什么 WALT 的负载侧代码在 `walt.c` 而映射侧在 `cpufreq_walt.c`。

---

## 1. 触发时机

| | schedutil | WALT |
|---|---|---|
| 注册 | `cpufreq_add_update_util_hook()` [cpufreq.h:20](../../kernel/include/linux/sched/cpufreq.h#L20) | `waltgov_add_callback()` [walt.h:383](../../kernel/kernel/sched/walt/walt.h#L383) |
| 触发者 | `cpufreq_update_util()` [sched.h:2896](../../kernel/kernel/sched/sched.h#L2896)，**7 个调度器调用点** | `__walt_irq_work_locked()` [walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992)，**窗口滚动** |
| 频率 | 每次 enqueue/tick/RT 变化 | **每窗口一次**（~16-20ms）|
| 去重 | `sg_policy->work_in_progress` / `need_freq_update` | `walt_load_reported_window` 的 `cmpxchg`（在 `walt.c` 侧）|
| 响应下界 | 事件驱动，可到微秒级 | **一个窗口**；唯一例外是 `do_pl_notif()` |

> **这是 WALT 在突发负载上可能慢于 schedutil 的根本原因**——
> 详见 [cpufreq.md §7](../02-walt/03-cpufreq.md#7-与-schedutil-的对照速览)。

---

## 2. util 计算

### 2.1 基线：`effective_cpu_util()`

core.c:7320。八步计算：CFS → IRQ → RT → DL → thermal → uclamp → …

```c
unsigned long effective_cpu_util(int cpu, unsigned long util_cfs, ...)   /* :7320 */
{
        unsigned long util, irq, max = ...;
        unsigned long new_util = ULONG_MAX;                              /* :7326 */

        trace_android_rvh_effective_cpu_util(cpu, util_cfs, max, type, p, &new_util);  /* :7328 */
        if (new_util != ULONG_MAX)                                       /* :7329 */
                return new_util;                                         /* :7330 */
        ...
}
```

> **【重要】`★ WALT 在这里接管` 是错的。** 本树中
> `register_trace_android_rvh_effective_cpu_util` **搜索结果为空**——
> 没有任何注册者。因此 `new_util` 恒为 `ULONG_MAX`，
> **短路分支永不进入**，第 2-8 步完整执行。
>
> WALT 与这个函数**没有调用关系**：它走 §2.2 那条独立的路。
> 两者是**平行**的两套 util 计算。若要验证，只需两条命令：
>
> ```bash
> grep -rn 'register_trace_android_rvh_effective_cpu_util' .
> grep -rn 'cpu_util_freq_walt' kernel/kernel/sched/   # 唯一调用者 cpufreq_walt.c:284
> ```
>
> `[待确认]`：该符号是 `EXPORT_TRACEPOINT_SYMBOL_GPL`
> [vendor_hooks.c:121](../../kernel/kernel/sched/vendor_hooks.c#L121) 导出的，
> **树外**模块仍可运行时注册它，届时短路会生效。
> 所以它是「设计上存在、本树未启用」的接管点。
> 已登记于 [04-open-questions.md](04-open-questions.md)。

### 2.2 WALT：`cpu_util_freq_walt()` —— **跨文件调用**

walt.c:680。**由 governor 侧主动调用**，不是由调度器推入。

```c
unsigned long
cpu_util_freq_walt(int cpu, struct walt_cpu_load *walt_load, unsigned int *reason)
{
        ...
        if (!cpumask_test_cpu(cpu, &asym_cap_sibling_cpus))
                return __cpu_util_freq_walt(cpu, walt_load, reason);

        /* 非对称容量兄弟核：取较高的 util */
        for_each_cpu(i, &asym_cap_sibling_cpus) { ... }
        util = ADJUSTED_ASYM_CAP_CPU_UTIL(util, util_other, mpct);
}
```

### 2.3 `__cpu_util_freq_walt()`：数据源

walt.c:638。

```c
util = scale_time_to_util(freq_policy_load(rq, reason));   /* ← 窗口计数器，不是 PELT */
wrq->util = util;
```

`freq_policy_load()`（[walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590)）
取**四路的最大值**（不是和）：

```c
load = wrq->prev_runnable_sum + aggr_grp_load;   /* 或 + grp_time.prev_runnable_sum */
if (kload > load)  load = kload;                 /* ksoftirqd 在跑 */
if (tt_load > load) load = tt_load;              /* top task 负载 */
if (should_apply_suh_freq_boost(cluster)) ...    /* user hint */
```

| 维度 | schedutil | WALT |
|---|---|---|
| 数据源 | PELT `rq->cfs.avg.util_avg` | **窗口计数器** `prev_runnable_sum` |
| 时间尺度 | 指数衰减，半衰期 32.8ms | **上一个窗口**（~16-20ms）的累计 |
| IRQ / RT / DL | 各自显式累加 | **无独立项**（只通过 `prev_runnable_sum` 反映）|
| 新任务 | `util_est`（enqueued / ewma）| **`nt_*` 计数器 + `nl` 通道 + NWD** |
| iowait boost | 有（`sugov_iowait_boost`，翻倍/减半）| **无**（改为 ksoftirqd 检测）|
| 额外 boost 源 | uclamp / thermal | uclamp + **ksoftirqd / top task / RTG / user hint / 预测** |
| 非对称兄弟核 | 无 | **`ADJUSTED_ASYM_CAP_CPU_UTIL`** |

### 2.4 uclamp 的应用位置 [关键]

| | 位置 |
|---|---|
| schedutil | `effective_cpu_util()` **内部**（第 6 步）|
| WALT | governor 侧 **`waltgov_get_util()`** [cpufreq_walt.c:276](../../kernel/kernel/sched/walt/cpufreq_walt.c#L276) |

```c
/* WALT: cpufreq_walt.c:276 */
util = cpu_util_freq_walt(wg_cpu->cpu, &wg_cpu->walt_load, &wg_cpu->reasons);
return uclamp_rq_util_with(rq, util, NULL);
```

> **因为 WALT 不走 `effective_cpu_util()`**，那里的 uclamp 处理
> （第 4 步，[core.c:7359](../../kernel/kernel/sched/core.c#L7359)）
> 自然也不会执行——WALT 必须自己补上。
>
> **注意归因**：原因是「**不在这条链上**」，不是「短路了这条链」。
> 后者（`core.c:7330` 的 `return new_util`）在本树中无注册者、从不触发。
> 这是个仍有价值的对照案例：一旦你**离开**了某个公共函数的调用链，
> 就必须自己接管它**所有的副作用**（此处是 uclamp），
> 而不仅仅是它返回的主值。

### 2.5 `walt_cpu_load`：新导出的中间结构

WALT 新增了 `struct walt_cpu_load`，把 governor 需要的调度器侧信息打包：

| 字段 | 用途 |
|---|---|
| `nl` | 新任务负载 → NWD 判定 |
| `pl` | 预测需求 → PL 提升 |
| `ws` | 上报窗口 → `waltgov_calc_avg_cap` |
| `rtgb_active` | RTG boost 中 |
| `big_task_rotation` | BTR 中 |
| `ed_active` | Early detection 中 |

**基线没有对应物**——`sugov_get_util()` 只拿到一个 `util` 数值。

---

## 3. 频率映射

### 3.1 基线：`get_next_freq()`

cpufreq_schedutil.c:177。

```c
unsigned int freq = arch_scale_freq_invariant() ?
                        policy->cpuinfo.max_freq : policy->cur;      /* :181-182 */
unsigned long next_freq = 0;

util = map_util_perf(util);                                          /* :185  ×1.25 */
trace_android_vh_map_util_freq(util, freq, max, &next_freq);         /* :186 */
trace_android_vh_map_util_freq_new(util, freq, max, &next_freq, policy,
                &sg_policy->need_freq_update);                       /* :187 */
if (next_freq)
        freq = next_freq;
else
        freq = map_util_freq(util, freq, max);                       /* :192 */

if (freq == sg_policy->cached_raw_freq && !sg_policy->need_freq_update)
        return sg_policy->next_freq;                                 /* :194-195 缓存 */
sg_policy->cached_raw_freq = freq;
return cpufreq_driver_resolve_freq(policy, freq);                    /* :198 */
```

两个 helper（[cpufreq.h:26-35](../../kernel/include/linux/sched/cpufreq.h#L26-L35)）：

```c
static inline unsigned long map_util_freq(unsigned long util,
                                        unsigned long freq, unsigned long cap)
{
        return freq * util / cap;        /* :29 */
}

static inline unsigned long map_util_perf(unsigned long util)
{
        return util + (util >> 2);       /* :34  ×1.25 */
}
```

### 3.2 WALT：`walt_map_util_freq()`

cpufreq_walt.c:209。**不用 `map_util_perf` / `map_util_freq`**。

```c
#define TARGET_LOAD 80
static inline unsigned long walt_map_util_freq(unsigned long util,
                                        struct waltgov_policy *wg_policy,
                                        unsigned long cap, int cpu)
{
        unsigned long fmax = wg_policy->policy->cpuinfo.max_freq;     /* ★ 绝对 fmax */
        unsigned int shift = wg_policy->tunables->target_load_shift;

        if (util >= wg_policy->tunables->target_load_thresh &&
            cpu_util_rt(cpu_rq(cpu)) < (cap >> 2))
                return max(
                        (fmax + (fmax >> shift)) * util,
                        (fmax + (fmax >> 2)) * wg_policy->tunables->target_load_thresh
                        ) / cap;
        return (fmax + (fmax >> 2)) * util / cap;                      /* 基础：×1.25 */
}
```

### 3.3 三处实质差异

#### ① 基准频率：`fmax` vs `policy->cur`

| | 表达式 |
|---|---|
| schedutil | `arch_scale_freq_invariant() ? policy->cpuinfo.max_freq : policy->cur`（:181-182）|
| WALT | **无条件 `policy->cpuinfo.max_freq`** |

**为什么 WALT 可以**：WALT 的 util 已经通过 `scale_exec_time()` 做了
**频率归一化**（见 [window-model.md §8](../02-walt/01-window-model.md#8-频率归一化scale_exec_time)），
所以 `util/cap` 恒为「相对满频的比率」，乘 `fmax` 就是绝对目标频率。
基线的 PELT `util_avg` 也是频率不变的，但它保留了 `policy->cur` 这条路
以兼容 `!arch_scale_freq_invariant()` 的架构。

#### ② 余量的表达方式不同但数值等价

| | 表达式 |
|---|---|
| schedutil | `map_util_perf(util)` = `util × 1.25`，再 `× freq / cap` |
| WALT | `(fmax + (fmax >> 2)) × util / cap` = `fmax × 1.25 × util / cap` |

**数学上等价**（乘法交换律）。差别在于 WALT 把它写在映射函数内部，
基线写成一个独立的 helper。

#### ③ WALT 多一条高负载分支

当 `util >= target_load_thresh`（默认 1024）且 RT 压力不大时，
WALT 用 **`max(1.0625×fmax×util, 1.25×fmax×thresh) / cap`**：

- 余量从 1.25 降到 **1.0625**（`>> shift`，shift 默认 4）
- 第二项保证「过了阈值就至少给到某个下限」

**基线没有这条分支。** `[推测]` 目的在于：满载时 1.25 倍余量只会
无谓地把频率顶到 fmax，改为小余量可以省电。

#### ④ WALT 多一层频率档吸附

```c
if (wg_policy->tunables->adaptive_high_freq) {                     /* :237 */
        if (raw_freq < get_adaptive_low_freq(wg_policy)) {
                freq = get_adaptive_low_freq(wg_policy);           /* 吸附低频档 */
                wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_LOW;
        } else if (raw_freq <= get_adaptive_high_freq(wg_policy)) {
                freq = get_adaptive_high_freq(wg_policy);          /* 吸附高频档 */
                wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_HIGH;
        }
}
```

**基线没有概念。** 这是 OS 厂商厂商定制（减少频率抖动）。

### 3.4 频率缓存：两者都有

| | schedutil | WALT |
|---|---|---|
| 字段 | `sg_policy->cached_raw_freq` | `wg_policy->cached_raw_freq` |
| 命中判定 | `freq == cached_raw_freq && !need_freq_update`（:194）| 同（`get_next_freq` :237 内）|
| 命中动作 | `return sg_policy->next_freq` | `return 0`（调用方跳过）|

---

## 4. 跨 CPU 聚合

### 4.1 基线：`sugov_update_shared()`

cpufreq_schedutil.c:472。

```c
for_each_cpu(j, policy->cpus) {
        sugov_iowait_boost(j_sg_cpu, time, flags);                  /* :480 */
        util = max(util, j_sg_cpu->util);
        ...
}
```

**取 util 的简单最大值**，然后调 `get_next_freq(sg_policy, util, max)`
（[:468](../../kernel/kernel/sched/cpufreq_schedutil.c#L468)）。

### 4.2 WALT：`waltgov_next_freq_shared()`

cpufreq_walt.c:364。**复杂得多**。

```c
for_each_cpu(j, policy->cpus) {
        j_util = j_wg_cpu->util;
        j_nl   = j_wg_cpu->walt_load.nl;
        j_max  = j_wg_cpu->max;

        if (boost) {                                       /* 全局 boost 百分比 */
                j_util = mult_frac(j_util, boost + 100, 100);
                j_nl   = mult_frac(j_nl, boost + 100, 100);
        }

        if (j_util * max >= j_max * util) {                /* ★ 交叉相乘比较比值 */
                util = j_util;
                max  = j_max;
                wg_policy->driving_cpu = j;
        }

        waltgov_walt_adjust(j_wg_cpu, j_util, j_nl, &util, &max);   /* ★ 每个 CPU 都调 */
}

return get_next_freq(wg_policy, util, max, wg_cpu, time);
```

**三处差异**：

| | 基线 | WALT |
|---|---|---|
| 比较量 | `util` 的绝对值 | **`util/max` 的比值**（交叉相乘 `j_util*max >= j_max*util`）|
| 迁移基准 | 无 | 同时保留胜出的 `max`，二者配对 |
| boost | 无（iowait boost 在别处）| **每个 CPU 跑一次 `waltgow_walt_adjust()`** |
| 记录 | 无 | `driving_cpu` 记录「谁赢的」|

> **交叉相乘的必要性**：不同簇的 `max` 不同，直接比 `util` 绝对值
> 会偏向大核。比 `util/max` 才是公平的负载率比较。
> **基线只比 `util` 绝对值**——因为基线总是对同一个 policy（同簇）聚合，
> `max` 相同，比值与绝对值单调一致。WALT 需要这个形式是因为
> `waltgov_walt_adjust()` 会引入 `*max` 作为 boost 值（如 NWD/BTR
> 直接拉到 `*max`），此时 `max` 可能被改写。

### 4.3 WALT 的源码注释坑

[cpufreq_walt.c:372-380](../../kernel/kernel/sched/walt/cpufreq_walt.c#L372-L380)
的注释说明：如果所有 CPU 的 util 都是 0，初值 `max = 1` 会让
某个 CPU 的非零 util 算出**虚高的比值**。**`max = 1` 是有意的**——
它让第一个 CPU 的 `j_util * 1 >= j_max * 0` 恒成立从而被选中。

---

## 5. Boost 机制

### 5.1 基线：只有 iowait boost

| 函数 | 位置 | 行为 |
|---|---|---|
| `sugov_iowait_boost()` | :252 | iowait 任务唤醒时提升 boost 值 |
| `sugov_iowait_apply()` | :299 | 把 boost 应用到 util；每次调用**减半** |

**机制**：`sg_cpu->iowait_boost` 从 `map_util_freq(...)` 起，
每次 `sugov_iowait_apply()` **减半**，形成一个衰减脉冲。

### 5.2 WALT：无 iowait boost，但有 6 个 boost 源

`waltgov_walt_adjust()`（[cpufreq_walt.c:305](../../kernel/kernel/sched/walt/cpufreq_walt.c#L305)）：

| # | 源 | 条件 | reason |
|---:|---|---|---|
| 1 | **Early Detection** | `ed_active && sysctl_ed_boost_pct` | `EARLY_DET` |
| 2 | **RTG boost** | `walt_load.rtgb_active` | `RTG_BOOST` |
| 3 | **hispeed** | `cpu_util >= avg_cap × hispeed_load/100` 且非迁移 | `HISPEED` |
| 4 | **NWD** | `is_hiload && nl >= cpu_util × 75/100` | `NWD` |
| 5 | **PL** | `tunables->pl` 且预测需求高 | `PL` |
| 6 | **BTR** | `big_task_rotation` → 直接拉到 `max` | `BTR` |

**全部走 `max_and_reason()`**，取最大值：

```c
static inline void max_and_reason(unsigned long *cur_util, unsigned long boost_util,
                struct waltgov_cpu *wg_cpu, unsigned int reason)
{
        if (boost_util && boost_util >= *cur_util) {
                *cur_util = boost_util;
                wg_cpu->reasons = reason;        /* ★ 赋值，不是位或 */
                wg_cpu->wg_policy->driving_cpu = wg_cpu->cpu;
        }
}
```

> **【反直觉】`reasons` 是赋值而非位或**（除 `EARLY_DET` 用 `|=`）。
> 所以 trace 里看到的 `reasons` **只是最后一个生效的 boost 原因**。
> 调试时不要当位掩码解读。

### 5.3 `avg_cap`：基线没有的概念

`waltgov_calc_avg_cap()`（[:168](../../kernel/kernel/sched/walt/cpufreq_walt.c#L168)）
用 `waltgov_track_cycles()`（[:151](../../kernel/kernel/sched/walt/cpufreq_walt.c#L151)）
累计的**窗口内 CPU 周期数**反推平均频率：

```c
avg_freq = wg_policy->curr_cycles;
avg_freq /= sched_ravg_window / (NSEC_PER_SEC / KHZ);
wg_policy->avg_cap = freq_to_util(wg_policy, avg_freq);
```

**用途**：`hispeed_load` 是「相对本簇**当前能力**的百分比」。
如果因热限制只能跑 60% 频率，`hispeed_load = 90` 应该是
「当前能力的 90%」而非「标称的 90%」。`avg_cap` 提供这个动态基线。

**基线完全没有对应物**——`sugov` 的阈值都相对 `policy->cpuinfo.max_freq`。

---

## 6. 限速

| | schedutil | WALT |
|---|---|---|
| 函数 | `sugov_should_update_freq()` :71 | `waltgov_should_update_freq()` :85 **+** `waltgov_up_down_rate_limit()` :106 |
| 参数 | `sg_policy->rate_limit_us`（**单一**）| `min_rate_limit_ns` **+** `up_rate_delay_ns` **+** `down_rate_delay_ns` |
| 升/降分离 | **否** | **是** |

```c
/* WALT: waltgov_up_down_rate_limit() cpufreq_walt.c:106 */
if (next_freq > wg_policy->next_freq && delta_ns < wg_policy->up_rate_delay_ns)
        return true;                       /* 升频太快 → 限 */
if (next_freq < wg_policy->next_freq && delta_ns < wg_policy->down_rate_delay_ns)
        return true;                       /* 降频太快 → 限 */
```

**sysfs**：基线只有 `rate_limit_us`；WALT 有
`up_rate_limit_us` / `down_rate_limit_us`。

> **物理意义**：升频通常要**尽快**（保性能），降频可以**慢一点**（保稳定）。
> 所以 `up_rate_limit_us` 一般设得比 `down_rate_limit_us` 小。
> **基线用一个值无法表达这个非对称性。**

---

## 7. `WALT_CPUFREQ_CONTINUE`：基线没有的调度协议

`__walt_irq_work_locked()`（[walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992)）：

```c
if (i == num_cpus)
        waltgov_run_callback(cpu_rq(cpu), wflag);
else
        waltgov_run_callback(cpu_rq(cpu), wflag | WALT_CPUFREQ_CONTINUE);
```

`waltgov_update_freq()` 里：

```c
if (wg_policy->should_update_freq(wg_policy, time) &&
    !(flags & WALT_CPUFREQ_CONTINUE)) {           /* ★ CONTINUE 就不算频率 */
        next_f = waltgov_next_freq_shared(wg_cpu, time);
        ...
}
```

**只有簇内最后一个 CPU 真正算频率**，前面的只更新自己的 `wg_cpu->util`。

**原因**：`waltgov_next_freq_shared()` 要遍历**所有** CPU 的 util。
如果每个都算一次，前面几个会读到**还没更新**的 util。
所以：**先让所有 CPU 更新 util，最后一个做聚合决策。**

> **基线没有这个问题的原因是它不需要**——`sugov_update_shared()` 直接在
> 一个回调里 `for_each_cpu(j, policy->cpus)` 读**所有** `j_sg_cpu->util`，
> 每个 CPU 的回调只更新自己那一个 `sg_cpu`。
> WALT 是为了配合窗口滚动的批量处理才需要这个标志。

**标志定义**：[walt.h:321-326](../../kernel/kernel/sched/walt/walt.h#L321-L326)

| 值 | 名称 |
|---:|---|
| 0x1 | `WALT_CPUFREQ_ROLLOVER` |
| 0x2 | `WALT_CPUFREQ_CONTINUE` |
| 0x4 | `WALT_CPUFREQ_IC_MIGRATION` |
| 0x8 | `WALT_CPUFREQ_PL` |
| 0x10 | `WALT_CPUFREQ_EARLY_DET` |
| 0x20 | `WALT_CPUFREQ_BOOST_UPDATE` |

---

## 8. 写频率

| | schedutil | WALT |
|---|---|---|
| 快速切换 | `cpufreq_driver_fast_switch()` | `waltgov_fast_switch()` [:193](../../kernel/kernel/sched/walt/cpufreq_walt.c#L193) |
| 延迟路径 | `irq_work` → kthread | `waltgov_deferred_update()` [:202](../../kernel/kernel/sched/walt/cpufreq_walt.c#L202) |
| 选择 | `fast_switch_enabled` | 同 |

**这一层两者结构基本相同**，都是「能 fast_switch 就 fast_switch，
否则扔给 kthread」。差异在实现细节，不在架构。

---

## 9. 差异速查表

| 维度 | schedutil | WALT | 差异性质 |
|---|---|---|---|
| **触发** | 事件驱动 | **窗口滚动** | **架构级** |
| **负载侧代码位置** | 调度器内（`core.c`）| **`walt.c`**（跨文件反向调用）| **架构级** |
| util 来源 | PELT `util_avg` | 窗口计数器 | 数据模型 |
| 新任务 | `util_est` | `nt_*` + `nl` + NWD | 数据模型 |
| **iowait boost** | 有 | **无**（改 ksoftirqd 检测）| 功能 |
| **boost 源数** | 1 | **6** | 功能 |
| **uclamp 位置** | `effective_cpu_util()` 内（第 4 步） | **governor 侧**（`waltgov_get_util` 末行） | 不在同一链上 |
| 基准频率 | `freq_invariant ? fmax : cur` | **恒 `fmax`** | 数学等价 |
| 余量实现 | `map_util_perf()` ×1.25 | `(fmax + fmax>>2)` ×1.25 | 写法 |
| **高负载分支** | 无 | **有**（1.0625 余量）| 功能 |
| **频率档吸附** | 无 | **有**（adaptive low/high）| 功能 |
| 跨 CPU 比较 | `util` 绝对值 | **`util/max` 比值** | 正确性 |
| 跨 CPU boost | 无 | **每 CPU 跑 `walt_adjust`** | 功能 |
| **`avg_cap` 动态基线** | 无 | **有** | 功能 |
| 限速 | **单一** | **升/降分离** | 可调性 |
| **`CONTINUE` 协议** | 无 | **有** | 并发正确性 |
| 频率缓存 | `cached_raw_freq` | 同 | 一致 |
| fs / deferred | 同 | 同 | 一致 |

---

## 10. 相关文档

- 基线 schedutil 详解 → [03-schedutil.md](../01-baseline/03-schedutil.md)
- WALT governor 详解 → [03-cpufreq.md](../02-walt/03-cpufreq.md)
- 数据源（窗口模型）→ [01-window-model.md](../02-walt/01-window-model.md)
- 预测需求（PL / `do_pl_notif`）→ [02-demand-prediction.md](../02-walt/02-demand-prediction.md)
- 高层对照 → [base-vs-walt.md](01-base-vs-walt.md)
- 未决问题 → [open-questions.md](04-open-questions.md)
