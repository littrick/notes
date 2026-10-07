# WALT 选核（placement）

> **源码**：[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)、[walt.h](../../kernel/kernel/sched/walt/walt.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17
> 原生 EAS 对照见 [../01-baseline/placement-eas.md](../01-baseline/04-placement-eas.md)。
> 逐函数差异见 [../03-comparison/base-vs-walt.md](../03-comparison/01-base-vs-walt.md)。

---

## 1. 接管点

WALT 通过**单个 vendor hook 完全替换** `select_task_rq_fair()`：

```c
void walt_cfs_init(void)                                       /* walt_cfs.c:1546 */
{
        register_trace_android_rvh_select_task_rq_fair(walt_select_task_rq_fair, NULL);  /* :1548 */
        ...
}
```

```c
static void
walt_select_task_rq_fair(void *unused, struct task_struct *p, int prev_cpu,
                         int sd_flag, int wake_flags, int *target_cpu)   /* :1149 */
{
        int sync;
        int sibling_count_hint;

        if (unlikely(walt_disabled))
                return;                                          /* ← 不改 target_cpu，原生逻辑继续 */

        sync = (wake_flags & WF_SYNC) && !(current->flags & PF_EXITING);
        sibling_count_hint = p->wake_q_count;
        p->wake_q_count = 0;

        *target_cpu = walt_find_energy_efficient_cpu(p, prev_cpu, sync, sibling_count_hint);
}
```

**这是 `DECLARE_RESTRICTED_HOOK`（单回调），WALT 是唯一注册者。**
挂钩位置在 [fair.c:7229](../../kernel/kernel/sched/fair.c#L7229)，
运行在 `select_task_rq_fair()` 的**最开头，早于任何分支**——
包括 `WF_TTWU` 判定（见 [placement-eas.md §2](../01-baseline/04-placement-eas.md#2-select_task_rq_fair-的决策树)）。

> **`walt_disabled` 时直接 `return`**，不改 `*target_cpu`——
> 这依赖 hook 框架的约定：hook 返回后若 `*target_cpu` 未被改写，
> 原生逻辑继续。**这是 WALT 的全局开关，所有 hook 入口都做这个检查。**

注意 WALT **忽略了 `sd_flag`**。原生 EAS 用 `sd_flag & SD_BALANCE_WAKE`
区分「唤醒放置」和「fork/exec 放置」，WALT 的 `walt_find_energy_efficient_cpu()`
不接收这个参数——**它对所有放置场景用同一套逻辑**。

---

## 2. 三层结构

```
walt_select_task_rq_fair()                    walt_cfs.c:1149
  └─ 抽 sync / sibling_count_hint
     └─ walt_find_energy_efficient_cpu()      walt_cfs.c:933   ← 决策主体
          ├─ 【早退】many_wakeup / cpu_array 未就绪
          ├─ 【fastpath】pipeline_cpu         :977-989
          ├─ 【fastpath】sync wakeup          :1013-1018
          ├─ walt_get_indicies()              :221   ← 算 order/end/ignore/energy_eval
          ├─ walt_find_best_target()          :370   ← 选候选集 candidates
          ├─ 【fastpath】CLUSTER_PACKING      :1043
          ├─ 【早退】weight==1 / need_idle    :1048-1058
          ├─ 【早退】!energy_eval_needed      :1060-1069  ← 按 spare cap 选
          └─ for_each_cpu(candidates)          :1092
                └─ walt_compute_energy()      :862   ← 逐候选算能量
                     └─ walt_pd_compute_energy → walt_em_cpu_energy
```

**关键分工**：

| 层 | 职责 | 手段 |
|---|---|---|
| `walt_find_best_target()` | **粗筛**——挑出 1~N 个候选 CPU | 启发式（spare cap / idle latency / nr_running）|
| `walt_compute_energy()` | **精算**——对候选算能量 | EM 模型的 `walt_em_cpu_energy()` |

**这与原生 EAS 的结构一致**（`find_energy_efficient_cpu()` 也只对候选算能量），
但 WALT 的粗筛复杂得多——它引入了一套**簇的顺序索引**。

---

## 3. 簇顺序索引：`cpu_array`

### 3.1 `walt_get_indicies()`

walt_cfs.c:221。**决定「从哪个簇开始找」和「找到哪个簇为止」。**

```c
static void walt_get_indicies(struct task_struct *p, int *order_index,
                int *end_index, int per_task_boost, bool is_uclamp_boosted,
                bool *energy_eval_needed, bool *ignore_cluster)
{
        *order_index = 0;
        *end_index = 0;

        if (num_sched_clusters <= 1)
                return;

        /* ① 强 boost 任务：直接从最大簇开始，跳过能量评估 */
        if (per_task_boost > TASK_BOOST_ON_MID) {
                *order_index = num_sched_clusters - 1;
                *energy_eval_needed = false;
                return;
        }

        /* ② full throttle boost */
        if (is_full_throttle_boost()) {
                *energy_eval_needed = false;
                *order_index = num_sched_clusters - 1;
                if ((*order_index > 1) && task_demand_fits(p,
                    cpumask_first(&cpu_array[*order_index][1])))
                        *end_index = 1;
                return;
        }

        /* ③ uclamp boost / per-task boost / ON_BIG / skip_min */
        if (is_uclamp_boosted || per_task_boost ||
            task_boost_policy(p) == SCHED_BOOST_ON_BIG ||
            walt_task_skip_min_cpu(p)) {
                *energy_eval_needed = false;
                *order_index = 1;

                /* 跳过被 ignore 的簇 */
                i = 0;
                while (*order_index + i <= num_sched_clusters - 1) {
                        if (!ignore_cluster[*order_index + i]) break;
                        i++;
                }
                *order_index = *order_index + i;
                ...
        }

        if (i <= num_sched_clusters - 2)
                *end_index = i;

        if (p->in_iowait && task_in_related_thread_group(p))
                *energy_eval_needed = false;
}
```

### 3.2 输出的三个索引

| 输出 | 含义 |
|---|---|
| `order_index` | **起始簇序号**（0 = 最小簇）|
| `end_index` | **终止簇序号**（`walt_find_best_target` 扫到这个簇就停）|
| `energy_eval_needed` | 是否值得做能量精算 |
| `ignore_cluster[]` | 每簇是否被 `ignore_cluster_valid()` 排除 |

**四种任务的起点**：

| 任务类型 | `order_index` | `energy_eval_needed` |
|---|---|---|
| 普通任务 | 0（最小簇起）| **true** |
| `TASK_BOOST_ON_MAX` / `STRICT_MAX` | 最大簇 | false |
| Full throttle boost | 最大簇（可能 `end_index=1` 收窄）| false |
| uclamp boost / per-task boost / ON_BIG / skip_min | **1（跳过最小簇）** | false |

> **`per_task_boost > TASK_BOOST_ON_MID` 而不是 `>=`。**
> `TASK_BOOST_NONE=0, ON_MID=1, ON_MAX=2, STRICT_MAX=3, END=4`
> （[walt.h:32-38](../../kernel/include/linux/sched/walt.h#L32-L38)）。
> 所以 `ON_MID` **不**走强 boost 分支——它落到分支 ③
> （因为 `per_task_boost` 非零）。**只有 `ON_MAX` 和 `STRICT_MAX`
> 才从最大簇开始。** 见 [boost.md](08-boost.md)。

### 3.3 `cpu_array`

```c
extern int sysctl_cluster_arr[3][15];
```

`cpu_array[][]` 是**按「频率-容量关系」排好的簇序表**，
不再是简单的「小→中→大」。构造逻辑用 `sysctl_cluster_arr`
（每个大簇配一组可以替代它的中簇）。

`walt_get_indicies()` 里的注释（:255-262）把 ignore 的情形画了出来：

```
G S -> Since G cannot have relationship exit with i = 0
G P S -> Enter loop and exit with i = 1.
G T P S -> here we will exit with i = 1 OR 2 (if T also needs to be ignored).
```

即**从 `order_index=1` 开始逐个跳过被 ignore 的簇**。

`MIN_UTIL_FOR_ENERGY_EVAL = 52`（walt_cfs.c:220）——
util 低于此值的任务不值得做能量精算。**注意：本函数里没有直接使用它**，
它用在别处（见 §5）。

---

## 4. 四条 fastpath

`walt_find_energy_efficient_cpu()` 里有**四处早退**，都能跳过整个搜索。
`fbt_env.fastpath` 记录走了哪条（[枚举 walt_cfs.c:325](../../kernel/kernel/sched/walt/walt_cfs.c#L325)）：

```c
enum fastpaths {
        NONE = 0,
        SYNC_WAKEUP,
        PREV_CPU_FASTPATH,
        CLUSTER_PACKING_FASTPATH,
        PIPELINE_FASTPATH,
};
```

| fastpath | 触发 | 位置 | 结果 |
|---|---|---|---|
| **many_wakeup** | `walt_is_many_wakeup(sibling_count_hint) && prev_cpu != cpu && prev_cpu ∈ p->cpus_ptr` | :960-962 | 直接返回 `prev_cpu` |
| **PIPELINE** | 有 `pipeline_cpu` 且 5 项检查全过 | :977-989 | 返回 `pipeline_cpu` |
| **SYNC_WAKEUP** | `sysctl_sched_sync_hint_enable && sync && bias_to_this_cpu(...) && !cpu_halted(cpu)` | :1013-1018 | 返回当前 CPU |
| **CLUSTER_PACKING** | `walt_find_and_choose_cluster_packing_cpu()` 返回 ≥0 | :417-422, :1043-1046 | 返回打包 CPU |
| **PREV_CPU** | prev 与 start 同簇（或 asym 兄弟）且 prev 空闲 | :425-435 | 候选集 = {prev_cpu} |

### 4.1 many_wakeup（:960-962）

```c
if (walt_is_many_wakeup(sibling_count_hint) && prev_cpu != cpu &&
    cpumask_test_cpu(prev_cpu, p->cpus_ptr))
        return prev_cpu;
```

```c
static inline bool walt_is_many_wakeup(int sibling_count_hint)   /* :153 */
{
        return sibling_count_hint >= sysctl_sched_many_wakeup_threshold;
}
```

**`sibling_count_hint` 来自 `p->wake_q_count`**（:1159）——
在 `walt_select_task_rq_fair()` 里被读出后**清零**。
它是「本任务最近被唤醒的频率」的计数。频繁唤醒的任务（如 binder
往返的线程对）**直接钉在 prev_cpu**，避免反复迁移。

### 4.2 PIPELINE fastpath（:977-989）

```c
if ((wts->low_latency & WALT_LOW_LATENCY_MASK) &&
    (pipeline_cpu != -1) &&
    walt_task_skip_min_cpu(p) &&
    cpumask_test_cpu(pipeline_cpu, p->cpus_ptr) &&
    cpu_active(pipeline_cpu) &&
    !cpu_halted(pipeline_cpu) &&
    !ignore_cluster[cpu_cluster(pipeline_cpu)->id]) {
        if (!walt_pipeline_low_latency_task(cpu_rq(pipeline_cpu)->curr)) {
                best_energy_cpu = pipeline_cpu;
                fbt_env.fastpath = PIPELINE_FASTPATH;
                goto out;
        }
}
```

**六项前置检查**，最后一项是「目标 CPU 当前跑的任务**不是**另一个
pipeline 低延迟任务」——即**每个 CPU 最多放一个 pipeline 任务**。

`pipeline_cpu` 由 heavy-task rearrangement 分配，见
[groups-and-clusters.md](06-groups-and-clusters.md)。

### 4.3 SYNC_WAKEUP（:1013-1018）

```c
if (sysctl_sched_sync_hint_enable && sync
    && bias_to_this_cpu(p, cpu, start_cpu) && !cpu_halted(cpu)) {
        best_energy_cpu = cpu;
        fbt_env.fastpath = SYNC_WAKEUP;
        goto unlock;
}
```

`bias_to_this_cpu()`（:65）：

```c
bool base_test = cpumask_test_cpu(cpu, p->cpus_ptr) && cpu_active(cpu);
bool start_cap_test = (wrq->cluster->id >= start_wrq->cluster->id);
return base_test && start_cap_test;
```

**「当前 CPU 的簇不低于起始簇」**——防止把任务 sync-wakeup 到一个
比它需要的还小的簇上。

但在此之前有一段**让 `sync` 失效**的逻辑（:1009-1011）：

```c
if (sync && (need_idle || (is_rtg && curr_is_rtg) ||
             ignore_cluster[cpu_cluster(cpu)->id]))
        sync = 0;
```

**RTG 任务被同组任务唤醒时不做 sync 亲和**——因为要让它们**共置**
（colocation）到同一个 CPU，而不是一个跑一个等。见
[groups-and-clusters.md](06-groups-and-clusters.md)。

### 4.4 PREV_CPU fastpath（:425-435）

```c
if (((prev_wrq->cluster->id == start_wrq->cluster->id) ||
     asym_cap_siblings(prev_cpu, start_cpu)) &&
     cpu_active(prev_cpu) &&
     available_idle_cpu(prev_cpu) &&
     cpumask_test_cpu(prev_cpu, p->cpus_ptr) &&
     !cpu_halted(prev_cpu)) {
        fbt_env->fastpath = PREV_CPU_FASTPATH;
        cpumask_set_cpu(prev_cpu, candidates);
        goto out;
}
```

**prev_cpu 空闲且簇不低于起始簇 → 直接用它**，不做任何能量评估。

---

## 5. `walt_find_best_target()`：粗筛

walt_cfs.c:370。**逐簇扫描，每簇选出至多一个代表，放进 `candidates`。**

### 5.1 外层：簇循环

```c
for (cluster = 0; cluster < num_sched_clusters; cluster++) {   /* :438 */
        rq = cpu_rq(cpumask_first(&cpu_array[order_index][cluster]));
        wrq = ...;

        /* ignore_cluster 的两趟扫描 */
        if ((!scan_ignore_cluster && ignore_cluster[wrq->cluster->id])
            || (scan_ignore_cluster && !ignore_cluster[wrq->cluster->id])) {
                ignored = true;
                continue;
        }

        target_max_spare_cap = 0;      /* 每簇重置 */
        min_exit_latency = INT_MAX;
        best_idle_cuml_util = ULONG_MAX;

        cpumask_and(&visit_cpus, p->cpus_ptr, &cpu_array[order_index][cluster]);
        for_each_cpu(i, &visit_cpus) { ... }                    /* :466 */

        if (best_idle_cpu_cluster != -1)
                cpumask_set_cpu(best_idle_cpu_cluster, candidates);   /* :599 */
        else if (target_cpu_cluster != -1)
                cpumask_set_cpu(target_cpu_cluster, candidates);      /* :601 */

        if ((cluster >= end_index) && (!cpumask_empty(candidates)) &&
            walt_target_ok(target_cpu_cluster, order_index))          /* :603 */
                break;

        if (most_spare_cap_cpu != -1 && cluster >= stop_index)
                break;                                                /* :607 */
}
```

**每簇只放一个候选**——空闲的优先，否则放「spare cap 最大」的。
`end_index` 到了且有候选就停。

### 5.2 内层：逐 CPU 评分

```c
fbt_env->prs[i] = wrq->prev_runnable_sum + wrq->grp_time.prev_runnable_sum;  /* :476 */

if (walt_should_reject_fbt_cpu(wrq, p, i, order_index, fbt_env))
        continue;                                             /* :478-479 */

wake_cpu_util = cpu_util_without(i, p);                       /* :486 */
spare_wake_cap = capacity_orig - wake_cpu_util;               /* :487 */

if (spare_wake_cap > most_spare_wake_cap) { ... }             /* :489-492 */

if (cpu_rq(i)->nr_running < cpu_rq_runnable_cnt) { ... }      /* :499-502 */

new_cpu_util = wake_cpu_util + min_task_util;
if (new_cpu_util > capacity_orig)
        continue;                                             /* :510-511 */

if (available_idle_cpu(i)) {
        idle_exit_latency = walt_get_idle_exit_latency(cpu_rq(i));   /* :534 */
        this_complex_idle = is_complex_sibling_idle(i) ? 1 : 0;      /* :536 */
        if (this_complex_idle < best_complex_idle) continue;         /* :538 */
        if (idle_exit_latency > min_exit_latency)  continue;         /* :544 */
        ...
        best_idle_cpu_cluster = i;                                   /* :556 */
        continue;
}

if (best_idle_cpu_cluster != -1) continue;                           /* :562 */

spare_cap = capacity_orig - new_cpu_util;                            /* :570 */

if (rtg_high_prio_task) {
        if (walt_nr_rtg_high_prio(i) > target_nr_rtg_high_prio) continue;   /* :580 */
        if (walt_nr_rtg_high_prio(i) == target_nr_rtg_high_prio &&
            spare_cap < target_max_spare_cap) continue;                     /* :584 */
} else {
        if (spare_cap < target_max_spare_cap) continue;                     /* :589 */
}

target_max_spare_cap = spare_cap;
target_nr_rtg_high_prio = walt_nr_rtg_high_prio(i);
target_cpu_cluster = i;
```

**几个要点**：

- **`cpu_util_without(i, p)`**：扣除 `p` 自身的 util（如果它当前在 `i` 上），
  避免「因为有我所以显得忙」的自我计算。
- **`spare_wake_cap` 与 `spare_cap` 是两个不同的量**：
  `spare_wake_cap`（:487）只看唤醒后的空闲度，
  `spare_cap`（:570）还要算上 `min_task_util`（任务的最小需求）。
  **只有后者参与最终评分**；前者仅供 `most_spare_cap_cpu` 兜底用。
- **空闲 CPU 一旦找到，就不再考虑忙 CPU**（:562）——「空闲优先」是硬规则。
- **`is_complex_sibling_idle`**：L2 兄弟核是否空闲。让同 L2 的两个核
  一忙一闲优于同忙或同闲。

### 5.3 `walt_should_reject_fbt_cpu()`

walt_cfs.c:340。**六条否决**：

```c
if (!cpu_active(cpu))                          return true;   /* :344 */
if (cpu_halted(cpu))                           return true;   /* :347 */
if (is_reserved(cpu))                          return true;   /* :354 */
if (sched_cpu_high_irqload(cpu))               return true;   /* :357 */
if (fbt_env->skip_cpu == cpu)                  return true;   /* :360 */
if (wrq->num_mvp_tasks > 0 &&
    per_task_boost(p) != TASK_BOOST_STRICT_MAX) return true;   /* :363 */
return false;
```

| 否决 | 含义 |
|---|---|
| 非 active | 热插拔 |
| **halted** | 被 `core_ctl` 停核 → [power-side.md](07-power-side.md) |
| `is_reserved` | 有未完成的 active migration 目标 |
| `sched_cpu_high_irqload` | 中断负载过高 |
| `skip_cpu` | many-wakeup 时要避开当前 CPU |
| **MVP** | 目标 CPU 上有 MVP 任务，且本任务不是 STRICT_MAX |

> **最后一条是 WALT 特有的「MVP 独占」语义**：只要 CPU 上有 MVP 任务，
> 普通任务（非 STRICT_MAX boost）**不允许放进来**。
> 见 [rt-mvp.md](09-rt-mvp.md)。

### 5.4 兜底阶梯

```c
if (unlikely(cpumask_empty(candidates))) {                     /* :633 */
        if (most_spare_cap_cpu != -1)
                cpumask_set_cpu(most_spare_cap_cpu, candidates);          /* :635 */
        else if (cpu_active(prev_cpu)
                 && (cpu_rq(prev_cpu)->nr_running < DIRE_STRAITS_PREV_NR_LIMIT))
                cpumask_set_cpu(prev_cpu, candidates);                    /* :638 */
        else if (least_nr_cpu != -1)
                cpumask_set_cpu(least_nr_cpu, candidates);                /* :640 */
}
```

**三级兜底**：最空闲 → prev_cpu（若 runnable 数 < 10）→ 最少 runnable。

`DIRE_STRAITS_PREV_NR_LIMIT = 10`（:369）。**这是防止「任务粘在
prev_cpu 上把它压垮」的最后一道闸**——prev_cpu 上超过 10 个可运行任务
就不再回退到它。

另有一趟 **ignore_cluster 重扫**（:611-622）：如果所有候选簇都被
ignore 且没找到候选，就 `scan_ignore_cluster = true` 重来一趟，
这次**只扫被 ignore 的簇**。

---

## 6. 能量精算

### 6.1 候选的两种归宿

回到 `walt_find_energy_efficient_cpu()`：

```c
weight = cpumask_weight(candidates);                            /* :1037 */
if (!weight) goto unlock;

first_cpu = cpumask_first(candidates);

if (fbt_env.fastpath == CLUSTER_PACKING_FASTPATH) {             /* :1043 */
        best_energy_cpu = first_cpu;
        goto unlock;
}

if (weight == 1) {                                              /* :1048 */
        if (available_idle_cpu(first_cpu) || first_cpu == prev_cpu) {
                best_energy_cpu = first_cpu;
                goto unlock;
        }
}

if (need_idle && available_idle_cpu(first_cpu)) {               /* :1055 */
        best_energy_cpu = first_cpu;
        goto unlock;
}

/* ★ 不做能量评估：直接选 spare capacity 最大的 */
if (!energy_eval_needed) {                                      /* :1060 */
        int max_spare_cpu = first_cpu;

        for_each_cpu(cpu, candidates) {
                if (capacity_spare_of(max_spare_cpu) < capacity_spare_of(cpu))
                        max_spare_cpu = cpu;
        }
        best_energy_cpu = max_spare_cpu;
        goto unlock;
}
```

`capacity_spare_of()`（:927）：

```c
static inline unsigned int capacity_spare_of(int cpu)
{
        return capacity_orig_of(cpu) - cpu_util(cpu);
}
```

> **这是 WALT 与原生 EAS 最大的行为差异之一。**
> 原生 EAS **总是**算能量（`cpu_util()` 低于阈值时也走 `cpumask_any` 兜底）。
> WALT 在 `energy_eval_needed == false` 时**完全不算能量**，
> 改用纯启发式的「spare capacity 最大」。
>
> `energy_eval_needed` 为 false 的场景：boost 任务、uclamp boost、
> `skip_min` 任务、RTG 的 iowait 任务（§3.1）。
> **这些都是「已经知道该去大核」的情况——算能量是浪费。**

### 6.2 逐候选算能量

```c
if (READ_ONCE(p->__state) == TASK_WAKING)
        delta = task_util(p);                                   /* :1071-1072 */

if (cpumask_test_cpu(prev_cpu, p->cpus_ptr) && !__cpu_overutilized(prev_cpu, delta) &&
    !ignore_cluster[cpu_cluster(prev_cpu)->id]) {               /* :1074 */
        prev_energy = walt_compute_energy(p, prev_cpu, pd, candidates, fbt_env.prs, &output);
        best_energy = prev_energy;
} else {
        prev_energy = best_energy = ULONG_MAX;                  /* :1088 */
}

for_each_cpu(cpu, candidates) {                                 /* :1092 */
        if (cpu == prev_cpu) continue;

        cur_energy = walt_compute_energy(p, cpu, pd, candidates, fbt_env.prs, &output);

        if (cur_energy < best_energy) {
                best_energy = cur_energy;
                best_energy_cpu = cpu;
        } else if (cur_energy == best_energy) {
                if (select_cpu_same_energy(cpu, best_energy_cpu, prev_cpu)) {
                        best_energy = cur_energy;
                        best_energy_cpu = cpu;
                }
        }
        ...
}
```

**`prev_cpu` 先算并作为基准**，然后逐个候选比较。

**`best_energy = ULONG_MAX` 的含义**（:1088）：如果 `prev_cpu` 不可用
（不在亲和集里、已 overutilized、或被 ignore），那么**任何候选都优于
prev**——第一个候选立即成为 `best_energy_cpu`。

> **【反直觉】`prev_cpu` 被忽略时基准被设成 `ULONG_MAX`，
> 而不是「不参与比较」。** 效果是「只要有一个候选，就选它」——
> 这对「任务必须离开 prev_cpu」的场景是正确且高效的。

### 6.3 `walt_compute_energy()`

walt_cfs.c:862。

```c
static inline long
walt_compute_energy(struct task_struct *p, int dst_cpu, struct perf_domain *pd,
                cpumask_t *candidates, u64 *prs, struct compute_energy_output *output)
{
        long energy = 0;
        unsigned int x = 0;

        for (; pd; pd = pd->next) {
                struct cpumask *pd_mask = perf_domain_span(pd);

                if (cpumask_intersects(candidates, pd_mask)
                    || cpumask_test_cpu(task_cpu(p), pd_mask)) {
                        energy += walt_pd_compute_energy(p, dst_cpu, pd, prs, output, x);
                        x++;
                }
        }

        return energy;
}
```

**只对「有候选的 perf domain」和「任务当前所在 domain」算能量**——
其它 domain 不可能被选中，跳过。

**`prs` 是性能域的 `prev_runnable_sum`**（:476 采集），
传给 `walt_pd_compute_energy()` → `walt_em_cpu_energy()`。
**这是 WALT 用自己的窗口统计替代 PELT `util_avg` 的地方。**

### 6.4 同能量时的选优

`select_cpu_same_energy()`（:891）：

```c
if (best_wrq->cluster->id < wrq->cluster->id)  return false;   /* 偏好大簇 */
if (wrq->cluster->id < best_wrq->cluster->id)  return true;

if (best_cpu_is_idle && walt_get_idle_exit_latency(cpu_rq(best_cpu)) <= 1)
        return false;                                          /* best 已是最浅 idle */
if (new_cpu_is_idle && walt_get_idle_exit_latency(cpu_rq(cpu)) <= 1)
        return true;                                           /* new 是最浅 idle */

if (best_cpu_is_idle && !new_cpu_is_idle)  return false;       /* 偏好 idle */
if (new_cpu_is_idle && !best_cpu_is_idle)  return true;

if (best_cpu == prev_cpu)  return false;                       /* 偏好 prev */
if (cpu == prev_cpu)       return true;

if (best_cpu_is_idle && new_cpu_is_idle)  return false;
if (cpu_util(best_cpu) <= cpu_util(cpu))  return false;        /* 偏好 util 低的 */

return true;
```

**优先级链**：大簇 → 浅 idle → idle → prev_cpu → util 低。

> **`walt_get_idle_exit_latency(...) <= 1` 是「最浅 idle」的判定**——
> 退出延迟 ≤ 1 说明几乎立刻可用（如 WFI）。

---

## 7. 6% 回退规则

```c
/*
 * Pick the prev CPU, if best energy CPU can't saves at least 6% of
 * the energy used by prev_cpu.
 */
if (!(available_idle_cpu(best_energy_cpu) &&
    walt_get_idle_exit_latency(cpu_rq(best_energy_cpu)) <= 1) &&
    (prev_energy != ULONG_MAX) && (best_energy_cpu != prev_cpu) &&
    ((prev_energy - best_energy) <= prev_energy >> 5) &&
    (prev_wrq->cluster->id <= start_wrq->cluster->id))
        best_energy_cpu = prev_cpu;                            /* :1124-1129 */
```

**五条全部成立才回退到 `prev_cpu`**：

| 条件 | 含义 |
|---|---|
| `!(best 是最浅 idle)` | 如果 best 是立刻可用的空闲核，**不回退**（性能优先）|
| `prev_energy != ULONG_MAX` | prev 确实算过能量 |
| `best_energy_cpu != prev_cpu` | 本来就选的 prev |
| `(prev - best) <= prev >> 5` | **节省不到 1/32 ≈ 3.1%** |
| `prev_wrq->cluster->id <= start_wrq->cluster->id` | prev 的簇不高于起始簇 |

> **注意注释与实际代码的差异**：注释写「at least 6%」，
> 但 `prev_energy >> 5` 是 **1/32 ≈ 3.125%**，不是 6%。
> **6% 对应的是 `>> 4`（1/16）。**
>
> 对比原生 EAS 的 `find_energy_efficient_cpu()`，那里的判定是
> `(prev_energy - best_energy) * 16 < prev_energy`（即 1/16 = 6.25%，
> 见 [placement-eas.md §3](../01-baseline/04-placement-eas.md#3-find_energy_efficient_cpu)）。
>
> **【本树的一个注释-代码不一致】**：WALT 的注释声称 6%（与上游一致），
> 但实际阈值放宽到了约 3.1%。效果是**更愿意迁移**——
> 只要省一点点能量就搬。**这点值得在真机上验证是否有意为之。**
> 已记入 [../03-comparison/open-questions.md](../03-comparison/04-open-questions.md)。

最后一道保险（:1134）：

```c
if (best_energy_cpu < 0 || best_energy_cpu >= WALT_NR_CPUS)
        best_energy_cpu = prev_cpu;
```

---

## 8. 与原生 EAS 的对照

| 维度 | 原生 EAS | WALT |
|---|---|---|
| 入口 | `find_energy_efficient_cpu()` fair.c:7019 | `walt_find_energy_efficient_cpu()` walt_cfs.c:933 |
| hook | 无（内建）| `android_rvh_select_task_rq_fair` |
| `sd_flag` | 用于区分 wake/fork | **忽略** |
| 候选生成 | 遍历 perf domain，每个 domain 取 `best_cpu` | **两阶段**：`walt_find_best_target()` 粗筛 → 能量精算 |
| 每 domain 候选数 | 1 | 1（每簇 1）|
| 总是算能量 | **是** | **否**（`energy_eval_needed=false` 时跳过）|
| `prev_cpu` 为基准 | 是 | 是 |
| 迁移阈值 | **1/16 = 6.25%** | **`>>5` ≈ 3.1%**（注释错误地写 6%）|
| `overutilized` | 一票否决 `rd->overutilized` | 只对 `prev_cpu` 用 `__cpu_overutilized` |
| fastpath 数量 | 无（`select_idle_sibling` 另算）| **4 条** |
| 新任务 | `util_est` 加速 | `nt_*` 计数器 + `p->wake_q_count` |
| MVP/RTG/pipeline | 无 | **有**（否决 + 独占）|

---

## 9. 相关文档

- 窗口模型（`prev_runnable_sum` 的来源）→ [window-model.md](01-window-model.md)
- 预测需求（`walt_em_cpu_energy` 的输入）→ [demand-prediction.md](02-demand-prediction.md)
- RTG / 共置 / pipeline → [groups-and-clusters.md](06-groups-and-clusters.md)
- MVP / 高优先级 RTG → [rt-mvp.md](09-rt-mvp.md)
- boost（`per_task_boost` / `energy_eval_needed` 的来源）→ [boost.md](08-boost.md)
- `cpu_halted` / `is_reserved` → [power-side.md](07-power-side.md)
- 原生 EAS baseline → [../01-baseline/placement-eas.md](../01-baseline/04-placement-eas.md)
