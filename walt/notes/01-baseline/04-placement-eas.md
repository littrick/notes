# EAS 任务放置（baseline）

> **源码**：[fair.c](../../kernel/kernel/sched/fair.c)、[topology.c](../../kernel/kernel/sched/topology.c)、[energy_model.h](../../kernel/include/linux/energy_model.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本篇覆盖「任务该放到哪个 CPU」的原生实现：`select_task_rq_fair()` 与
EAS（Energy Aware Scheduling）的 `find_energy_efficient_cpu()`。

**这是 WALT 替换得最彻底的一块**——一个 hook 就把整个函数接管了。
见 [../02-walt/placement.md](../02-walt/04-placement.md)。

---

## 1. `select_task_rq_fair()` 决策树

[fair.c:7215](../../kernel/kernel/sched/fair.c#L7215)。注册在
[fair.c:12053](../../kernel/kernel/sched/fair.c#L12053) 的 `.select_task_rq`。

### 1.1 变量初始化（:7217-7224）

```c
int sync = (wake_flags & WF_SYNC) && !(current->flags & PF_EXITING);
struct sched_domain *tmp, *sd = NULL;
int cpu = smp_processor_id();               /* 唤醒者所在的 CPU */
int new_cpu = prev_cpu;                     /* 默认：留在原 CPU */
int want_affine = 0;
int target_cpu = -1;
/* SD_flags and WF_flags share the first nibble */
int sd_flag = wake_flags & 0xF;
```

> **`sd_flag = wake_flags & 0xF`** 这一行注释说明了原因：
> `WF_*` 与 `SD_BALANCE_*` 的标志位**共用低 4 位**，
> 有编译期断言保证（[sched.h:2107-2109](../../kernel/kernel/sched/sched.h#L2107-L2109)）。
> `WF_TTWU == SD_BALANCE_WAKE`、`WF_FORK == SD_BALANCE_FORK` 等。

### 1.2 【WALT 接管点】 :7226-7232

```c
if (trace_android_rvh_select_task_rq_fair_enabled() &&
    !(sd_flag & SD_BALANCE_FORK))
        sync_entity_load_avg(&p->se);
trace_android_rvh_select_task_rq_fair(p, prev_cpu, sd_flag,
                wake_flags, &target_cpu);
if (target_cpu >= 0)
        return target_cpu;
```

**这三行是整个函数最重要的一段**：

1. `trace_android_rvh_select_task_rq_fair_enabled()` —— hook 启用时，
   先同步 `p->se` 的 PELT 平均（因为 vendor 回调**可能**会读它）
2. hook 可写入 `target_cpu`
3. **`target_cpu >= 0` 就直接返回**——后续原生逻辑全部跳过

> **[反直觉] 这个 hook 在任何分支之前**，包括 `WF_TTWU` 检查。
> 也就是说 WALT 同时接管了 **wake / fork / exec / idle-balance**
> 四条路径的放置决策，不只是唤醒。
>
> 另外注意 `sync_entity_load_avg()` 是**条件调用**的——
> 只在 WALT 的 hook 启用时才付这个代价。
> 这是 Qualcomm 为避免为 vendor 特性拖慢原生路径做的一处优化。

### 1.3 EAS 快速路径 :7238-7246

```c
if (wake_flags & WF_TTWU) {
        record_wakee(p);

        if (sched_energy_enabled()) {
                new_cpu = find_energy_efficient_cpu(p, prev_cpu, sync);
                if (new_cpu >= 0)
                        return new_cpu;
                new_cpu = prev_cpu;
        }

        want_affine = !wake_wide(p) && cpumask_test_cpu(cpu, p->cpus_ptr);
}
```

**`sched_energy_enabled()` 为真 → 先试 EAS**。返回 `>= 0` 就直接用。

`sched_energy_enabled()` [sched.h:3160](../../kernel/kernel/sched/sched.h#L3160)：

```c
static inline bool sched_energy_enabled(void)
{
        return static_branch_unlikely(&sched_energy_present);
}
```

### 1.4 亲和性路径：`for_each_domain` 遍历 :7252-7270

```c
if (!want_affine) goto no_affine;   /* 概念示意 */
for_each_domain(cpu, tmp) {                      /* 从唤醒 CPU 逐级向上 */
        if (want_affine && (tmp->flags & SD_WAKE_AFFINE) &&
            cpumask_test_cpu(prev_cpu, sched_domain_span(tmp))) {
                if (cpu != prev_cpu)
                        new_cpu = wake_affine(tmp, p, cpu, prev_cpu, sync);
                sd = NULL;
                break;                            /* 首选 wake_affine，不用 slow path */
        }
        if (tmp->flags & sd_flag)
                sd = tmp;
        else if (!want_affine)
                break;
}
```

**第一个同时包含 `this_cpu` 与 `prev_cpu` 且带 `SD_WAKE_AFFINE` 的域**
决定了亲和性判断，然后就 `break`——**把 slow path 的 `sd` 丢掉**
（源码注释: "Prefer wake_affine over balance flags"）。

### 1.5 收尾 :7272-7281

```c
if (unlikely(sd))
        new_cpu = find_idlest_cpu(sd, p, cpu, prev_cpu, sd_flag);   /* 慢路径 */
else if (wake_flags & WF_TTWU)
        new_cpu = select_idle_sibling(p, prev_cpu, new_cpu);         /* 快路径 */

return new_cpu;
```

### 1.6 决策树全景

```
select_task_rq_fair(p, prev_cpu, wake_flags)
 │
 ├─ 【WALT hook】android_rvh_select_task_rq_fair
 │     └─ target_cpu >= 0 ? → 直接返回（WALT 接管）
 │
 ├─ WF_TTWU ？
 │    ├─ sched_energy_enabled() → find_energy_efficient_cpu()
 │    │      └─ 返回值 >= 0 → 直接返回
 │    └─ want_affine = !wake_wide(p) && cpu ∈ p->cpus_ptr
 │
 ├─ for_each_domain(cpu) 向上爬
 │    ├─ 首个含 prev_cpu 的 SD_WAKE_AFFINE 域 → wake_affine() → break
 │    └─ 否则记住带 sd_flag 的最高域为 sd
 │
 └─ sd ？ → find_idlest_cpu()   （慢路径：idle 负载均衡扫描）
    否则 → select_idle_sibling()（快路径：就近找空闲 CPU）
```

---

## 2. EAS：`find_energy_efficient_cpu()`

[fair.c:7019](../../kernel/kernel/sched/fair.c#L7019)（文档注释 :6980-7018）。
**这是原生调度的能耗最优放置**。

### 2.1 前置检查

```c
prev_delta = best_delta = ULONG_MAX;                 /* :7021-7022 */
p_util_min/p_util_max 来自 uclamp                      /* :7022-7023 */
rd = cpu_rq(smp_processor_id())->rd;                 /* :7024 */
sync_entity_load_avg(&p->se);                        /* :7036 */

/* 【WALT hook】:7037-7039 */
if (trace_android_rvh_find_energy_efficient_cpu_enabled()) { ... }
/* 若填入 new_cpu != INT_MAX → 直接返回 */

rcu_read_lock();
pd = rcu_dereference(rd->pd);
if (!pd || READ_ONCE(rd->overutilized))              /* :7043-7044 */
        goto unlock;                                 /* 返回 -1 */
```

> **`rd->overutilized` 是关键门控**：系统过载时 EAS **完全放弃**，
> 退回 `find_idlest_cpu()`。因为此时能耗优化已无意义，保性能优先。
> WALT 有自己的过载判定逻辑，见 [../02-walt/placement.md](../02-walt/04-placement.md)。

**sync 快捷路径** :7046-7052：如果是同步唤醒、`nr_running == 1`、
目标 CPU 允许、且任务放得下 → 直接返回唤醒 CPU。

### 2.2 确定搜索域 :7058-7062

```c
sd = per_cpu(sd_asym_cpucapacity, cpu);
if (sd) {
        while (sd->parent && !cpumask_test_cpu(prev_cpu, sched_domain_span(sd)))
                sd = sd->parent;
        if (!cpumask_test_cpu(prev_cpu, sched_domain_span(sd)))
                goto unlock;
}
```

从 `sd_asym_cpucapacity` 起步向上找**同时包含 `this_cpu` 和 `prev_cpu`
的最小非对称容量域**。找不到就返回 -1。

### 2.3 无 util_est 的任务直接返回 prev_cpu :7064-7067

```c
target = prev_cpu;
if (!task_util_est(p) && p_util_min == 0)
        goto unlock;
```

> **【修正】这一处的行为与上游不同。** 上游（含函数文档注释
> :7009-7017）说 fork 出来的新任务会落到 `find_idlest_cpu()`。
> 但在这个树里 `target` 在 :7064 已被设为 `prev_cpu`，
> `goto unlock` 会**返回 `prev_cpu`**，
> 于是 `select_task_rq_fair()` 在 :7243-7244 直接返回——
> **新任务不经过任何 idle CPU 搜索**。
>
> 【推测】这是 Qualcomm 的有意改动（新任务通常应与父任务同簇），
> 但**函数文档注释没有同步更新**。已在
> [04-open-questions.md](../03-comparison/04-open-questions.md) 登记。

### 2.4 遍历性能域 :7073-7180

```c
for (; pd; pd = pd->next) {
        for_each_cpu_and(cpu, perf_domain_span(pd), sched_domain_span(sd)) {
                if (!cpumask_test_cpu(cpu, p->cpus_ptr)) continue;

                util = cpu_util_next(cpu, p, cpu);
                cpu_cap = capacity_of(cpu);
                spare_cap = cpu_cap - util;              /* lsub_positive */

                /* uclamp 聚合 + util_fits_cpu 检查 */
                if (!util_fits_cpu(util, util_min, util_max, cpu))
                        continue;                        /* :7116 */
                ...
        }
        base_energy_pd = compute_energy(p, -1, pd);      /* :7159 空载能耗 */
        base_energy += base_energy_pd;

        prev_delta = compute_energy(p, prev_cpu, pd) - base_energy_pd;   /* :7163 */
        best_delta = min(best_delta, prev_delta);

        cur_delta = compute_energy(p, max_spare_cap_cpu, pd) - base_energy_pd;  /* :7172 */
        if (cur_delta < best_delta)
                best_energy_cpu = max_spare_cap_cpu;
}
```

**对每个性能域，比较两个候选的能耗增量**：
- `prev_delta` —— 把任务放在 `prev_cpu`
- `cur_delta` —— 放在该域中**剩余容量最大**的 CPU

### 2.5 最终决策：6.25% 阈值 [反直觉]

[fair.c:7192-7194](../../kernel/kernel/sched/fair.c#L7192-L7194)：

```c
if ((prev_delta == ULONG_MAX) ||
    (prev_delta - best_delta) > ((prev_delta + base_energy) >> 4))
        target = best_energy_cpu;
```

> **注释说「at least 6%」但代码是 `>> 4` 即 1/16 = 6.25%。**
>
> 含义：**候选 CPU 必须比 `prev_cpu` 省下超过 6.25% 的能量才值得迁移**。
> 否则留在原地——避免为了微小收益付迁移代价（缓存失效、TLB 失效）。

### 2.6 延迟敏感任务走另一条路

```c
if (latency_sensitive)
        return best_idle_cpu >= 0 ? best_idle_cpu : max_spare_cap_cpu_ls;  /* :7185-7186 */
```

uclamp 标记为 latency-sensitive 的任务**不做能耗比较**，
直接选最优空闲 CPU（按退出延迟 `min_exit_lat` 挑）。

---

## 3. `compute_energy()`

[fair.c:6910](../../kernel/kernel/sched/fair.c#L6910)。

```c
pd_mask = perf_domain_span(pd);                                   /* :6912 */
cpu_cap = arch_scale_cpu_capacity(cpumask_first(pd_mask));        /* :6913 */
_cpu_cap = cpu_cap - arch_scale_thermal_pressure(...);            /* :6919 热压 */

for_each_cpu_and(cpu, pd_mask, cpu_online_mask) {
        util_freq = cpu_util_next(cpu, p, dst_cpu);               /* :6931 */

        if (cpu == dst_cpu)                                       /* :6944-6948 */
                util_running = cpu_util_next(cpu, p, -1) + task_util_est(p);

        cpu_util = effective_cpu_util(cpu, util_running, cpu_cap, ENERGY_UTIL, NULL);
        sum_util += min(cpu_util, _cpu_cap);                      /* :6959 */

        cpu_util = effective_cpu_util(cpu, util_freq, cpu_cap, FREQUENCY_UTIL, tsk);
        max_util = max(max_util, min(cpu_util, _cpu_cap));        /* :6968-6970 */
}

/* 【WALT hook】:6973 */
trace_android_vh_em_cpu_energy(pd->em_pd, max_util, sum_util, &energy);
if (!energy)
        energy = em_cpu_energy(pd->em_pd, max_util, sum_util, _cpu_cap);
```

### 3.1 【关键】两种 util 类型的用法

同一个域里调了**两次** `effective_cpu_util()`：

| 类型 | 用途 | 输入 |
|---|---|---|
| `ENERGY_UTIL` | 累加 `sum_util`（能量正比于 Σutil）| `util_running` |
| `FREQUENCY_UTIL` | 定 `max_util`（决定 OPP / 频率）| `util_freq` |

> **注意 `ENERGY_UTIL` 路径下 `p` 传 NULL**，而 `FREQUENCY_UTIL` 传 `tsk`。
> 因为 uclamp 只影响频率选择，不影响能耗估算。
>
> **【已解决】WALT 会不会通过 `effective_cpu_util` 的 hook 影响
> `ENERGY_UTIL`？—— 不会，两种类型都不影响。**
>
> hook 本身确实**不看 `type`**（[core.c:7328](../../kernel/kernel/sched/core.c#L7328)
> 无条件调用），所以问题只取决于「有没有人注册它」。
> 答案是**全树无注册者**，因此 `ENERGY_UTIL` 和 `FREQUENCY_UTIL`
> 都走原生逻辑，EAS 的能耗估算**不受 WALT 影响**。
>
> 早先版本的本文档把这条列为 `[待确认]`，并推测「WALT 的回调实现里
> 检查了 `type`」——**该推测是错的**：不存在这样的 WALT 回调。
> 验证命令：`grep -rn 'register_trace_android_rvh_effective_cpu_util' .`

### 3.2 `em_cpu_energy()` 的能耗模型

定义在 [energy_model.h:120-200](../../kernel/include/linux/energy_model.h#L120-L200)
（**不是** `.c` 文件，是内联函数）。

```c
if (!sum_util) return 0;                                     /* :128 */

cpu = cpumask_first(to_cpumask(pd->cpus));
scale_cpu = arch_scale_cpu_capacity(cpu);
ps = &pd->table[pd->nr_perf_states - 1];                     /* 最高 OPP */

max_util = map_util_perf(max_util);                          /* ×1.25 */
max_util = min(max_util, allowed_cpu_cap);
freq = map_util_freq(max_util, ps->frequency, scale_cpu);

for (i = 0; i < pd->nr_perf_states; i++) {                   /* 选最低够用的档 */
        ps = &pd->table[i];
        if (ps->frequency >= freq) break;
}

return ps->cost * sum_util / scale_cpu;                      /* :199 */
```

**整个能耗模型就是一行**：`cost × Σutil / scale_cpu`。

### 3.3 `cost` 的含义

`struct em_perf_state` [energy_model.h:21-25](../../kernel/include/linux/energy_model.h#L21-L25)：
`frequency`、`power`、`cost`。

`cost = power × max_frequency / frequency`——
即**每单位频率的功耗**。计算在
[energy_model.c:155-166](../../kernel/kernel/power/energy_model.c#L155-L166)。

> **【本树没有的符号】** `em_pd_energy()` 在本树**不存在**——
> 那是 5.16+ 把 EM 设备通用化后的改名。本树的 API 只有 `em_cpu_energy()`。
> 另外**没有 `kernel/sched/energy.c` 这个文件**，
> EM 代码在 `include/linux/energy_model.h` + `kernel/power/energy_model.c`。

---

## 4. 性能域（perf domain）的构建与 EAS 启用

### 4.1 `build_perf_domains()`

[topology.c:355](../../kernel/kernel/sched/topology.c#L355)。**四道门，全过才启用 EAS**：

```c
if (!sysctl_sched_energy_aware) goto free;                    /* :363 */
trace_android_rvh_build_perf_domains(&eas_check);             /* :370 【WALT hook】*/
if (!per_cpu(sd_asym_cpucapacity, cpu) && !eas_check) goto free;  /* :371 */
if (sched_smt_active()) goto free;                            /* :380 */
if (!arch_scale_freq_invariant()) goto free;                  /* :386 */
```

| 条件 | 含义 |
|---|---|
| `sysctl_sched_energy_aware` | 总开关，**默认 1** [topology.c:215](../../kernel/kernel/sched/topology.c#L215) |
| `sd_asym_cpucapacity` | **必须是非对称容量拓扑**（big.LITTLE）|
| `!sched_smt_active()` | 不支持 SMT |
| `arch_scale_freq_invariant()` | **必须支持频率不变性** |

然后复杂度门 :415：

```c
if (nr_pd * (nr_ps + nr_cpus) > EM_MAX_COMPLEXITY) goto free;
```

`EM_MAX_COMPLEXITY = 2048`（[topology.c:353](../../kernel/kernel/sched/topology.c#L353)）。

> **`android_rvh_build_perf_domains` 是 WALT 的 hook**——
> 它可以通过 `eas_check` **绕过 `sd_asym_cpucapacity` 检查**
> （:371 的 `&& !eas_check`）。这是高通强制启用 EAS 的后门。

### 4.2 数据结构

| 结构 | 位置 | 说明 |
|---|---|---|
| `struct em_perf_domain` | [energy_model.h:44](../../kernel/include/linux/energy_model.h#L44) | `table` / `nr_perf_states` / `cpus[]` |
| `struct perf_domain`（调度侧）| [sched.h:788](../../kernel/kernel/sched/sched.h#L788) | `em_pd` / `next` / `rcu` |
| `root_domain.pd` | [sched.h:867](../../kernel/kernel/sched/sched.h#L867) | `struct perf_domain __rcu *pd` |

`root_domain.pd` 是一条 **NULL 结尾的 RCU 链表**——
这就是 `find_energy_efficient_cpu()` 里 `for (; pd; pd = pd->next)` 的迭代对象。

---

## 5. 快路径：`select_idle_sibling()`（概览）

[fair.c:6619](../../kernel/kernel/sched/fair.c#L6619)。EAS 未启用或返回 -1 时使用。

按顺序尝试：

| 顺序 | 候选 | 位置 |
|---:|---|---|
| 1 | `target` 空闲且放得下 | :6642-6644 |
| 2 | `prev`（与 `target` 共享缓存、空闲、放得下）| :6649-6652 |
| 3 | `prev`（per-cpu kthread 堆叠场景）| :6662-6668 |
| 4 | `p->recent_used_cpu` | :6671-6680 |
| 5 | 非对称：`select_idle_capacity()` | :6697 |
| 6 | SMT：`select_idle_smt()` | :6710 |
| 7 | `select_idle_cpu()` | :6716 |

### 5.1 `recent_used_cpu`

[include/linux/sched.h:781](../../kernel/include/linux/sched.h#L781)。
**每次调用都被立即更新**（读过之后立刻写 `prev`）：

```c
recent_used_cpu = p->recent_used_cpu;
p->recent_used_cpu = prev;
```

**这是「记住上次真正空闲的那个 CPU」的启发式**，避免每轮都重扫。

### 5.2 `SIS_PROP`：扫描预算

`select_idle_cpu()` [fair.c:6493](../../kernel/kernel/sched/fair.c#L6493)
不是无脑遍历所有 CPU，而是按**预估收益**算一个扫描上限：

```c
span_avg = sd->span_weight * this_rq->wake_avg_idle;
avg_cost = this_sd->avg_scan_cost + 1;
nr = (span_avg > 4 * avg_cost) ? div_u64(span_avg, avg_cost) : 4;
```

扫到 `nr` 个还没找到就放弃（返回 -1，退化为用 `target`）。

> **`wake_avg_idle` 是「最近唤醒的平均空闲时长」**，随每次唤醒更新
> （:6517-6522、:6554-6564）。这是一个**自适应的搜索代价模型**。

---

## 6. 亲和性：`wake_affine()`

[fair.c:6213](../../kernel/kernel/sched/fair.c#L6213)。由 `wake_wide()` 门控。

### 6.1 `wake_wide()`：判断唤醒是否「宽」

[fair.c:6118](../../kernel/kernel/sched/fair.c#L6118)：

```c
master = current->wakee_flips;
slave = p->wakee_flips;
factor = __this_cpu_read(sd_llc_size);
if (master < slave) swap(master, slave);
if (slave < factor || master < slave * factor) return 0;   /* 不宽 */
return 1;                                                   /* 宽 */
```

**基于「唤醒翻转次数」的启发式**：如果唤醒关系在一对任务间反复横跳，
说明它们没有稳定的亲和性，就不要强行拉到同一 CPU。

### 6.2 两级判断

```c
if (sched_feat(WA_IDLE) && !wake_affine_idle(...))      /* :6218 */
        ...
if (sched_feat(WA_WEIGHT) && !wake_affine_weight(...))  /* :6221 */
        ...
if (new_cpu == nr_cpumask_bits) return prev_cpu;        /* :6225-6226 无结论 */
return new_cpu;
```

`wake_affine_weight()` 的 **sync 偏置** [fair.c:6207-6208](../../kernel/kernel/sched/fair.c#L6207-L6208)：

```c
if (sync)
        prev_eff_load += 1;      /* 平局时让 wakee 贴着 waker */
```

> **`nr_cpumask_bits` 作为「无结论」哨兵值**——
> 因为它是合法 CPU 编号之外的最小值。这种用法在 fair.c 里反复出现。

---

## 7. 重要 tunable

| tunable | 默认 | 位置 |
|---|---|---|
| `sysctl_sched_energy_aware` | **1** | [topology.c:215](../../kernel/kernel/sched/topology.c#L215) |
| `sysctl_sched_nr_migrate` | 32 | [core.c:92](../../kernel/kernel/sched/core.c#L92) |
| `sysctl_sched_wakeup_granularity` | 1000000 ns | [fair.c:87](../../kernel/kernel/sched/fair.c#L87) |
| `EM_MAX_COMPLEXITY` | 2048 | [topology.c:353](../../kernel/kernel/sched/topology.c#L353) |
| `UTIL_EST_MARGIN` | 10 | [fair.c:4010](../../kernel/kernel/sched/fair.c#L4010) |

> **`sched_util_freq_margin` 在本树不存在**——已全树 grep 确认。
> （与 [schedutil.md §7.1](03-schedutil.md#71-本树没有的-tunable) 是同一个结论。）
>
> **`sysctl_sched_wakeup_granularity` 与放置无关**——它用于
> `check_preempt_wakeup()`（[fair.c:7363](../../kernel/kernel/sched/fair.c#L7363)）的
> **抢占粒度**。不要把它和放置决策混为一谈。

---

## 8. 本路径上的 Android/WALT hook

| hook | 位置 | 能力 |
|---|---|---|
| `android_rvh_select_task_rq_fair` | [fair.c:7229](../../kernel/kernel/sched/fair.c#L7229) | **完全接管放置**（WALT 用）|
| `android_rvh_find_energy_efficient_cpu` | [fair.c:7037](../../kernel/kernel/sched/fair.c#L7037) | 接管 EAS 决策 |
| `android_rvh_build_perf_domains` | [topology.c:370](../../kernel/kernel/sched/topology.c#L370) | 强制启用 EAS |
| `android_vh_em_cpu_energy` | [fair.c:6973](../../kernel/kernel/sched/fair.c#L6973) | 覆盖能耗计算 |
| `android_rvh_effective_cpu_util` | [core.c:7328](../../kernel/kernel/sched/core.c#L7328) | 覆盖 util（影响 EAS）；**本树无注册者** ❌ |

**五道接管点**，其中第一个（`select_task_rq_fair`）使其余四个在
WALT 启用时基本用不上——因为整个函数提前返回了。

> 第五道（`android_rvh_effective_cpu_util`）是**另一回事**：
> 它不是「被第一个取代」，而是**从来就没注册过**。
> 注意区分这两种「用不上」——前者是设计使然，后者是配置缺失。

---

## 9. 相关文档

- WALT 的放置实现 → [../02-walt/placement.md](../02-walt/04-placement.md)
- 负载均衡 → [load-balance.md](05-load-balance.md)
- PELT（`task_util_est` 的来源）→ [pelt.md](02-pelt.md)
- 调度框架 → [sched-framework.md](01-sched-framework.md)
