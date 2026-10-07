# 原生 CFS 负载均衡

> **源码**：[fair.c](../../kernel/kernel/sched/fair.c)、[topology.c](../../kernel/kernel/sched/topology.c)、[sched.h](../../kernel/kernel/sched/sched.h)、[topology.h](../../kernel/include/linux/sched/topology.h)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17
> **范围**：CFS 负载均衡本体。PELT 内部见 [pelt.md](02-pelt.md)，EAS 选核见 [placement-eas.md](04-placement-eas.md)。

---

## 0. 先读：本树相对上游 5.15 的三处改名 / 缺失

**读这套代码前必须先知道这三点，否则会找不到函数。**

| 上游名字 | 本树情况 | 位置 |
|---|---|---|
| `newidle_balance()` | **改名为 `sched_balance_newidle()`** | fair.c:11154（前向声明 :3959；`!CONFIG_SMP` stub :4295）|
| `sched_balance_trigger()` | **不存在**（上游 ~5.4 已移除）| 职能拆分到 `trigger_load_balance()` (fair.c:11308) 和 `select_task_rq_fair()` / `wake_affine()` 里的 `SD_BALANCE_*` 判定 |
| `/proc/sys/kernel/sched_*` 注册表 | **本裁剪子树中不存在** | 变量都在（fair.c:42/64/87/90、core.c:92），但只有 debugfs 暴露（[debug.c:314-324](../../kernel/kernel/sched/debug.c#L314-L324)）+ `EXPORT_SYMBOL_GPL` |

字符串 `newidle_balance` 在本树**只出现在** `nohz_newidle_balance()`
（fair.c:11107）及其 `!CONFIG_NO_HZ_COMMON` stub（fair.c:11142）。
调用 `sched_balance_newidle()` 的地方是 `pick_next_task_fair()`
（fair.c:7711）。

---

## 1. 三个触发时机

负载均衡**不是**每次调度都跑。它由三个独立的入口触发：

| 时机 | 入口 | `enum cpu_idle_type` | 特点 |
|---|---|---|---|
| **周期性** | `trigger_load_balance()` → softirq → `run_rebalance_domains()` | `CPU_IDLE`/`CPU_NOT_IDLE` | 每个 rq 的 `next_balance` 到期时 |
| **即将 idle**（newidle）| `pick_next_task_fair()` → `sched_balance_newidle()` | `CPU_NEWLY_IDLE` | CPU 要闲下来了，**不必等 tick** |
| **NOHZ idle** | `nohz_balancer_kick()` → IPI → `nohz_idle_balance()` | `CPU_IDLE` | 已 idle 的 CPU 通过 IPI 被拉起来平衡 |

### 1.1 周期性触发

```c
void trigger_load_balance(struct rq *rq)          /* fair.c:11308 */
{
        if (time_after_eq(jiffies, rq->next_balance))
                raise_softirq(SCHED_SOFTIRQ);
        nohz_balancer_kick(rq);                    /* 总是调用 */
}
```

`raise_softirq(SCHED_SOFTIRQ)` → `run_rebalance_domains()`
（fair.c:11283）：

```c
static __latent_entropy void run_rebalance_domains(struct softirq_action *h)
{
        struct rq *this_rq = this_rq();
        enum cpu_idle_type idle = this_rq->idle_balance ?
                                  CPU_IDLE : CPU_NOT_IDLE;

        if (nohz_idle_balance(this_rq, idle))      /* :11297 先跑 NOHZ */
                return;
        update_blocked_averages(this_rq->cpu);     /* :11301 */
        rebalance_domains(this_rq, idle);          /* :11302 */
}
```

### 1.2 `rebalance_domains()` — 逐 domain 遍历

fair.c:10560。

```c
for_each_domain(cpu, sd) {
        /* max_newidle_lb_cost 按 ~1%/秒 衰减（因子 253/256） */
        if (time_after(jiffies, sd->next_decay_max_lb_cost)) {
                sd->max_newidle_lb_cost = (sd->max_newidle_lb_cost * 253) / 256;
                sd->next_decay_max_lb_cost = jiffies + HZ;
        }

        interval = get_sd_balance_interval(sd, busy);       /* :10602 */

        if (sd->flags & SD_SERIALIZE)                       /* :10604-10608 */
                spin_trylock(&balancing);                   /* 全局锁，:10543 */
        if (time_after_eq(jiffies, sd->last_balance + interval)) {
                if (load_balance(cpu, rq, sd, idle, &continue_balancing)) {  /* :10610 */
                        ...
                        if (!continue_balancing) { ... break; }
                }
                sd->last_balance = jiffies;
        }
        if (sd->flags & SD_SERIALIZE) spin_unlock(&balancing);
        ...
        rq->next_balance = next_balance;                    /* :10626-10629 */
        rq->max_idle_balance_cost = max(sysctl_sched_migration_cost, max_cost);
}
```

**逐层向上遍历 domain**（MC → DIE → …），每层有独立的
`balance_interval`。`SD_SERIALIZE` 只在 NUMA 层设置（见 §7g）。

`get_sd_balance_interval()`（fair.c:10422）：

```c
interval = sd->balance_interval;
if (idle != CPU_IDLE) interval *= sd->busy_factor;      /* busy_factor = 16 */
jiffies_to_msecs 换算；busy 时 -1 jiffy
clamp(interval, 1, max_load_balance_interval);
```

**`busy_factor` 的物理意义**：忙时要**降低**平衡频率（间隔 ×16），
因为迁移开销在忙时更贵。空闲时用 `min_interval` 快速响应。

### 1.3 newidle 触发

`pick_next_task_fair()`（fair.c:7584）在 **CFS 队列空**时：

```c
idle:                                                       /* :7709 */
        new_tasks = sched_balance_newidle(rq, rf);          /* :7711 */
        if (new_tasks < 0) return RETRY_TASK;               /* :7719-7720 */
        if (new_tasks > 0) goto again;                      /* :7722-7723 */
```

见 §6a。

---

## 2. `should_we_balance()` — 谁来平衡

fair.c:10117。**这是「一头热」原则的实现：避免多个 CPU 同时去拉同一个源。**

```c
static int should_we_balance(struct lb_env *env)
{
        struct sched_group *sg = env->sd->groups;

        if (!cpumask_test_cpu(env->dst_cpu, env->cpus))   /* :10126 hotplug 保护 */
                return 0;

        if (env->idle == CPU_NEWLY_IDLE)                  /* :10133-10134 */
                return 1;                                 /* newidle 时所有 CPU 都允许 */

        for_each_cpu_and(cpu, group_balance_mask(sg), env->cpus)
                if (idle_cpu(cpu))
                        return cpu == env->dst_cpu;       /* :10137-10143 第一个空闲 CPU 赢 */

        return group_balance_cpu(sg) == env->dst_cpu;     /* :10146 */
}
```

**两条规则**：

1. **`CPU_NEWLY_IDLE` 时全部允许**——因为「我要变成 idle」是新鲜信息，
   不该被旧的平衡权挡掉。
2. 其他情况：组内**第一个空闲 CPU** 负责；都不空闲则指定
   `group_balance_cpu()`（通常是组内第一个 CPU）。

> 这正是 WALT 用 `trace_android_rvh_sched_newidle_balance` 接管
> newidle 路径的原因：**newidle 是唯一不受「一头热」约束的入口**，
> 响应最快。

---

## 3. `load_balance()` — 主循环

fair.c:10153。

```c
static int load_balance(int this_cpu, struct rq *this_rq,
                        struct sched_domain *sd, enum cpu_idle_type idle,
                        int *continue_balancing)
```

### 3.1 骨架

```c
struct lb_env env = {                                  /* :10164-10174 */
        .sd = sd, .dst_cpu = this_cpu, .dst_rq = this_rq,
        .dst_grpmask = group_balance_mask(sd->groups),
        .idle = idle,
        .loop_break = sched_nr_migrate_break,          /* = 32 */
        .fbq_type = all,
        .tasks = LIST_HEAD_INIT(env.tasks),
};

cpumask_and(cpus, sched_domain_span(sd), cpu_active_mask);   /* :10176 */
env.cpus = cpus;

redo:                                                  /* :10180 */
        if (!should_we_balance(&env)) { *continue_balancing = 0; goto out_balanced; }
        group = find_busiest_group(&env);              /* :10186 */
        if (!group) { schedstat_inc(sd->lb_nobusyg[idle]); goto out_balanced; }

        busiest = find_busiest_queue(&env, group);     /* :10192 */
        if (!busiest) { schedstat_inc(sd->lb_nobusyq[idle]); goto out_balanced; }

        BUG_ON(busiest == env.dst_rq);                 /* :10198 */

        env.src_cpu = busiest->cpu;  env.src_rq = busiest;   /* :10202-10203 */
        env.flags |= LBF_ALL_PINNED;                   /* :10207 先假设全被 pin */

        if (busiest->nr_running > 1) {                 /* :10208-10215 */
                env.loop_max = min(sysctl_sched_nr_migrate, busiest->nr_running);
                ...
        }

more_balance:                                          /* :10217 */
        rq_lock_irqsave(busiest, &rf);  update_rq_clock(busiest);
        cur_ld_moved = detach_tasks(&env);             /* :10226 */
        rq_unlock(busiest, &rf);                       /* :10236 */

        if (cur_ld_moved) { attach_tasks(&env); ld_moved += cur_ld_moved; }  /* :10238-10241 */

        if (env.flags & LBF_NEED_BREAK) { env.flags &= ~LBF_NEED_BREAK; goto more_balance; }  /* :10245-10248 */
        ...
```

### 3.2 关键循环上限：`env.loop_max`

```c
env.loop_max = min(sysctl_sched_nr_migrate, busiest->nr_running);
```

**一次平衡最多迁移 `sysctl_sched_nr_migrate`（默认 32）个任务。**
这是个硬上限，防止一次平衡扫过整个运行队列导致中断延迟爆炸。

配合 `detach_tasks()` 里的 `loop_break`（§4a）：每搬 32 个就
设 `LBF_NEED_BREAK` 让出并重来。

### 3.3 重试路径

| 条件 | 动作 | 位置 |
|---|---|---|
| `LBF_NEED_BREAK` | `goto more_balance` | :10245-10248 |
| `LBF_DST_PINNED && env.imbalance > 0` | 换目标 CPU（从 `env.cpus` 清除 `dst_cpu`，重新选 `new_dst_cpu`），`goto more_balance` | :10269-10285 |
| 亲和性失败 | 父域 `sgc->imbalance = 1`（`LBF_SOME_PINNED`）| :10290-10295 |
| `LBF_ALL_PINNED` | 从 `cpus` 清除 busiest CPU，还有别的就 `goto redo`，否则 `goto out_all_pinned` | :10297-10314 |

### 3.4 失败计数与 active balance

```c
if (!ld_moved) {
        schedstat_inc(sd->lb_failed[idle]);
        if (idle != CPU_NEWLY_IDLE)
                sd->nr_balance_failed++;               /* :10317-10326 */
}

if (need_active_balance(&env)) {                       /* :10328 */
        if (cpumask_test_cpu(this_cpu, busiest->curr->cpus_ptr)) {
                busiest->active_balance = 1;           /* :10351-10354 */
                busiest->push_cpu = this_cpu;
                stop_one_cpu_nowait(cpu_of(busiest),
                        active_load_balance_cpu_stop, busiest,
                        &busiest->active_balance_work);
        }
} else
        sd->nr_balance_failed = 0;                     /* :10366-10368 */
```

> **【反直觉】`CPU_NEWLY_IDLE` 故意不增加 `nr_balance_failed`。**
> 源码注释说明：newidle 平衡频繁失败是正常的（刚刚还有活干），
> 不该污染失败计数去触发 active balance。

### 3.5 `balance_interval` 的复位与翻倍

```c
/* 复位 */
if (likely(!active_balance) || need_active_balance(&env))
        sd->balance_interval = sd->min_interval;       /* :10370-10373 */
...
/* 翻倍 */
if (idle != CPU_NEWLY_IDLE) {                          /* :10409-10410 跳过 */
        if (env.flags & LBF_ALL_PINNED && sd->balance_interval < MAX_PINNED_INTERVAL)
                sd->balance_interval *= 2;             /* :10412-10414 */
        else if (sd->balance_interval < sd->max_interval)
                sd->balance_interval *= 2;             /* :10415-10416 */
}
```

**平衡成功或触发 active balance → 间隔复位到 `min_interval`；
失败 → 间隔翻倍（退避）。** `MAX_PINNED_INTERVAL = 512`（:10055）。

---

## 4. 找最忙的组：`find_busiest_group()` + `calculate_imbalance()`

### 4.1 组分类（`enum group_type`）

fair.c:7937-7966。**顺序即优先级**（低→高，越靠后越该被拉）：

| 值 | 名称 | 含义 |
|---:|---|---|
| 0 | `group_has_spare` | 还有余量 |
| 1 | `group_fully_busy` | 刚好满，没余量但不过载 |
| 2 | `group_misfit_task` | 有任务放不下当前容量 |
| 3 | `group_asym_packing` | 非对称打包（如 big.LITTLE 偏好高核）|
| 4 | `group_imbalanced` | 父域标记的不平衡 |
| 5 | `group_overloaded` | 过载 |

`group_classify()`（fair.c:8932）的判定顺序：
overloaded → imbalanced → asym_packing → misfit → fully_busy → has_spare。

`group_has_capacity()`（fair.c:8890）/ `group_is_overloaded()`
（fair.c:8915）。注意 :8907 的注释明确说明
**`group_is_overloaded()` 不是简单的 `!group_has_capacity()`**——
两者的水位线不同，中间存在 `group_fully_busy` 这个「灰色地带」。

### 4.2 统计收集

`update_sg_lb_stats()`（fair.c:8961）逐组累计：

| 字段 | 来源 |
|---|---|
| `group_load` / `group_util` / `group_runnable` | 累加各 rq 的 PELT 值 |
| `sum_nr_running` / `sum_h_nr_running` | 运行队列长度 |
| `SG_OVERLOAD` (:8984) / `SG_OVERUTILIZED` (:8987) | 标志 |
| `idle_cpus` | :8997 |
| misfit | `rq->misfit_task_load`（:9006）|
| asym_packing | :9014 |
| `group_capacity` | `group->sgc->capacity`（:9021）|
| `group_type` | `group_classify()`（:9025）|
| `avg_load` | **仅当过载时计算**（:9028-9030）|

`update_sd_lb_stats()`（fair.c:9517）遍历 `sd->groups`，标记本地组，
累计 `total_load` / `total_capacity`（:9552-9553），判定
`prefer_sibling`（:9559），更新 `rd->overload` / overutilized（:9565-9574）。

`update_sd_pick_busiest()`（fair.c:9046）是组间比较的**决策矩阵**
（矩阵注释在 :9753）。

### 4.3 `find_busiest_group()` 的门槛链

fair.c:9784：

```c
init_sd_lb_stats(&sds);  update_sd_lb_stats(env, &sds);     /* :9789, :9795 */

if (sched_energy_enabled()) {                                /* :9797-9806 */
        trace_android_rvh_find_busiest_group(...);           /* :9801 */
        if (rd->pd && !overutilized && out_balance)
                goto out_balanced;                            /* EAS 接管 */
}

if (!sds.busiest) goto out_balanced;                         /* :9812-9813 */

/* 三种情况直接 force_balance，跳过 avg_load 检查 */
if (group_misfit_task | group_asym_packing | group_imbalanced)  /* :9816-9829 */
        goto force_balance;

if (local->group_type > busiest->group_type) goto out_balanced;  /* :9835-9836 */
```

之后是一长串「本地也挺忙，算了」的判定（:9847-9902），
包括 `imbalance_pct` 保守性检查：

```c
if (100 * busiest->avg_load <= sd->imbalance_pct * local->avg_load)
        goto out_balanced;                                   /* :9865-9867 */
```

**`imbalance_pct` 默认 117**（见 §7g）——意味着 busiest 必须比
local 忙 **17% 以上**才值得搬。

### 4.4 `calculate_imbalance()` — 决定搬多少 / 搬什么

fair.c:9601。**返回 `env->imbalance`，含义随 `migration_type` 变化。**

| `group_type` / 场景 | `migration_type` | `imbalance` 含义 |
|---|---|---|
| `group_misfit_task` | `migrate_misfit` | 1（一个任务）:9608-9612 |
| `group_asym_packing` | `migrate_task` | `busiest->sum_h_nr_running` :9615-9622 |
| `group_imbalanced` | `migrate_task` | 1 :9625-9634 |
| local 有余量 + busiest 过载 + `!SD_SHARE_PKG_RESOURCES` | `migrate_util` | `max(group_capacity, group_util) - group_util` :9641-9669 |
| local 有余量 + `group_weight == 1 \|\| prefer_sibling` | `migrate_task` | `(busiest - local sum_nr_running) >> 1` :9671-9679 |
| local 有余量 + 其他 | `migrate_task` | `max(0, (local_idle - busiest_idle) >> 1)` :9686-9688 |
| 两边都过载 | **`migrate_load`** | :9744-9748 |

两边都过载时（最复杂的一支）：

```c
env->imbalance = min(
        (busiest->avg_load - sds->avg_load) * busiest->group_capacity,
        (sds->avg_load - local->avg_load) * local->group_capacity
) / SCHED_CAPACITY_SCALE;
```

**取「把 busiest 拉到平均值」和「把 local 拉到平均值」两者中较小的**——
即「谁先到平均线谁说了算」，避免过度迁移。

> `migrate_load` 和 `migrate_util` 的区别是**度量单位**：
> `load` 是权重相关的（`cpu_load()`），`util` 是容量相关的
> （`cpu_util()`，频率归一化后）。在有 `SD_SHARE_PKG_RESOURCES`
> （同簇共享 L2）时用 `util`，跨簇时用 `load`。`[推测]`

---

## 5. 找最忙的 CPU：`find_busiest_queue()`

fair.c:9917。

```c
trace_android_rvh_find_busiest_queue(env->dst_cpu, group, env->cpus,
                                     &busiest, &done);      /* :9925-9928 */
if (done) return busiest;

for_each_cpu_and(i, sched_group_span(group), env->cpus) {
        ...
        nr_running = rq->cfs.h_nr_running;
        if (!nr_running) continue;                          /* :9960-9962 */

        capacity = capacity_of(i);                          /* :9964 */

        /* ASYM_CPUCAPACITY 保护：别选一个会迫使 高→低 active balance 的 CPU */
        if (!capacity_greater(capacity_of(env->dst_cpu), capacity) &&
            nr_running == 1) continue;                      /* :9972-9975 */

        switch (env->migration_type) {
        case migrate_load:  ... :9985-10006
        case migrate_util:  ... :10009-10023
        case migrate_task:  ... :10026-10030
        case migrate_misfit:... :10033-10043
        }
}
```

### 5.1 四种 `migration_type` 的选择标准

| 类型 | 度量 | 特殊规则 |
|---|---|---|
| `migrate_load` | `cpu_load(rq)` | `nr_running == 1 && load > imbalance && !check_cpu_capacity()` → **跳过**（:9985-9987）|
| `migrate_util` | `cpu_util(cpu)` | `nr_running <= 1` → 跳过（:10009-10023）|
| `migrate_task` | `nr_running` | 取最大 |
| `migrate_misfit` | `rq->misfit_task_load` | 取最大 |

`migrate_load` 的比较也用**交叉相乘**避免除法（:10002-10006）：

```c
if (load_i * capacity_j > load_j * capacity_i)   /* 等价于比较 scaled load */
```

> **【重要修正】本树 5.15 中没有字面的 `weight == 1` 判定。**
> misfit 的处理散在三处：(a) `migrate_misfit` 分支；
> (b) ASYM_CPUCAPACITY 保护里的 `nr_running == 1` 跳过（:9972-9975）；
> (c) `migrate_load` 里的单任务跳过（:9985-9987）。
>
> 相关 helper：`task_fits_cpu()` fair.c:4239、`update_misfit_status()` fair.c:4247、
> `check_cpu_capacity()` fair.c:8825、`check_misfit_status()` fair.c:8836。

---

## 6. 搬任务：`detach_tasks()` / `can_migrate_task()` / `attach_tasks()`

### 6.1 `detach_tasks()`

fair.c:8273。

```c
if (env->src_rq->nr_running <= 1) { env->flags &= ~LBF_ALL_PINNED; return 0; }  /* :8286-8289 */
if (env->imbalance <= 0) return 0;                                            /* :8291 */

/* 从队列尾部（vruntime 最大 = 最近跑过的）开始 */
list_for_each_entry_reverse(p, &env->src_rq->cfs_tasks, se.group_node) {      /* :8294 */
        env->loop++;
        if (env->loop > env->loop_max) { env->flags |= LBF_ALL_PINNED; break; }  /* :8304-8307 */
        if (env->loop > env->loop_break) {                                       /* :8310-8313 */
                env->loop_break += sched_nr_migrate_break;   /* = 32, fair.c:8265 */
                env->flags |= LBF_NEED_BREAK;
                break;
        }
        if (!can_migrate_task(p, env)) goto next;                                /* :8316-8317 */
        ...按 migration_type 记账...                                             /* :8319-8366 */
        detach_task(p, env);
        list_add(&p->se.group_node, &env->tasks);                                /* :8368-8369 */

#if defined(CONFIG_PREEMPTION) && !defined(CONFIG_PREEMPT_NONE)  /* :8373-8381 */
        if (env->idle == CPU_NEWLY_IDLE) break;   /* newidle 只搬 1 个，防止延迟 */
#endif
        if (env->imbalance <= 0) break;                                          /* :8387-8388 */
}
```

**三个关键上限**：

| 上限 | 值 | 位置 |
|---|---|---|
| `loop_max` | `min(sysctl_sched_nr_migrate=32, src->nr_running)` | :8304-8307 |
| `loop_break` | 每 32 个让出一次（`sched_nr_migrate_break`）| :8310-8313 |
| newidle 只搬 1 个 | `CONFIG_PREEMPTION` 下 | :8373-8381 |

> **【反直觉】从 `cfs_tasks` 的尾部开始扫**（`list_for_each_entry_reverse`）。
> `cfs_tasks` 按 vruntime 排序，**头部是 vruntime 最小的（最该跑的）**。
> 从尾部开始 = **优先搬「最不急着跑」的任务**，对源 CPU 伤害最小。

### 6.2 `can_migrate_task()`

fair.c:8118。

```c
trace_android_rvh_can_migrate_task(p, env->dst_cpu, &can_migrate);  /* :8125-8127 */
if (!can_migrate) return 0;                                          /* 厂商可直接否决 */

if (throttled_lb_pair(task_group(p), env->src_cpu, env->dst_cpu)) return 0;  /* :8136 */
if (!dl_task(p) && !rt_task(p) && kthread_is_per_cpu(p)) return 0;           /* :8140 */

if (!cpumask_test_cpu(env->dst_cpu, p->cpus_ptr)) {                          /* :8143-8174 */
        int new_dst_cpu;
        env->flags |= LBF_SOME_PINNED;
        new_dst_cpu = cpumask_any_and_distribute(env->dst_grpmask, p->cpus_ptr);
        if (new_dst_cpu < nr_cpu_ids) { env->flags |= LBF_DST_PINNED; ... }
        return 0;
}

if (task_running(env->src_rq, p)) return 0;                                  /* :8179 */

if (env->flags & LBF_ACTIVE_LB) return 1;                                    /* :8191-8192 无条件放行 */

/* cache-hot 逻辑 */
if (migrate_degrades_locality(p, env)) { ... }                               /* fair.c:8060 */
if (!task_hot(p, env->src_rq->clock_task, env->sd)) return 1;                /* fair.c:8012 */
if (env->sd->nr_balance_failed > env->sd->cache_nice_tries) return 1;        /* :8194-8205 */
```

**否决顺序**（从早到晚）：厂商 hook → cgroup 限流 → per-cpu kthread →
亲和性 → 正在运行 → **cache 热度**。

`LBF_ACTIVE_LB`（active balance）**无条件放行**——因为 active balance
是被动的、代价高的，一旦触发就应该成功。

### 6.3 `attach_tasks()`

fair.c:8435。锁 `dst_rq`，排空 `env->tasks`，每个任务调
`attach_task()`（fair.c:8408）：

```c
activate_task(rq, p, ENQUEUE_NOCLOCK);
check_preempt_curr(rq, p, 0);
```

### 6.4 `LBF_*` 标志

fair.c:7975-7979：

| 值 | 名称 | 含义 |
|---:|---|---|
| 0x01 | `LBF_ALL_PINNED` | 所有任务都被亲和性挡住 |
| 0x02 | `LBF_NEED_BREAK` | 需要让出并重来 |
| 0x04 | `LBF_DST_PINNED` | 目标 CPU 被 pin，但可换 |
| 0x08 | `LBF_SOME_PINNED` | 部分被 pin |
| 0x10 | `LBF_ACTIVE_LB` | 这是 active balance |

---

## 7. Active Balance（主动拉取）

**普通负载均衡是「拉」（pull）——目标 CPU 主动去搬。但如果源 CPU
一直不放弃锁（或任务一直 cache-hot），拉也拉不动。**

Active balance 反过来：**让源 CPU 自己停下来，把任务「推」（push）出去。**

### 7.1 触发条件：`need_active_balance()`

fair.c:10086：

```c
static int need_active_balance(struct lb_env *env)
{
        struct sched_domain *sd = env->sd;

        if (env->idle == CPU_NEWLY_IDLE) {          /* newidle 不做 active balance */
                ...
                return 0;
        }

        if (asym_active_balance(env))  return 1;    /* :10090 */
        if (imbalanced_active_balance(env)) return 1;/* :10093 */

        /* 单 CFS 任务 + 源容量降级 + 目标容量更好且空闲 */
        if (env->sd->flags & SD_ASYM_CPUCAPACITY &&
            env->migration_type == migrate_task &&
            ... ) return 1;                          /* :10102-10107 */

        if (env->migration_type == migrate_misfit) return 1;   /* :10109 */

        return 0;
}
```

| 依据 | 函数 | 条件 |
|---|---|---|
| 非对称打包 | `asym_active_balance()` fair.c:10058 | `SD_ASYM_PACKING` && `idle != CPU_NOT_IDLE` && `sched_asym_prefer(dst, src)` |
| 反复失败 | `imbalanced_active_balance()` fair.c:10070 | `migration_type == migrate_task && nr_balance_failed > cache_nice_tries + 2` |
| misfit | 直接判定 | `migration_type == migrate_misfit` |

> **`nr_balance_failed > cache_nice_tries + 2` 是「拉不动就推」的量化。**
> 联系 §3.4：newidle 不增计数，所以这个计数只在周期性/newidle 之外的
> 反复失败中累积。

### 7.2 执行：`active_load_balance_cpu_stop()`

fair.c:10464。**这是一个 `cpu_stop` 回调**——在源 CPU 上以最高优先级
停下当前任务执行。

```c
struct rq *busiest_rq = data;
int busiest_cpu = cpu_of(busiest_rq);
int target_cpu = busiest_rq->push_cpu;                      /* :10466-10469 */
struct rq *target_rq = cpu_rq(target_cpu);

if (unlikely(!cpu_active(busiest_cpu) ||
             !busiest_rq->active_balance ||
             smp_processor_id() != busiest_cpu))
        return 0;                                            /* :10480-10486 复查 */

if (busiest_rq->nr_running <= 1) goto out;                   /* :10489 */
BUG_ON(busiest_rq == target_rq);                             /* :10497 */

/* 找一条同时覆盖 busiest 和 target 的 domain */
for_each_domain(target_cpu, sd)
        if (cpumask_test_cpu(busiest_cpu, sched_domain_span(sd))) break;  /* :10500-10504 */

env = (struct lb_env) { .sd = sd, .dst_cpu = target_cpu, .dst_rq = target_rq,
                        .src_cpu = busiest_cpu, .src_rq = busiest_rq,
                        .idle = CPU_IDLE, .flags = LBF_ACTIVE_LB, ... };  /* :10507-10516 */

p = detach_one_task(&env);                                   /* :10521 */
if (p) {
        sd->nr_balance_failed = 0;                           /* :10525 成功后清零 */
        ...
        attach_one_task(target_rq, p);                       /* :10536 */
}
out:
        busiest_rq->active_balance = 0;                      /* :10532 */
```

**`active_balance` 标志的生命周期**（`rq->active_balance`，
[sched.h:1034](../../kernel/kernel/sched/sched.h#L1034)）：

```
load_balance() :10351 置 1  →  stop_one_cpu_nowait() 排队
   → 源 CPU 停下，active_load_balance_cpu_stop() 执行
   → :10532 清零（唯一清零点）
```

`rq->active_balance` 实际上是在**串行化 `active_balance_work`**：
:10351 处和 :10485 处都会检查它，防止同一个 rq 上排队多个 stop work。

相关定义：`rq->push_cpu` sched.h:1035；`rq->active_balance_work` sched.h:1036；
`struct cpu_stop_work` [stop_machine.h:24-30](../../kernel/include/linux/stop_machine.h#L24-L30)；
`stop_one_cpu_nowait()` 声明于 stop_machine.h:34。

---

## 8. newidle 路径

### 8.1 `sched_balance_newidle()`

fair.c:11154（== 上游 `newidle_balance()`）。

```c
static int sched_balance_newidle(struct rq *this_rq, struct rq_flags *rf)
```

**返回值语义**：`<0` = 锁已释放且出现更高优先级任务（返回 `RETRY_TASK`）；
`0` = 没拉到；`>0` = 拉到了 fair 任务。

```c
trace_android_rvh_sched_newidle_balance(this_rq, rf, &pulled_task, &done);  /* :11163-11165 */
if (done) return pulled_task;                    /* ★ WALT 在这里完全接管 */

update_misfit_status(NULL, this_rq);                          /* :11167 */
if (this_rq->ttwu_pending) return 0;                          /* :11173 */

this_rq->idle_stamp = rq_clock(this_rq);                      /* :11180 */
if (!cpu_active(this_cpu)) return 0;                          /* :11185 */

/* 成本门槛 */
if (this_rq->avg_idle < sysctl_sched_migration_cost ||
    !this_rq->rd->overload) {                                 /* :11196-11206 */
        rcu_read_lock(); sd = rcu_dereference_check_sched_domain(this_rq->sd);
        if (sd) update_next_balance(sd, &next_balance);
        /* 直接跳过，不做平衡 */
}
rq_unpin_lock(this_rq, rf);  raw_spin_rq_unlock(this_rq);
update_blocked_averages(this_cpu);                            /* :11208-11210 */

for_each_domain(this_cpu, sd) {
        if (this_rq->avg_idle < curr_cost + sd->max_newidle_lb_cost) break;  /* :11216-11219 */
        if (sd->flags & SD_BALANCE_NEWIDLE) {
                t0 = sched_clock();
                pulled_task = load_balance(this_cpu, this_rq, sd,
                                           CPU_NEWLY_IDLE, &continue_balancing);  /* :11221-11226 */
                t1 = sched_clock();
                curr_cost += t1 - t0;
                this_rq->max_idle_balance_cost = max(curr_cost, ...);
        }
        ...
        if (pulled_task || this_rq->nr_running > 0 || this_rq->ttwu_pending) break;  /* :11241-11243 */
}
```

**三道门槛决定要不要做**：

| 门槛 | 条件 | 位置 |
|---|---|---|
| 厂商 hook | `done` 被置位 | :11163 |
| `ttwu_pending` | 已有任务在来的路上 | :11173 |
| 平均空闲时间 | `avg_idle < sysctl_sched_migration_cost`（默认 500us） | :11196 |
| 全局过载标志 | `!rd->overload` | :11196 |
| 逐 domain 成本 | `avg_idle < curr_cost + sd->max_newidle_lb_cost` | :11216 |

> **`avg_idle` 门槛的物理意义**：如果 CPU 平均空闲时间很短（< 500us），
> 说明它马上就要被唤醒，花时间做平衡得不偿失。
> `max_newidle_lb_cost` 是**上次这层 domain 平衡的实际耗时**，
> 用来预测这次要花多久——**自适应的成本模型**。

### 8.2 尾部处理

```c
rq_lock(this_rq, rf);
this_rq->max_idle_balance_cost = max(sysctl_sched_migration_cost, max_cost);  /* :11247-11250 */

if (!pulled_task) {
        /* 期间有任务被唤醒进来了？当作拉到了 */
        if (this_rq->nr_running > 1) { pulled_task = 1; ... }   /* :11257-11258 */
        /* 出现更高优先级 class → 交回去重新选 */
        if (this_rq->nr_running && !check_preempt_curr(...)) ...
        else if (this_rq->curr->sched_class != &fair_sched_class) return -1;  /* :11261-11262 */
}
out:
if (pulled_task || time_after(jiffies, this_rq->next_balance))
        this_rq->next_balance = next_balance;                /* :11265-11267 */
if (!pulled_task)
        nohz_newidle_balance(this_rq);                       /* :11272 想更新 blocked load */
```

---

## 9. NOHZ idle balance

**问题**：一个 CPU 已经 idle（NOHZ 关闭了 tick），它的运行队列上
可能还挂着 blocked load（睡眠任务的 PELT 衰减）。没人去更新它。

**方案**：由别的 CPU 通过 IPI 把它「踢」起来。

### 9.1 触发端

`nohz_balancer_kick()`（fair.c:10729）——在 `trigger_load_balance()`
里**每次 tick 都调用**（不管 `next_balance` 是否到期）：

```c
if (rq->idle_balance) goto out;                  /* :10738 正在做，别重复 */
if (rq->nr_running == 1 && ...) ...
nohz_balance_exit_idle(rq);                      /* :10745 */
if (!atomic_read(&nohz.nr_cpus)) return;         /* :10751 没有 idle CPU，没什么可踢 */

/* 依次判定要踢什么 */
if (nohz.has_blocked && time_after(...)) flags |= NOHZ_STATS_KICK;   /* :10754-10756 */

if (rq->nr_running >= 2) {                                            /* :10765-10768 */
        flags |= NOHZ_STATS_KICK | NOHZ_BALANCE_KICK;
        goto out;
}

/* 有降容量任务 */
if (sd && sd->flags & SD_ASYM_CPUCAPACITY && ... ) { ... }            /* :10779-10782 */

/* ASYM_PACKING：有更好的空闲 CPU */
if (sd && sd->flags & SD_ASYM_PACKING && ... ) { ... }                /* :10785-10798 */

/* misfit 任务 */
if (check_misfit_status(rq, sd) && ...) { ... }                       /* :10800-10809 */

/* LLC 共享且忙 CPU > 1 */
if (sd && sd->flags & SD_SHARE_PKG_RESOURCES && ... nr_busy_cpus > 1) {  /* :10821-10837 */
        ...
}

kick_ilb(flags);                                                      /* :10842 */
out:
        ...
```

`kick_ilb()`（fair.c:10693）：

```c
if (flags & NOHZ_BALANCE_KICK)
        WRITE_ONCE(nohz.next_balance, jiffies + 1);
smp_call_function_single_async(ilb_cpu, &cpu_rq(ilb_cpu)->nohz_csd);  /* :10722 */
```

`find_new_ilb()`（fair.c:10666）挑一个 idle 且在
`housekeeping_cpumask(HK_FLAG_MISC)` 里的 CPU；有厂商 hook
（:10671）。

### 9.2 被踢端

```
IPI (nohz_csd) → nohz_csd_func() → nohz_run_idle_balance(cpu)   /* fair.c:11093 */
    → _nohz_idle_balance(..., NOHZ_STATS_KICK, CPU_IDLE)         /* fair.c:10974 */
```

`_nohz_idle_balance()`：

```c
if (flags & NOHZ_STATS_KICK) {
        if (atomic_dec_and_test(&nohz.nr_cpus)) ... 
        nohz.has_blocked = false;                                 /* :10996-10997 */
}
smp_mb();                                                         /* :11003 */

for_each_cpu_wrap(balance_cpu, nohz.idle_cpus_mask, this_cpu + 1) {   /* :11009 */
        rq = cpu_rq(balance_cpu);
        update_nohz_stats(rq);                                    /* 更新 blocked load */
        if (time_after_eq(jiffies, rq->next_balance))
                rebalance_domains(rq, CPU_IDLE);                   /* :11026-11042 */
}
...
nohz.next_balance = next_balance;                                 /* :11055-11056 */
nohz.next_blocked = now + LOAD_AVG_PERIOD;                        /* :11058-11060 */
```

### 9.3 NOHZ 标志

[sched.h:2786-2800](../../kernel/kernel/sched/sched.h#L2786-L2800)：

| 位 | 名称 | 含义 |
|---:|---|---|
| 0 | `NOHZ_BALANCE_KICK` | 要跑完整平衡 |
| 1 | `NOHZ_STATS_KICK` | 只要更新统计数据 |
| 2 | `NOHZ_NEWILB_KICK` | 进入 idle 前先更新 blocked load |
| 3 | `NOHZ_NEXT_KICK` | |

`nohz_flags(cpu)` 宏在 sched.h:2802（→ `cpu_rq(cpu)->nohz_flags`）。
`rq` 侧字段：`nohz_idle_balance` sched.h:1028、`nohz_flags` sched.h:961、
`nohz_csd` sched.h:958。

`struct nohz`（file-static）fair.c:6010-6016：
`idle_cpus_mask`、`atomic_t nr_cpus`、`has_blocked`、`next_balance`、`next_blocked`。

> **`sd->nohz_idle` 在本树是残留字段。** 声明在
> [topology.h:94](../../kernel/include/linux/sched/topology.h#L94)，
> 但 5.15 的 NOHZ 路径全部由 `rq->nohz_idle_balance` / `rq->nohz_flags` /
> `rq->nohz_csd` 驱动。**在 fair.c 中找不到任何对 `->nohz_idle` 的引用。**

其他入口：`nohz_idle_balance()`（fair.c:11072，在
`run_rebalance_domains()` 里先跑）、`nohz_newidle_balance()`
（fair.c:11107，设 `NOHZ_NEWILB_KICK`）。

---

## 10. sched_domain / sched_group 拓扑

### 10.1 结构

| 结构 | 位置 | 关键字段 |
|---|---|---|
| `struct sched_domain` | [topology.h:83-162](../../kernel/include/linux/sched/topology.h#L83-L162) | `parent`(:85) `child`(:86) `groups`(:87) `min_interval`(:88) `max_interval`(:89) `busy_factor`(:90) `imbalance_pct`(:91) `cache_nice_tries`(:92) `nohz_idle`(:94) `flags`(:95) `level`(:96) `last_balance`(:99) `balance_interval`(:100) `nr_balance_failed`(:101) `max_newidle_lb_cost`(:104) `next_decay_max_lb_cost`(:105) `avg_scan_cost`(:107) `shared`(:147) `span_weight`(:149) `span[]`(:161) |
| `struct sched_domain_shared` | topology.h:75-81 | `ref` `nr_busy_cpus` `has_idle_cores` + 厂商数据 |
| `struct sched_group` | [sched.h:1856-1872](../../kernel/kernel/sched/sched.h#L1856-L1872) | `next`（环形）`ref` `group_weight` `sgc` `asym_prefer_cpu` `cpumask[]` |
| `struct sched_group_capacity` | sched.h:1837-1854 | `ref` `capacity` `min_capacity` `max_capacity` `next_update` **`imbalance`** `cpumask[]` |
| `struct sched_domain_topology_level` | topology.h:194-203 | `mask` `sd_flags` `flags` `numa_level` `data` `name` |

`group_weight == 1` 是 §4.4 里「组内只有一个 CPU」的判定依据。
`sgc->imbalance` 是父域向子域传递不平衡信令的字段
（§3.3 亲和性失败时置 1，`find_busiest_group()` :9816-9829 读到后
`force_balance`）。

### 10.2 domain 层级

`default_topology[]` [topology.c:1619-1628](../../kernel/kernel/sched/topology.c#L1619-L1628)：

```
SMT   (若 CONFIG_SCHED_SMT)      flags: SD_SHARE_CPUCAPACITY | SD_SHARE_PKG_RESOURCES
MC    (若 CONFIG_SCHED_MC)       flags: SD_SHARE_PKG_RESOURCES
DIE   (cpu_cpu_mask)
NULL  ← 终止符
```

**自底向上排列。** NUMA 层**不在这里**——由 NUMA 代码动态添加
（`sd_numa_mask` topology.c:1646、`sched_domains_numa_masks`）。

`for_each_domain()` 从 `rq->sd` 开始沿 `->parent` 向上，
所以遍历顺序是 **内核内 → 簇内 → 芯片内**，由近及远。✓

`set_sched_topology()` topology.c:1636；`sched_domain_topology` topology.c:1630。

Mask/flags 回调：`cpu_smt_flags()` topology.h:41、
`cpu_core_flags()` topology.h:48、`cpu_numa_flags()` topology.h:55。

### 10.3 `SD_*` 标志

由 [sd_flags.h](../../kernel/include/linux/sched/sd_flags.h) 的
`SD_FLAG(name, metaflags)` X-macro 定义，在 topology.h:16-28 展开成
「位号 enum + 位值」。

| 标志 | 位号 | metaflags |
|---|---|---|
| `SD_BALANCE_NEWIDLE` | :51 | `SDF_SHARED_CHILD \| SDF_NEEDS_GROUPS` |
| `SD_BALANCE_EXEC` | :59 | |
| `SD_BALANCE_FORK` | :67 | |
| `SD_BALANCE_WAKE` | :75 | |
| `SD_WAKE_AFFINE` | :82 | |
| `SD_ASYM_CPUCAPACITY` | :91 | |
| `SD_ASYM_CPUCAPACITY_FULL` | :101 | |
| `SD_SHARE_CPUCAPACITY` | :110 | |
| `SD_SHARE_PKG_RESOURCES` | :119 | |
| `SD_SERIALIZE` | :130 | |
| `SD_ASYM_PACKING` | :140 | |
| `SD_PREFER_SIBLING` | :150 | |
| `SD_OVERLAP` | :158 | |
| `SD_NUMA` | :166 | |

Metaflags：`SDF_SHARED_CHILD`(0x1) / `SDF_SHARED_PARENT`(0x2) /
`SDF_NEEDS_GROUPS`(0x4)，sd_flags.h:32-43。

### 10.4 `sd_init()` 的默认值

[topology.c:1505](../../kernel/kernel/sched/topology.c#L1505)，
默认值在 :1529-1558：

| 字段 | 默认 |
|---|---|
| `min_interval` | `sd_weight` |
| `max_interval` | `2 * sd_weight` |
| `busy_factor` | **16** |
| `imbalance_pct` | **117** |
| `cache_nice_tries` | 0 |
| `last_balance` | `jiffies` |
| `balance_interval` | `sd_weight` |
| 默认 flags | `SD_BALANCE_NEWIDLE \| SD_BALANCE_EXEC \| SD_BALANCE_FORK \| SD_WAKE_AFFINE \| SD_PREFER_SIBLING`（:1537-1548）|

按拓扑标志覆盖（:1577-1599）：

| 条件 | 覆盖 |
|---|---|
| `SD_SHARE_CPUCAPACITY`（SMT）| `imbalance_pct = 110` |
| `SD_SHARE_PKG_RESOURCES`（MC）| `imbalance_pct = 117`，`cache_nice_tries = 1` |
| `SD_NUMA` | `cache_nice_tries = 2`；清 `SD_PREFER_SIBLING`；置 `SD_SERIALIZE`；超过 reclaim 距离再清 `SD_BALANCE_EXEC\|SD_BALANCE_FORK\|SD_WAKE_AFFINE`（:1590-1594）|
| 其他 | `cache_nice_tries = 1`（:1598）|

`TOPOLOGY_SD_FLAGS` 掩码 topology.c:1498-1502。
`build_sched_domains()` topology.c:2170；`build_sched_domain()` :2099；
`cpu_attach_domain()` :680。

> **`cache_nice_tries` 的含义**：超过这个次数后，cache-hot 的任务
> 也允许被迁移（§6.2 :8194-8205）。同簇内（MC）为 1，
> **跨 NUMA 为 2——因为跨节点的迁移代价更高，要更谨慎。**

---

## 11. 可调参数

| 参数 | 定义 | 默认 |
|---|---|---|
| `sysctl_sched_nr_migrate` | [core.c:92](../../kernel/kernel/sched/core.c#L92) | 32 |
| `sysctl_sched_migration_cost` | [fair.c:90](../../kernel/kernel/sched/fair.c#L90) | 500000 (ns) |
| `sysctl_sched_latency` | fair.c:42 | 6000000 (ns) |
| `sysctl_sched_min_granularity` | fair.c:64 | 750000 (ns) |
| `sysctl_sched_wakeup_granularity` | fair.c:87 | 1000000 (ns) |
| `sysctl_sched_tunable_scaling` | fair.c:57 | `SCHED_TUNABLESCALING_LOG` |
| `sched_nr_latency` | fair.c:70（static）| 8（:634 处按 latency/min_granularity 重算）|
| `sched_nr_migrate_break` | fair.c:8265（static const）| 32 |
| `max_load_balance_interval` | `update_max_interval()` fair.c:10549-10552 | `HZ * num_online_cpus() / 10` |

`max_load_balance_interval` 的 extern 声明在 sched.h:2334。

> **同 §0 第 3 条**：`sysctl_sched_min_granularity_ns` 这类**带 `_ns`
> 后缀的 procfs 名字在本子树中不存在**。C 变量名是
> `sysctl_sched_min_granularity`（fair.c:64），
> `_ns` 后缀名只出现在 [Documentation/scheduler/sched-design-CFS.rst:97](../../kernel/Documentation/scheduler/sched-design-CFS.rst#L97)。
> `sched_balance_scan_size` 等 `sched_balance_*` 参数**不存在**
> （上游 5.15 前已移除）。

---

## 12. WALT 在 LB 路径上的注入点 [对后续文档最重要]

这些是原生 LB 路径中供厂商代码（WALT）插手的挂载点：

| 钩子 | 位置 | 作用 |
|---|---|---|
| `trace_android_rvh_find_busiest_group` | fair.c:9801 | **EAS-gated**，可跳过深度统计 |
| `trace_android_rvh_find_busiest_queue` | fair.c:9925 | 可直接指定 busiest，置 `done` |
| `trace_android_rvh_can_migrate_task` | fair.c:8125 | **可否决任何迁移** |
| `trace_android_rvh_sched_newidle_balance` | fair.c:11163 | **WALT 接管 newidle 的主入口** |
| `trace_android_rvh_sched_rebalance_domains` | fair.c:10573 | 可跳过整层 domain 平衡 |
| `trace_android_rvh_sched_nohz_balancer_kick` | fair.c:10761 | 可改 NOHZ kick 的 flags |
| `trace_android_rvh_find_new_ilb` | fair.c:10671 | 可指定 ILB CPU |
| `trace_android_rvh_update_cpu_capacity` | fair.c:8755 | 可改容量 |
| `trace_android_rvh_update_misfit_status` | fair.c:4251 | 可改 misfit 判定 |
| `trace_android_vh_build_sched_domains` | topology.c:2255 | domain 构建后回调 |

WALT 自己的 LB 实现在 [../../kernel/kernel/sched/walt/walt_lb.c](../../kernel/kernel/sched/walt/walt_lb.c)。
注意 walt_lb.c:803 的注释：「`/* similar to sysctl_sched_migration_cost */`」——
**WALT 在 LB 里复制了原生 `sysctl_sched_migration_cost` 的语义，而不是复用这个变量。**

> **一个直接可验证的结论**：`sched_balance_newidle()` 的第一个动作就是
> 调 `trace_android_rvh_sched_newidle_balance` 并在 `done` 时直接返回
> （fair.c:11163-11165）。也就是说**如果 WALT 注册了这个钩子并置 `done`，
> 原生 newidle 平衡的全部逻辑（:11167 之后）都不会执行**。
> 这是「`done` 标志」式接管手法；与之并列的还有「短路 return」式和
> 「副作用旁路」式，三者对照见
> [base-vs-walt.md §2](../03-comparison/01-base-vs-walt.md)。
> 具体 WALT 是否注册见 [../02-walt/05-load-balance.md](../02-walt/05-load-balance.md)。

> **【反例警示】** 不要假设「有 hook 调用点」就等于「hook 被注册」。
> `effective_cpu_util()` 里的 `android_rvh_effective_cpu_util`
> 就是**已声明、已导出、已调用、但全树无注册者**的例子
> （见 [schedutil.md §3.2](03-schedutil.md#32-第-1-步一个未生效的-vendor-hook-短路点-关键)）。
> 判断一个 hook 是否真的生效，必须去搜 `register_trace_android_rvh_<名字>`。

---

## 13. 相关文档

- PELT（`load` / `util` 的来源）→ [pelt.md](02-pelt.md)
- EAS 选核（`select_task_rq_fair` / `find_energy_efficient_cpu`）→ [placement-eas.md](04-placement-eas.md)
- 调度框架（`pick_next_task_fair` 调用点）→ [sched-framework.md](01-sched-framework.md)
- WALT 的 LB 改造 → [../02-walt/load-balance.md](../02-walt/05-load-balance.md)
- 逐模块差异 → [../03-comparison/base-vs-walt.md](../03-comparison/01-base-vs-walt.md)
