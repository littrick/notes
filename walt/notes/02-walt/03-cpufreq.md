# WALT 调频：`walt` governor

> **源码**：[cpufreq_walt.c](../../kernel/kernel/sched/walt/cpufreq_walt.c)、[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

原生 schedutil 的 baseline 见 [../01-baseline/schedutil.md](../01-baseline/03-schedutil.md)。
逐函数差异见 [../03-comparison/cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md)。

---

## 1. 最重要的结构特征：跨两个文件

**WALT 调频的「负载侧」在 `walt.c`，「映射侧」在 `cpufreq_walt.c`。**
这是读这套代码最容易迷路的地方。

```
┌─ walt.c ──────────────────────────────────────────────┐
│  窗口模型维护的计数器                                   │
│    wrq->prev_runnable_sum / grp_time / top_tasks     │
│                    │                                  │
│                    ▼                                  │
│  freq_policy_load(rq, &reason)      walt.c:590        │
│                    │                                  │
│                    ▼                                  │
│  scale_time_to_util() → 0..1024                       │
│                    │                                  │
│  __cpu_util_freq_walt()             walt.c:638        │
│  cpu_util_freq_walt(cpu, &wl, &rsn) walt.c:680        │
└────────────────────┼──────────────────────────────────┘
                     │ util (0..1024)
                     │  ← 直接传参，不经过 effective_cpu_util()
┌─ cpufreq_walt.c ───▼──────────────────────────────────┐
│  waltgov_get_util()                 :276              │
│    └─ + uclamp_rq_util_with()                         │
│                    │                                  │
│  waltgov_next_freq_shared()         :364              │
│    └─ per CPU: waltgov_walt_adjust() :305   ← 各种 boost │
│                    │                                  │
│  get_next_freq()                    :237              │
│    └─ walt_map_util_freq()          :209              │
│                    │                                  │
│  waltgov_update_next_freq()         :124  ← 升降限速    │
│                    │                                  │
│  fast_switch / deferred_update                        │
└───────────────────────────────────────────────────────┘
```

> **为什么这样分**：负载侧需要访问窗口模型的数据（`wrq` / `wts`），
> 那在 `walt.c` 里；映射侧要注册成 cpufreq governor，
> 那必须是独立的模块。**两者通过 `cpu_util_freq_walt()` 这一个函数接口衔接。**

---

## 2. 负载侧：从窗口计数器到 util

### 2.1 `freq_policy_load()`

[walt.c:590](../../kernel/kernel/sched/walt/walt.c#L590)。**决定「这个 CPU 有多忙」的原始量。**

```c
static inline u64 freq_policy_load(struct rq *rq, unsigned int *reason)
{
        struct walt_rq *wrq = (struct walt_rq *) rq->android_vendor_data1;
        struct walt_sched_cluster *cluster = wrq->cluster;
        u64 aggr_grp_load = cluster->aggr_grp_load;
        u64 load, tt_load = 0, kload = 0;
        struct task_struct *cpu_ksoftirqd = per_cpu(ksoftirqd, cpu_of(rq));

        if (sched_freq_aggr_en) {
                load = wrq->prev_runnable_sum + aggr_grp_load;
                *reason = CPUFREQ_REASON_FREQ_AGR;
        }
        else
                load = wrq->prev_runnable_sum + wrq->grp_time.prev_runnable_sum;

        /* ksoftirqd 在跑 → 至少按它的需求 */
        if (cpu_ksoftirqd && READ_ONCE(cpu_ksoftirqd->__state) == TASK_RUNNING) {
                kload = task_load(cpu_ksoftirqd);
                if (kload > load) { load = kload; *reason = CPUFREQ_REASON_KSOFTIRQD; }
        }

        /* top task 负载 */
        tt_load = top_task_load(rq);
        if (tt_load > load) { load = tt_load; *reason = CPUFREQ_REASON_TT_LOAD; }

        /* user hint 提升 */
        if (should_apply_suh_freq_boost(cluster)) {
                if (is_suh_max()) load = sched_ravg_window;
                else load = div64_u64(load * sysctl_sched_user_hint, 100);
                *reason = CPUFREQ_REASON_SUH;
        }
        ...
        return load;
}
```

**输入是四个源的「最大值」，不是和**：

| 源 | 含义 |
|---|---|
| `wrq->prev_runnable_sum` | 上一窗口该 CPU 的主计数器（**不含 RTG**）|
| `+ aggr_grp_load` / `grp_time.prev_runnable_sum` | RTG 任务的负载（两种聚合方式）|
| `task_load(ksoftirqd)` | ksoftirqd 若在跑 |
| `top_task_load(rq)` | top-app 任务的负载 |
| user hint 缩放 | `sysctl_sched_user_hint` 百分比提升 |

> **【反直觉】是 `max` 不是 `sum`。** `load` 从 `prev_runnable_sum` 起步，
> 后续每一路只在**更大**时才覆盖它（`if (kload > load)` / `if (tt_load > load)`）。
>
> 这与 §2 的 RTG 分流设计一致——见
> [window-model.md §7.0](01-window-model.md)。**为什么用 max 而非 sum**：
> `top_task_load` 和 `prev_runnable_sum` 可能描述的是**同一个任务**
> （top-app 任务既在自己的需求里，也在 CPU 的忙时里），
> 相加会重复计算。【推测】

**两种 RTG 聚合方式**：

| 模式 | 表达式 | 条件 |
|---|---|---|
| `sched_freq_aggr_en` 开启 | `prev_runnable_sum + cluster->aggr_grp_load` | 簇级聚合 |
| 关闭 | `prev_runnable_sum + wrq->grp_time.prev_runnable_sum` | 本 CPU 的 grp 计数 |

`aggr_grp_load` 是**整个簇**的 RTG 负载；`grp_time` 是**本 CPU** 的。
前者用于「簇内任一 CPU 的需求算全簇的需求」，是更激进的聚合。`[推测]`

### 2.2 `cpu_util_freq_walt()`

[walt.c:680](../../kernel/kernel/sched/walt/walt.c#L680)。**这是 `walt` governor
获取 util 的唯一入口**，由 `waltgov_get_util()`
[cpufreq_walt.c:284](../../kernel/kernel/sched/walt/cpufreq_walt.c#L284) 调用，
**全树仅此一个调用者**。

> **【常见误解】它不注册给 `effective_cpu_util()` 的 hook。**
> `kernel/kernel/sched/core.c:7328` 那个 `android_rvh_effective_cpu_util`
> 短路点在**本树中没有任何注册者**，从不触发。
> WALT 是**绕开** `effective_cpu_util()` 而非改写它——
> 详见 [02-cpufreq-diff.md §2.1](../03-comparison/02-cpufreq-diff.md)
> 与 [11-freq-pipeline.md §7](11-freq-pipeline.md)。

```c
unsigned long
cpu_util_freq_walt(int cpu, struct walt_cpu_load *walt_load, unsigned int *reason)
{
        ...
        if (!cpumask_test_cpu(cpu, &asym_cap_sibling_cpus))
                return __cpu_util_freq_walt(cpu, walt_load, reason);

        for_each_cpu(i, &asym_cap_sibling_cpus) {
                if (i == cpu) util = __cpu_util_freq_walt(cpu, walt_load, reason);
                else          util_other = __cpu_util_freq_walt(i, &wl_other, reason);
        }

        if (cpu == cpumask_last(&asym_cap_sibling_cpus))
                mpct = 100;

        util = ADJUSTED_ASYM_CAP_CPU_UTIL(util, util_other, mpct);
        ...
}
```

**非对称容量兄弟 CPU 的处理**：

```c
#define ADJUSTED_ASYM_CAP_CPU_UTIL(orig, other, x) \
                        (max(orig, mult_frac(other, x, 100)))
```

即「**同样容量但标称频率不同的兄弟核，取较高 util**」，
比例由 `sysctl_sched_asym_cap_sibling_freq_match_pct` 控制。
目的是让同容量的两个核（如两个 prime 核）跑一致频率，
避免「一个核忙一个核闲却因为共享时钟域而互相拖累」。

### 2.3 `__cpu_util_freq_walt()`

[walt.c:638](../../kernel/kernel/sched/walt/walt.c#L638)：

```c
util = scale_time_to_util(freq_policy_load(rq, reason));
wrq->util = util;                       /* 存下来供观测 */

if (walt_load) {
        u64 nl = wrq->nt_prev_runnable_sum + wrq->grp_time.nt_prev_runnable_sum;
        u64 pl = wrq->walt_stats.pred_demands_sum_scaled;

        wrq->old_busy_time = util;
        wrq->old_estimated_time = pl;    /* ← do_pl_notif 的输入 */

        nl = scale_time_to_util(nl);
        walt_load->nl = nl;              /* 新任务负载 */
        walt_load->pl = pl;              /* 预测负载 */
        walt_load->ws = walt_load_reported_window;
        walt_load->rtgb_active = rtgb_active;
        walt_load->big_task_rotation = walt_rotation_enabled;
        walt_load->ed_active = !!wrq->ed_task;
}

return (util >= capacity) ? capacity : util;
```

导出的 `struct walt_cpu_load` 是 governor 侧的输入：

| 字段 | 含义 | governor 侧用途 |
|---|---|---|
| `nl` | 新任务（`nt_*`）负载 | NWD 判定（`CPUFREQ_REASON_NWD`）|
| `pl` | 预测需求 | PL 提升（`CPUFREQ_REASON_PL`）|
| `ws` | 上报的窗口 | `waltgov_calc_avg_cap` 的窗口对齐 |
| `rtgb_active` | RTG boost 生效中 | `CPUFREQ_REASON_RTG_BOOST` |
| `big_task_rotation` | big task 轮转中 | `CPUFREQ_REASON_BTR` |
| `ed_active` | early-detection 任务存在 | `CPUFREQ_REASON_EARLY_DET` |

> **注意 `wrq->old_busy_time` / `old_estimated_time` 在这里被更新**——
> 它们是 `do_pl_notif()`（[demand-prediction.md §6.3](02-demand-prediction.md)）
> 的比较基准。**这条链把「预测」接到了「紧急加频」上**。

### 2.4 与 schedutil 的根本差异

| | schedutil | WALT |
|---|---|---|
| util 来源 | `rq->cfs.avg.util_avg`（PELT）| 窗口计数器 → `freq_policy_load()` |
| 时间尺度 | 半衰期 32.8 ms 的指数平均 | 上一个窗口（~16-20ms）的累计 |
| 额外输入 | iowait boost、uclamp、RT/DL/IRQ | **ksoftirqd、top task、RTG、user hint、预测** |
| 新任务 | `util_est` 的 FASTUP | `nt_*` 计数器 + `nl` 通道 |

---

## 3. 映射侧：`waltgov`

### 3.1 数据结构

`struct waltgov_policy` [cpufreq_walt.c:36](../../kernel/kernel/sched/walt/cpufreq_walt.c#L36)：

| 字段 | 用途 |
|---|---|
| `last_ws` | 上次计算的窗口起点 |
| `curr_cycles` / `last_cyc_update_time` | 窗口内累计的 CPU 周期数 |
| **`avg_cap`** | **上一窗口的平均容量**（由周期数算出）|
| `hispeed_util` / `rtg_boost_util` | 由频率反算的 util 阈值 |
| `max` | 本簇的 `arch_scale_cpu_capacity()` |
| `next_freq` / `cached_raw_freq` | 频率缓存 |
| `driving_cpu` | 哪个 CPU 决定了本次频率 |
| `limits_changed` / `need_freq_update` | 强制更新标志 |

`struct waltgov_cpu` [:68](../../kernel/kernel/sched/walt/cpufreq_walt.c#L68)：
`walt_load`（§2.3 的 `struct walt_cpu_load`）、`util`、`max`、`flags`、`reasons`。

### 3.2 `avg_cap`：用周期数反推平均容量 [重要]

`waltgov_calc_avg_cap()` [:168](../../kernel/kernel/sched/walt/cpufreq_walt.c#L168) +
`waltgov_track_cycles()` [:151](../../kernel/kernel/sched/walt/cpufreq_walt.c#L151)：

```c
static void waltgov_track_cycles(...)
{
        u64 next_ws = wg_policy->last_ws + sched_ravg_window;

        upto = min(upto, next_ws);
        delta_ns = upto - wg_policy->last_cyc_update_time;
        delta_ns *= prev_freq;                          /* 频率 × 时间 = 周期数 */
        do_div(delta_ns, (NSEC_PER_SEC / KHZ));
        cycles = delta_ns;
        wg_policy->curr_cycles += cycles;
        wg_policy->last_cyc_update_time = upto;
}
```

**在每个窗口边界，把「这一窗口内 CPU 实际跑了多少周期」累积起来，
再除以窗口长度，得到平均频率，最后换成 `avg_cap`。**

```c
avg_freq = wg_policy->curr_cycles;
avg_freq /= sched_ravg_window / (NSEC_PER_SEC / KHZ);
wg_policy->avg_cap = freq_to_util(wg_policy, avg_freq);
```

> **为什么要自己算平均频率？** `hispeed_load` 是「相对本簇**当前能力**的百分比」——
> 如果 CPU 因为热限制只能跑到 60% 的频率，`hispeed_load = 90` 应该是
> 「当前能力的 90%」而不是「标称的 90%」。
> `avg_cap` 提供了这个动态基线。
>
> **【反直觉】这个 `avg_cap` 是「窗口内平均」而非瞬时值**——
> 周期数/时间是平均值，所以它对 DVFS 抖动不敏感。

还有一处跳窗处理：

```c
if (curr_ws > (last_ws + sched_ravg_window)) {
        avg_freq = prev_freq;                       /* 跳过多个窗口：退化用当前频率 */
        wg_policy->last_cyc_update_time = curr_ws;  /* 重置追踪 */
}
```

### 3.3 `waltgov_get_util()`

[:276](../../kernel/kernel/sched/walt/cpufreq_walt.c#L276)：

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

**`uclamp` 在 governor 侧单独应用**——这是 WALT 与 schedutil 的**真实差异**：
schedutil 的 uclamp 发生在 `effective_cpu_util()` 内部第 4 步
（[core.c:7359](../../kernel/kernel/sched/core.c#L7359)），
而 WALT 把它放在 governor 的最后一行。
`p` 传 NULL，与 `sugov_get_util()` 一致（只应用 rq 级 clamp）。

> **但原因不是「WALT 短路了 `effective_cpu_util()`」。**
> 那个短路点（[core.c:7328-7330](../../kernel/kernel/sched/core.c#L7328-L7330)）
> 在本树中无注册者、从不触发。真实原因是
> **WALT 根本不在 `effective_cpu_util()` 这条链上**——
> 它自己算 util，所以必须自己补上 uclamp 这一步。
> 详见 [02-cpufreq-diff.md §2.4](../03-comparison/02-cpufreq-diff.md)。

### 3.4 `waltgov_walt_adjust()`：boost 汇总

[:305](../../kernel/kernel/sched/walt/cpufreq_walt.c#L305)。**这是 WALT 相对
schedutil 增加的全部「加速逻辑」的汇总点。**

```c
static void waltgov_walt_adjust(struct waltgov_cpu *wg_cpu, unsigned long cpu_util,
                                unsigned long nl, unsigned long *util, unsigned long *max)
{
        bool is_migration = wg_cpu->flags & WALT_CPUFREQ_IC_MIGRATION;
        bool is_rtg_boost = wg_cpu->walt_load.rtgb_active;
        bool big_task_rotation = wg_cpu->walt_load.big_task_rotation;
        bool employ_ed_boost = wg_cpu->walt_load.ed_active && sysctl_ed_boost_pct;
        unsigned long pl = wg_cpu->walt_load.pl;

        /* 1. Early detection：检测到重任务提前加频 */
        if (employ_ed_boost) {
                cpu_util = mult_frac(cpu_util, 100 + sysctl_ed_boost_pct, 100);
                max_and_reason(util, cpu_util, wg_cpu, CPUFREQ_REASON_EARLY_DET);
        }

        /* 2. RTG boost：相关线程组活跃 */
        if (is_rtg_boost)
                max_and_reason(util, wg_policy->rtg_boost_util, wg_cpu, CPUFREQ_REASON_RTG_BOOST);

        /* 3. hispeed：负载超过 hispeed_load% 就至少跑 hispeed_freq */
        is_hiload = (cpu_util >= mult_frac(wg_policy->avg_cap,
                                           wg_policy->tunables->hispeed_load, 100));
        if (is_hiload && !is_migration)
                max_and_reason(util, wg_policy->hispeed_util, wg_cpu, CPUFREQ_REASON_HISPEED);

        /* 4. NWD：新任务多且负载高（New Workload Detection）*/
        if (is_hiload && nl >= mult_frac(cpu_util, NL_RATIO, 100))
                max_and_reason(util, *max, wg_cpu, CPUFREQ_REASON_NWD);

        /* 5. PL：预测需求超过实测 */
        if (wg_policy->tunables->pl) {
                if (sysctl_sched_conservative_pl)
                        pl = mult_frac(pl, TARGET_LOAD, 100);
                max_and_reason(util, pl, wg_cpu, CPUFREQ_REASON_PL);
        }

        if (employ_ed_boost)
                wg_cpu->reasons |= CPUFREQ_REASON_EARLY_DET;

        /* 6. BTR：big task rotation 中，直接拉到 max */
        if (big_task_rotation)
                max_and_reason(util, *max, wg_cpu, CPUFREQ_REASON_BTR);
}
```

**六个 boost 源，全部走 `max_and_reason()`——取最大值，且记录「谁赢了」**：

```c
static inline void max_and_reason(unsigned long *cur_util, unsigned long boost_util,
                struct waltgov_cpu *wg_cpu, unsigned int reason)
{
        if (boost_util && boost_util >= *cur_util) {
                *cur_util = boost_util;
                wg_cpu->reasons = reason;           /* ← 覆盖，不是位或 */
                wg_cpu->wg_policy->driving_cpu = wg_cpu->cpu;
        }
}
```

> **【反直觉】`wg_cpu->reasons = reason` 是赋值**（除了 `EARLY_DET` 那行
> 用 `|=`）。所以 `trace_waltgov_next_freq` 里看到的 `reasons` **只是最后一个
> 生效的 boost 原因**，不是全部。调试时不要把 `reasons` 当成位掩码来解读。

reason 常量定义在 [walt.h:328-340](../../kernel/kernel/sched/walt/walt.h#L328-L340)：

| 值 | 名称 | 触发 |
|---:|---|---|
| 0 | `CPUFREQ_REASON_LOAD` | 默认（纯负载）|
| 0x1 | `BTR` | big task rotation |
| 0x2 | `PL` | 预测需求 |
| 0x4 | `EARLY_DET` | early detection |
| 0x8 | `RTG_BOOST` | RTG 活跃 |
| 0x10 | `HISPEED` | 高负载 |
| 0x20 | `NWD` | 新任务检测 |
| 0x40 | `FREQ_AGR` | 频率聚合模式 |
| 0x80 | `KSOFTIRQD` | ksoftirqd 在跑 |
| 0x100 | `TT_LOAD` | top task 负载 |
| 0x200 | `SUH` | user hint |
| 0x400 / 0x800 | `ADAPTIVE_LOW` / `ADAPTIVE_HIGH` | adaptive 频率档 |

**几个阈值常量**：

| 常量 | 值 | 含义 |
|---|---:|---|
| `TARGET_LOAD` | 80 | 目标负载百分比 |
| `NL_RATIO` | 75 | NWD 判定的 nl/cpu_util 比 |
| `DEFAULT_HISPEED_LOAD` | 90 | 默认 hispeed 阈值 |
| `DEFAULT_TARGET_LOAD_THRESH` | 1024 | |
| `DEFAULT_TARGET_LOAD_SHIFT` | 4 | |
| `DEFAULT_SILVER/GOLD/PRIME_RTG_BOOST_FREQ` | 1000000 / 768000 / 0 | |

### 3.5 `waltgov_next_freq_shared()`

[:364](../../kernel/kernel/sched/walt/cpufreq_walt.c#L364)。**跨 CPU 聚合**。

```c
for_each_cpu(j, policy->cpus) {
        j_util = j_wg_cpu->util;
        j_nl   = j_wg_cpu->walt_load.nl;
        j_max  = j_wg_cpu->max;

        if (boost) {                             /* 全局 boost 百分比 */
                j_util = mult_frac(j_util, boost + 100, 100);
                j_nl   = mult_frac(j_nl, boost + 100, 100);
        }

        if (j_util * max >= j_max * util) {      /* 交叉相乘比较比值 */
                util = j_util;
                max  = j_max;
                wg_policy->driving_cpu = j;
        }

        waltgov_walt_adjust(j_wg_cpu, j_util, j_nl, &util, &max);
}

return get_next_freq(wg_policy, util, max, wg_cpu, time);
```

**两条关键设计**：

**(a) 用交叉相乘而非浮点比**

```c
if (j_util * max >= j_max * util)     /* 等价于 j_util/j_max >= util/max */
```

避免除法，且**保留 `j_max` 一起选出**——因为不同簇的 `max` 不同，
必须在同一比值基准上比较。

**(b) `waltgov_walt_adjust()` 在循环内被每个 CPU 调用一次**

所以任一 CPU 的 boost 都能拉高**整个 policy** 的目标频率。
`max_and_reason` 内部的 `driving_cpu = wg_cpu->cpu` 记录了是哪个 CPU 赢的。

> **源码注释里的一个坑**（[:372-380](../../kernel/kernel/sched/walt/cpufreq_walt.c#L372-L380)）：
> 如果所有 CPU 的 util 都是 0，初值 `max = 1` 会让后面某个 CPU 的
> 非零 util 算出一个**虚高的比值**，导致 `freq` 突跳到 fmax。
> 注释说明这是「WALT 统计后续更新聚合 util」造成的。**初始 `max = 1` 是有意的**
> ——它让第一个 CPU 的 `j_util * 1 >= j_max * 0` 恒成立，从而被选中。

---

## 4. 频率映射：`walt_map_util_freq()`

[:209](../../kernel/kernel/sched/walt/cpufreq_walt.c#L209)：

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

### 4.1 基础公式

```c
return (fmax + (fmax >> 2)) * util / cap;
```

即 **`1.25 × fmax × util / cap`**。

> **【关键差异】WALT 用绝对 `fmax`，schedutil 用 `base_freq`。**
>
> schedutil 的 `get_next_freq()`（[cpufreq_schedutil.c:181-182](../../kernel/kernel/sched/cpufreq_schedutil.c#L181-L182)）：
> `freq = arch_scale_freq_invariant() ? policy->cpuinfo.max_freq : policy->cur`
> ——**非 frequency-invariant 架构下用 `policy->cur`**。
>
> WALT **无条件用 `fmax`**。因为 WALT 的 util 已经通过
> `scale_exec_time()` 做了频率归一化（见
> [window-model.md §8](01-window-model.md#8-频率归一化scale_exec_time)），
> 所以 `util/cap` 恒为「相对满频的比率」，乘以 `fmax` 就是绝对目标频率。
>
> 1.25 倍余量的表达方式两者**数值等价**（`util + util>>2` vs `fmax + fmax>>2` 配 `util`）。

### 4.2 高负载分支

当 `util >= target_load_thresh`（默认 1024，即**满负载**）且
`RT util < cap/4`（RT 压力不大）时，走另一条公式：

```c
max( (fmax + (fmax >> shift)) * util,          /* shift=4 → 1.0625 × fmax */
     (fmax + (fmax >> 2)) * target_load_thresh ) / cap
```

**为什么要 `max` 两项**：第二项保证「无论 util 多低（只要过了阈值），
至少给到 `1.25 × fmax × thresh / cap`」。第一项用较小的余量（1.0625 而非 1.25）
处理高 util——**避免在满载时因为 1.25 倍余量而长期钉在 fmax**。

> `[推测]` 这是省电考量：余量在接近满负载时反而浪费，
> 因为此时系统已经需要高频，1.25 倍只会让它无谓地顶到最高频。
> 用 `target_load_shift` 把余量降到 1/16。

### 4.3 adaptive 频率档

`get_next_freq()` [:237](../../kernel/kernel/sched/walt/cpufreq_walt.c#L237)：

```c
raw_freq = walt_map_util_freq(util, wg_policy, max, wg_driv_cpu->cpu);
freq = raw_freq;

if (wg_policy->tunables->adaptive_high_freq) {
        if (raw_freq < get_adaptive_low_freq(wg_policy)) {
                freq = get_adaptive_low_freq(wg_policy);       /* 吸附到低频档 */
                wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_LOW;
        } else if (raw_freq <= get_adaptive_high_freq(wg_policy)) {
                freq = get_adaptive_high_freq(wg_policy);      /* 吸附到高频档 */
                wg_driv_cpu->reasons = CPUFREQ_REASON_ADAPTIVE_HIGH;
        }
}
```

**把一个连续的频率目标「吸附」到两个档位之一**，减少频率跳变。
`get_adaptive_low/high_freq()` 取「userspace 设定值」与「kernel 设定值」的 `max`。

### 4.4 频率缓存与升降限速

```c
if (wg_policy->cached_raw_freq && freq == wg_policy->cached_raw_freq &&
    !wg_policy->need_freq_update)
        return 0;                            /* 缓存命中，不改 */
```

`waltgov_update_next_freq()` [:124](../../kernel/kernel/sched/walt/cpufreq_walt.c#L124)：

```c
if (wg_policy->next_freq == next_freq) return false;

if (waltgov_up_down_rate_limit(wg_policy, time, next_freq)) {
        wg_policy->cached_raw_freq = 0;      /* 被限速 → 清缓存，下次强制重算 */
        return false;
}
wg_policy->cached_raw_freq = raw_freq;
wg_policy->next_freq = next_freq;
wg_policy->last_freq_update_time = time;
return true;
```

**WALT 有两级限速**（schedutil 只有一级）：

| 级别 | 字段 | 位置 |
|---|---|---|
| 最小间隔 | `min_rate_limit_ns` | `waltgov_should_update_freq()` [:85](../../kernel/kernel/sched/walt/cpufreq_walt.c#L85) |
| **升频间隔** | `up_rate_delay_ns` | `waltgov_up_down_rate_limit()` [:106](../../kernel/kernel/sched/walt/cpufreq_walt.c#L106) |
| **降频间隔** | `down_rate_delay_ns` | 同上 |

```c
if (next_freq > wg_policy->next_freq && delta_ns < wg_policy->up_rate_delay_ns)
        return true;                          /* 升频太快 → 限 */
if (next_freq < wg_policy->next_freq && delta_ns < wg_policy->down_rate_delay_ns)
        return true;                          /* 降频太快 → 限 */
```

> **升降分离是 WALT 特有的**。schedutil 只有一个 `rate_limit_us`。
> 物理意义：升频通常要**尽快**（保性能），降频可以**慢一点**（保稳定）。
> 所以 `up_rate_limit_us` 一般设得比 `down_rate_limit_us` 小。
> 对应的 sysfs 节点：`up_rate_limit_us` / `down_rate_limit_us`。

---

## 5. 事件驱动：`waltgov_update_freq()`

[:403](../../kernel/kernel/sched/walt/cpufreq_walt.c#L403)。

```c
static void waltgov_update_freq(struct waltgov_callback *cb, u64 time,
                                unsigned int flags)
{
        if (!wg_policy->tunables->pl && flags & WALT_CPUFREQ_PL)
                return;                                  /* PL 未启用则忽略 PL 事件 */

        wg_cpu->util = waltgov_get_util(wg_cpu);
        wg_cpu->flags = flags;
        raw_spin_lock(&wg_policy->update_lock);

        if (wg_policy->max != wg_cpu->max) {             /* 拓扑变化 → 重算阈值 */
                wg_policy->max = wg_cpu->max;
                wg_policy->hispeed_util = target_util(wg_policy,
                                        wg_policy->tunables->hispeed_freq);
                wg_policy->rtg_boost_util = target_util(wg_policy,
                                        wg_policy->tunables->rtg_boost_freq);
        }

        waltgov_calc_avg_cap(wg_policy, wg_cpu->walt_load.ws, wg_policy->policy->cur);

        trace_waltgov_util_update(...);

        if (wg_policy->should_update_freq(wg_policy, time) &&
            !(flags & WALT_CPUFREQ_CONTINUE)) {          /* ← CONTINUE 就不算频率 */
                next_f = waltgov_next_freq_shared(wg_cpu, time);
                if (!next_f) goto out;
                if (fast_switch_enabled)
                        waltgov_fast_switch(wg_policy, time, next_f);
                else
                        waltgov_deferred_update(wg_policy, time, next_f);
        }
out:
        raw_spin_unlock(&wg_policy->update_lock);
}
```

### 5.1 `WALT_CPUFREQ_*` 标志

[walt.h:321-326](../../kernel/kernel/sched/walt/walt.h#L321-L326)：

| 值 | 名称 | 含义 |
|---:|---|---|
| 0x1 | `WALT_CPUFREQ_ROLLOVER` | 窗口滚动触发 |
| 0x2 | `WALT_CPUFREQ_CONTINUE` | **后面还有 CPU，先别算频率** |
| 0x4 | `WALT_CPUFREQ_IC_MIGRATION` | 簇间迁移 |
| 0x8 | `WALT_CPUFREQ_PL` | 预测需求事件 |
| 0x10 | `WALT_CPUFREQ_EARLY_DET` | early detection |
| 0x20 | `WALT_CPUFREQ_BOOST_UPDATE` | boost 状态变化 |

### 5.2 `WALT_CPUFREQ_CONTINUE` 的作用 [反直觉]

`__walt_irq_work_locked()`（[walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992)）
遍历簇内每个 CPU 时：

```c
if (i == num_cpus)
        waltgov_run_callback(cpu_rq(cpu), wflag);
else
        waltgov_run_callback(cpu_rq(cpu), wflag | WALT_CPUFREQ_CONTINUE);
```

**只有簇内最后一个 CPU 会真正算频率**（`!CONTINUE`）。
前面的 CPU 只更新自己的 `wg_cpu->util`，**不触发频率计算**。

原因：`waltgov_next_freq_shared()` 要遍历**所有** CPU 的 util 才能决定整簇频率。
如果每个 CPU 都算一次，前面几个 CPU 会读到**还没更新**的 util。
所以设计成：**先让所有 CPU 更新 util，最后一个做聚合决策。**

> 这也解释了为什么 `waltgov_update_freq()` 里
> `wg_cpu->util = waltgov_get_util(wg_cpu)` 在 `CONTINUE` 检查**之前**——
> 更新 util 是无条件的，算频率才是有条件的。

### 5.3 触发这个回调的路径

见 [data-flow.md §4](../00-overview/04-data-flow.md#4-消费方-1调频窗口滚动驱动)：

```
窗口滚动 → run_walt_irq_work_rollover() → walt_irq_work_queue()
   → walt_irq_work() → __walt_irq_work_locked()
        → per cluster, per cpu: waltgov_run_callback(rq, flags)
             → waltgov_update_freq()
```

`waltgov_run_callback()` 是内联函数
[walt.h:402](../../kernel/kernel/sched/walt/walt.h#L402)，从 per-CPU 的
`waltgov_cb_data` 取出回调并调用。

**注册**在 `waltgov_start()` 里通过
`waltgov_add_callback()` [walt.h:383](../../kernel/kernel/sched/walt/walt.h#L383)。

---

## 6. Tunables

`struct waltgov_tunables` [cpufreq_walt.c:19](../../kernel/kernel/sched/walt/cpufreq_walt.c#L19)：

| 字段 | 含义 |
|---|---|
| `up_rate_limit_us` / `down_rate_limit_us` | **升降频独立限速** |
| `hispeed_load` | 触发 hispeed 的负载百分比（默认 90）|
| `hispeed_freq` | hispeed 目标频率 |
| `rtg_boost_freq` | RTG boost 目标频率 |
| `adaptive_low_freq` / `adaptive_high_freq` | userspace 设定的吸附档 |
| `adaptive_low_freq_kernel` / `adaptive_high_freq_kernel` | kernel 设定的吸附档 |
| `target_load_thresh` | 高负载分支阈值（默认 1024）|
| `target_load_shift` | 高负载分支余量移位（默认 4）|
| `pl` | 是否启用预测需求提升 |
| `boost` | 全局 boost 百分比 |

governor 名称为 **`"walt"`**（[cpufreq_walt.c:1151-1152](../../kernel/kernel/sched/walt/cpufreq_walt.c#L1151-L1152)），
注册于 [:1163](../../kernel/kernel/sched/walt/cpufreq_walt.c#L1163)。

> 完整的 tunable 清单（含 WALT 的其他 sysctl）见
> [observability.md](10-observability.md)。

---

## 7. 与 schedutil 的对照速览

| 维度 | schedutil | WALT governor |
|---|---|---|
| **触发** | 事件驱动（enqueue/tick/RT 变化）| **窗口滚动**（时间驱动）|
| **频率** | 每次 util 变化 | 每窗口一次（`cmpxchg` 全局去重）|
| util 来源 | PELT `util_avg` | 窗口计数器 |
| 回调注册 | `cpufreq_add_update_util_hook` | `waltgov_add_callback` |
| 簇内聚合 | 取 `util/max` 最大者 | 同，但每个 CPU 都跑 `walt_adjust` |
| 余量 | `map_util_perf()` ×1.25 | `(fmax + fmax>>2)` ×1.25 |
| 基准频率 | `freq_invariant ? fmax : policy->cur` | **恒为 fmax** |
| 限速 | 单一 `rate_limit_us` | **升/降分离** |
| iowait boost | 有（翻倍/减半）| **无**（改用 ksoftirqd 检测等）|
| boost 源数量 | 1 | **6**（RTG/PL/NWD/BTR/ED/HISPEED）|
| 频率档吸附 | 无 | **adaptive low/high** |

> **响应延迟下界**：WALT 调频最快也要等**一个窗口**（~16-20ms）。
> 唯一例外是 `do_pl_notif()` 的亚窗口加频
> （[demand-prediction.md §6.3](02-demand-prediction.md)）。
> 这是 WALT 在突发负载上可能比 schedutil 慢的根本原因。

---

## 8. 相关文档

- 窗口模型（util 的数据源）→ [window-model.md](01-window-model.md)
- 预测需求（PL / `do_pl_notif`）→ [demand-prediction.md](02-demand-prediction.md)
- 原生 schedutil baseline → [../01-baseline/schedutil.md](../01-baseline/03-schedutil.md)
- 逐函数差异 → [../03-comparison/cpufreq-diff.md](../03-comparison/02-cpufreq-diff.md)
- boost 机制细节 → [boost.md](08-boost.md)
- `core_ctl`（核数）→ [power-side.md](07-power-side.md)
