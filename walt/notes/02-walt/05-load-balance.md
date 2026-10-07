# WALT 的负载均衡（WALT 侧）

> **源码**：[walt_lb.c](../../kernel/kernel/sched/walt/walt_lb.c)、[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

原生 `load_balance()` / sched domain / active balance 的机制见
[01-baseline/load-balance.md](../01-baseline/05-load-balance.md)，**不在本文**。
本文只回答：在原生 LB 之上，WALT 加了什么、改了什么、整个换掉了什么。

| 原生的哪一块 | WALT 的做法 | 章节 |
|---|---|---|
| `newidle_balance()` | **整体接管**（`done=1` 直接返回） | §2 |
| `find_busiest_queue()` | **整体接管**（选 rq 的判据换掉） | §3 |
| `can_migrate_task()` | 叠加否决（不接管，只额外说"不许"） | §3.4 |
| `nohz_balancer_kick()` | **整体接管** | §8.2 |
| `load_balance()` 主体（detach/attach、group 遍历） | **不接管**，仍走原生 | §1.2 |
| sched domain 层级 | WALT 完全不看 `sd` | §2.5 |
| misfit 主动上迁 | WALT 自己一条 tick 路径 | §6.2 |
| 大任务轮转 | WALT 独有，原生没有 | §5 |

一句话：**WALT 把"选谁"和"什么时候选"换掉，把"怎么搬"留给原生**；唯一例外是 newidle，那条路 WALT 从头写到尾。

---

## 1. 接入方式：4 个 hook，不是替换

### 1.1 `walt_lb_init()`

`walt_lb_init()` [walt_lb.c:1117](../../kernel/kernel/sched/walt/walt_lb.c#L1117) 由
`walt_init()` 在 [walt.c:5096](../../kernel/kernel/sched/walt/walt.c#L5096) 调用。
walt_lb.c 对外只暴露 3 个符号（`walt_lb_tick` / `walt_smp_call_newidle_balance` / `walt_lb_init`），
busiest 选择与 pull 逻辑全是 `static`。注册的 4 个 hook：

| hook | 注册点 | 原生调用点 | `done=1` | 作用 |
|---|---|---|---|---|
| `android_rvh_sched_newidle_balance` | [walt_lb.c:1126](../../kernel/kernel/sched/walt/walt_lb.c#L1126) | [fair.c:11163](../../kernel/kernel/sched/fair.c#L11163) | 是 | 完全接管 newidle |
| `android_rvh_find_busiest_queue` | [walt_lb.c:1125](../../kernel/kernel/sched/walt/walt_lb.c#L1125) | [fair.c:9925](../../kernel/kernel/sched/fair.c#L9925) | 是 | 周期 LB 的 busiest rq 由 WALT 选 |
| `android_rvh_can_migrate_task` | [walt_lb.c:1124](../../kernel/kernel/sched/walt/walt_lb.c#L1124) | [fair.c:8125](../../kernel/kernel/sched/fair.c#L8125) | 否 | 唯一的"只否决"型 hook |
| `android_rvh_sched_nohz_balancer_kick` | [walt_lb.c:1123](../../kernel/kernel/sched/walt/walt_lb.c#L1123) | [fair.c:10761](../../kernel/kernel/sched/fair.c#L10761) | 是 | 接管 nohz kick 判定 |

hook 的注册/调用机制见 [02-integration-model.md](../00-overview/02-integration-model.md) §5.4。

### 1.2 为什么 `load_balance()` 主体不用管

```c
/* find_busiest_queue()，fair.c:9925 */
trace_android_rvh_find_busiest_queue(env->dst_cpu, group, env->cpus,
                                     &busiest, &done);
if (done)
        return busiest;
```

返回值被 `load_balance()` 当作 `env.src_rq`，后面 detach / attach / active balance /
`stop_one_cpu_nowait` 全是原生代码。**只要换掉"哪个 rq 最忙"这个判据，整条搬运流水线自动跟着变**——
这就是 `android_rvh_find_busiest_queue` 给 `busiest` 出参的意义。

[反直觉] `done=1` 在这里的语义不是"跳过 LB"，而是"busiest 已给你，别再遍历 group"，
`load_balance()` 照常继续跑；只有在 `sched_newidle_balance` / `nohz_balancer_kick` 里
`done=1` 才是"整段跳过"。

### 1.3 `_walt_can_migrate_task()` 是唯一的门控实现

`walt_can_migrate_task()` [walt_lb.c:1091](../../kernel/kernel/sched/walt/walt_lb.c#L1091)
只算 `to_lower` / `to_higher`，再调
`_walt_can_migrate_task()` [walt_lb.c:235](../../kernel/kernel/sched/walt/walt_lb.c#L235)，
且传 `force=true`：

```c
/* walt_can_migrate_task() */
if (_walt_can_migrate_task(p, dst_cpu, to_lower, to_higher, true))
        return;
*can_migrate = 0;
```

同一函数在 WALT 自己的 pull 路径里被调两次（`force=false`、`force=true`）。
`force` = "忽略 `walt_get_rtg_status()` 与 `task_fits_max()` 这两条软门控"，只保留硬约束
（iowait、pipeline 低延迟、boost、halt）。`[推测]` 给原生 LB 用 `true`，是因为原生一轮只搬一个任务，
被软门控挡住就等于整轮白跑；WALT 自己的 pull 有第二轮兜底。

---

## 2. `walt_newidle_balance()`：完全接管

### 2.1 它在哪、谁调它

**注意路径**：函数在 [walt_lb.c:805](../../kernel/kernel/sched/walt/walt_lb.c#L805)，
**不在 walt_cfs.c**。`walt_cfs.c` 只有 `walt_cfs_tick()`
[walt_cfs.c:1399](../../kernel/kernel/sched/walt/walt_cfs.c#L1399)（MVP 运行时记账，见 [rt-mvp.md](09-rt-mvp.md)）。

| 入口 | 位置 | `force_overload` | 场景 |
|---|---|---|---|
| `walt_sched_newidle_balance()` | [walt_lb.c:1110](../../kernel/kernel/sched/walt/walt_lb.c#L1110) | `false` | 原生 `sched_balance_newidle()` [fair.c:11154](../../kernel/kernel/sched/fair.c#L11154) 经 hook 转进来 |
| `walt_smp_newidle_balance()` | [walt_lb.c:996](../../kernel/kernel/sched/walt/walt_lb.c#L996) | `true` | `walt_halt.c` 在 **unhalt** 后主动 kick（§7.2） |

第二个入口通过 `call_single_data_t` 异步投递，投递函数
`walt_smp_call_newidle_balance()` [walt_lb.c:1013](../../kernel/kernel/sched/walt/walt_lb.c#L1013)，
每 CPU 一个 `nib_csd`（[walt_lb.c:1011](../../kernel/kernel/sched/walt/walt_lb.c#L1011)），
在 `walt_lb_init()` 里 `INIT_CSD`。

### 2.2 与原生 `newidle_balance()` 的差别

原生走 sched domain 层级自下而上找空闲 CPU，带 `sched_group` 负载比较、
`avg_idle` / `sysctl_sched_migration_cost` 代价模型、一堆 `sd->flags` 判断。WALT 把这一整套**扔掉**：

- 不做 sched domain 遍历，只按 `cpu_array` 的**固定顺序**扫（§2.5）
- 不做组间负载比较，直接在掩码里挑 util 最大的 rq
- 没有 migration cost 概念，唯一的"够不够闲"判据是一个常量：
  `[walt_lb.c:804](../../kernel/kernel/sched/walt/walt_lb.c#L804)` 的
  `NEWIDLE_BALANCE_THRESHOLD 500000`，注释明说是对标 `sysctl_sched_migration_cost`。
  `enough_idle` 只决定"要不要多扫一个簇"，不是硬门禁。

进函数第一件事就交所有权：

```c
/* walt_newidle_balance() */
*done = 1;
*pulled_task = 0;
```

`done=1` 让 [fair.c:11163](../../kernel/kernel/sched/fair.c#L11163) 直接返回，原生 newidle 一行不执行。

### 2.3 早退条件与前置动作

按 [walt_lb.c:818-864](../../kernel/kernel/sched/walt/walt_lb.c#L818-L864) 的顺序：

1. `walt_disabled` → 返回（此时**不设** `*done`，交回原生）
2. 设 `*done = 1`、`*pulled_task = 0`
3. `this_rq->misfit_task_load = 0; this_rq->idle_stamp = rq_clock(this_rq);`
   —— 自己马上要睡，先清 misfit 标记，否则别的 CPU 一直以为这里压着大任务
4. `!cpu_active` / `cpu_halted(this_cpu)` / `is_reserved(this_cpu)` → 返回
5. `rq_unpin_lock()` 放锁
6. `walt_balance_rt(this_rq)`（§8.1）或 `this_rq->nr_running` 非零 → 跳 `rt_pulled`
7. `!force_overload && !READ_ONCE(this_rq->rd->overload)` → `repin`
8. `atomic_read(&this_rq->nr_iowait) && !enough_idle` → `repin`
9. 算 `help_min_cap = should_help_min_cap(this_cpu)`，放锁，开始扫

`should_help_min_cap()` [walt_lb.c:788](../../kernel/kernel/sched/walt/walt_lb.c#L788)
读 `sysctl_sched_force_lb_enable`（定义 [walt_lb.c:787](../../kernel/kernel/sched/walt/walt_lb.c#L787)，
sysctl 节点 [sysctl.c:845](../../kernel/kernel/sched/walt/sysctl.c#L845)）：

```c
if (!sysctl_sched_force_lb_enable || is_min_cluster_cpu(this_cpu))
        return false;
for_each_cpu(cpu, &cpu_array[0][0]) {
        if (walt_big_tasks(cpu))
                return true;
}
return false;
```

[反直觉] `help_min_cap` **算出来却没有任何分支消费**，只作为参数进
`trace_walt_newidle_balance()` [walt_lb.c:991](../../kernel/kernel/sched/walt/walt_lb.c#L991)。
`[待确认]` 是重构残留还是刻意留的观测信号。**别指望改 `sysctl_sched_force_lb_enable` 会影响 LB 行为。**

### 2.4 `sysctl_sched_newidle_balance` 不存在

`grep -n "newidle\|force_lb" walt/sysctl.c` 只有：

```
845:		.procname	= "sched_force_lb_enable",
846:		.data		= &sysctl_sched_force_lb_enable,
```

newidle 行为**没有运行时开关**。唯二的运行时杠杆是 `sysctl_sched_walt_rotate_big_tasks`（§5）
和 `sysctl_sched_asymcap_boost`（§3.3），二者都只间接改变 busiest 选择。

### 2.5 扫描策略：按 `cpu_array` 而不是 sched_group

`cpu_array` 是 WALT 自建的二维簇掩码表，`cpu_array[i][0]` 是簇 `i` 自己，
之后从 `i+1` 升序、再从 `i-1` 降序。构建于
`build_cpu_array()` [walt.c:2639](../../kernel/kernel/sched/walt/walt.c#L2639)：

```c
/* build_cpu_array() */
cpumask_copy(&cpu_array[i][0], &sched_cluster[i]->cpus);
for (j = i + 1; j < num_sched_clusters; j++)   /* 升序 */
        cpumask_copy(&cpu_array[i][k++], &sched_cluster[j]->cpus);
for (j = i - 1; j >= 0; j--)                    /* 从 i 往下降序 */
        cpumask_copy(&cpu_array[i][k++], &sched_cluster[j]->cpus);
```

即扫描优先级 = 本簇 → 更高的簇（由近及远）→ 更低的簇（由近及远）。
[反直觉] 不是字典序：3 簇机器上 `cpu_array[0]` 是 `[小,中,大]`、`[1]` 是 `[中,大,小]`、`[2]` 是 `[大,中,小]`
—— **高容量永远优先于低容量**。

`walt_newidle_balance()` 按 `num_sched_clusters` 硬编码展开两套：

- **2 簇** [walt_lb.c:872](../../kernel/kernel/sched/walt/walt_lb.c#L872)：先本簇 `[0]`；再看 `enough_idle` 决定扫不扫 `[1]`
- **3 簇** [walt_lb.c:892](../../kernel/kernel/sched/walt/walt_lb.c#L892) 起，按 `order_index`（= `wrq->cluster->id`）分三种：
  - `== 0`：本簇 → `enough_idle` 时 `[1]` → **无条件**扫 `[2]`；`[2]` 有货且旁边没人眼红时，
    改为 `walt_kick_cpu(first_idle)` 踢别的 CPU 去搬
  - `== 2`：本簇 → `[1]`（需 `enough_idle || has_misfit`）→ `[2]`，同样可能降级成 kick
  - 中间簇：本簇 → `enough_idle` 时 `[2]` → `[1]`（需 `enough_idle || has_misfit`）

`find_first_idle_if_others_are_busy()` [walt_lb.c:436](../../kernel/kernel/sched/walt/walt_lb.c#L436)
是"别做无用功"优化：目标簇里若存在 `cpu_util(i) < SMALL_TASK_THRESHOLD`（=102，
[walt_lb.c:424](../../kernel/kernel/sched/walt/walt_lb.c#L424)）且 `nr_running == 1` 的 CPU，
它马上要空出来自己做 newidle，此刻踢它是浪费，返回 -1 放弃。

### 2.6 收尾

```c
/* walt_newidle_balance() 尾部 */
if (this_rq->cfs.h_nr_running && !*pulled_task)  *pulled_task = 1;
if (this_rq->nr_running != this_rq->cfs.h_nr_running)
        *pulled_task = -1;              /* 有非 CFS 任务 → 调用者重选 */
if (*pulled_task)  this_rq->idle_stamp = 0;   /* 没睡成，清 idle 计时 */
rq_repin_lock(this_rq, rf);
```

`-1` 是原生 `newidle_balance()` 的约定，`__schedule()` 靠它决定要不要重新 pick。

---

## 3. busiest CPU 选择：三档策略

### 3.1 分派

`walt_lb_find_busiest_cpu()` [walt_lb.c:618](../../kernel/kernel/sched/walt/walt_lb.c#L618) 是统一入口，
newidle 与周期 LB 都走它：

```c
/* walt_lb_find_busiest_cpu() */
if (ignore_cluster_valid(NULL, cpu_rq(dst_cpu)))
        return -1;
if (dst_wrq->cluster->id == fsrc_wrq->cluster->id)
        busiest_cpu = walt_lb_find_busiest_similar_cap_cpu(...);
else if (dst_wrq->cluster->id > fsrc_wrq->cluster->id)
        busiest_cpu = walt_lb_find_busiest_from_lower_cap_cpu(...);
else
        busiest_cpu = walt_lb_find_busiest_from_higher_cap_cpu(...);
```

判据是**目标 CPU 的簇 id 与源掩码首个 CPU 的簇 id**（`fsrc_cpu = cpumask_first(src_mask)`）。
`ignore_cluster_valid()` [walt.h:1090](../../kernel/kernel/sched/walt/walt.h#L1090)
是"该簇频率已经够高，不必再跨簇搬"的开关（依赖 `cluster_arr` 频率关系表）。

### 3.2 三个变体

三者都把"忙"定义为 `walt_lb_cpu_util(i)` [walt_lb.c:12](../../kernel/kernel/sched/walt/walt_lb.c#L12)：

```c
return wrq->walt_stats.cumulative_runnable_avg_scaled;
```

即 WALT 窗口累计可运行时间（字段含义见 [03-data-structures.md](../00-overview/03-data-structures.md) §2.2/§3），
**不用 `cfs_rq` 的 load / PELT 的 util_avg**。

| 变体 | 位置 | 关键附加条件 |
|---|---|---|
| 同容量 | [walt_lb.c:463](../../kernel/kernel/sched/walt/walt_lb.c#L463) | `nr_running >= 2 && cfs.h_nr_running`；纯取 util 最大 |
| 从更高容量簇往下 | [walt_lb.c:489](../../kernel/kernel/sched/walt/walt_lb.c#L489) | 见下 |
| 从更低容量簇往上 | [walt_lb.c:549](../../kernel/kernel/sched/walt/walt_lb.c#L549) | 见下 |

**往下（`from_higher_cap`，dst 在高容量簇）**：
跳过 `cfs.h_nr_running == 2 && task_util(curr) < SMALL_TASK_THRESHOLD` 的簇；
`if (!walt_rotation_enabled && !cpu_overutilized(i) && !asymcap_boost) continue;`
—— 除非开了轮转或有 asymcap boost，**必须 `cpu_overutilized()` 才认账**；最后一道闸
（[walt_lb.c:541](../../kernel/kernel/sched/walt/walt_lb.c#L541)）：

```c
if (!walt_rotation_enabled && !asymcap_boost) {
        if (total_nr <= total_cpus || total_util * 1280 < total_capacity * 1024)
                busiest_cpu = -1;
}
```

即源簇整体要么任务数超过 CPU 数，要么利用率超过容量的 80%（1024/1280），否则不往下搬。
`1280/1024` 是硬编码的 1.25 倍，`[待确认]` 未找到对应 sysctl。

**往上（`from_lower_cap`，dst 在高容量簇）**：
`active_balance` 为真则跳过；`if (cfs.h_nr_running < 2 && (!walt_big_tasks(i) || !treat_dst_idle)) continue;`
—— **只有一个任务的源 CPU 也要，前提是它有大任务且目标空闲**（misfit 上迁的解药）。
`busy_nr_big_tasks` 会在结尾置 `*has_misfit = true`
（[walt_lb.c:612](../../kernel/kernel/sched/walt/walt_lb.c#L612)），该出参改变 §2.5 里
`order_index == 2` 要不要扫中间簇。

### 3.3 `walt_rotation_enabled` / `ASYMCAP_BOOST` 是"松绑"而非收紧

```c
/* walt.h:996 */
#define ASYMCAP_BOOST(cpu)  (sysctl_sched_asymcap_boost && !is_min_cluster_cpu(cpu))
```

两者打开时都让"非小簇 CPU 无视 `cpu_overutilized()` 和 80% util 闸门"。轮转那条的理由写在源码里
（[walt_lb.c:520-525](../../kernel/kernel/sched/walt/walt_lb.c#L520-L525)）：

```c
/*
 * During rotation, two silver fmax tasks gets
 * placed on gold/prime and the CPU may not be
 * overutilized but for rotation, we have to spread out.
 */
```

轮转会把两个小簇 fmax 任务塞到金/超大核，此时这些 CPU 的 WALT util 可能并未超容量，
用正常 overutilized 判据根本选不出来，必须强行放行。

### 3.4 周期 LB 的入口

`walt_find_busiest_queue()` [walt_lb.c:1023](../../kernel/kernel/sched/walt/walt_lb.c#L1023)：

```c
/* walt_find_busiest_queue() */
*done = 1;  *busiest = NULL;
if (same_cluster(dst_cpu, fsrc_cpu)) {
        busiest_cpu = fsrc_cpu;      /* 同簇只有一个 CPU，直接选 */
        goto done;
}
cpumask_and(&src_mask, sched_group_span(group), env_cpus);
busiest_cpu = walt_lb_find_busiest_cpu(dst_cpu, &src_mask, &has_misfit, false);
```

`is_newidle = false` 使 `from_lower_cap` 的 `treat_dst_idle` 退化为 `available_idle_cpu(dst_cpu)`。
注释（[walt_lb.c:1047-1056](../../kernel/kernel/sched/walt/walt_lb.c#L1047-L1056)）明说"跨簇迁移只在源 group 足够忙时才允许，
上游 load balancer 比我们宽松"——所以 WALT 在**周期 LB 上比原生更保守**，在 newidle 上则完全是另一套。

---

## 4. 搬运：`walt_lb_pull_tasks()` 与 active balance

### 4.1 三轮扫描

`walt_lb_pull_tasks()` [walt_lb.c:289](../../kernel/kernel/sched/walt/walt_lb.c#L289)
持 `src_rq` 锁反向遍历 `cfs_tasks`，每轮最多看 5 个（`task_visited > 5` 即 break）：

1. `force=false`，`to_lower` 选 util **最小**、否则选 **最大** —— 下行搬轻的、上行搬重的
2. `force=true` 再扫一遍，挑选规则同上
3. 只找 `task_running()` 且 `need_active_lb()` 成立的任务 → active balance

```c
/* need_active_lb()，walt_lb.c:270 */
if (cpu_rq(src_cpu)->active_balance)              return false;
if (dst_wrq->cluster->id <= src_wrq->cluster->id) return false;
if (!wts->misfit)                                 return false;
return true;
```

即：**只有 misfit、且往更高容量簇、且源 CPU 当前没有别的 active balance** 才主动迁移。

### 4.2 自己的 stopper

`stop_walt_lb_active_migration()` [walt_lb.c:33](../../kernel/kernel/sched/walt/walt_lb.c#L33)
在 `src_cpu` 上被 stop 时执行，先一串 sanity check（CPU 仍 active、
`busiest_cpu == raw_smp_processor_id()`、`active_balance` 仍在、`nr_running > 1`），
再 `walt_detach_task()` + `walt_attach_task()`。动机见
[walt_lb.c:393-396](../../kernel/kernel/sched/walt/walt_lb.c#L393-L396)：

```c
/*
 * Using our custom active load balance callback so that
 * the push_task is really pulled onto this CPU.
 */
```

它 `return 0` 并注明 `/* we did not pull any task here */`
（[walt_lb.c:405](../../kernel/kernel/sched/walt/walt_lb.c#L405)）——真正的搬运在 stop 回调里发生。

### 4.3 detach / attach 是 WALT 自己的版本

```c
/* walt_detach_task()，walt_lb.c:19 */
//TODO can we just replace with detach_task in fair.c??
deactivate_task(src_rq, p, 0);
set_task_cpu(p, dst_rq->cpu);
```

`walt_attach_task()` [walt_lb.c:27](../../kernel/kernel/sched/walt/walt_lb.c#L27) 是
`activate_task()` + `check_preempt_curr()`。
[反直觉] 两者**没有**做原生 `attach_task()` 的 `p->on_rq` / `se.cfs_rq` 维护，
而是靠 `deactivate_task()` / `activate_task()` 内部的 hook
（`android_rvh_dequeue_task_fair` 等）驱动 WALT 的迁移簿记（见 [window-model.md](01-window-model.md)）。
源码作者自己都打了 TODO，这是**已知技术债**，不是设计。

---

## 5. 大任务轮转（big task rotation）

### 5.1 先纠正几个名字

| 描述中的名字 | 实际情况 |
|---|---|
| `walt_rotation_enabled` | **存在** [walt.c:63](../../kernel/kernel/sched/walt/walt.c#L63) |
| `sysctl_sched_big_task_rotation_us` | **不存在**。真正开关是 `sysctl_sched_walt_rotate_big_tasks`（[sysctl.c:650](../../kernel/kernel/sched/walt/sysctl.c#L650)），**布尔**不是微秒数 |
| `rotate_heavy_to_random_cpu()` | **不存在**。实际函数是 `walt_lb_check_for_rotation()` [walt_lb.c:134](../../kernel/kernel/sched/walt/walt_lb.c#L134) |
| `walt_rotation_checkpoint` | **存在** [walt.c:4260](../../kernel/kernel/sched/walt/walt.c#L4260)（是函数不是变量） |

名字相近的 `rearrange_heavy` [walt.c:3809](../../kernel/kernel/sched/walt/walt.c#L3809) 是另一件事：
heavy task / topapp 的跨窗口重排，走 `walt_irq_work` 不走 LB（§9）。

### 5.2 开关怎么翻

```c
/* walt_rotation_checkpoint()，walt.c:4260 */
if (!hmp_capable())
        return;
if (!sysctl_sched_walt_rotate_big_tasks || sched_boost_type != NO_BOOST) {
        walt_rotation_enabled = 0;
        return;
}
walt_rotation_enabled = nr_big >= num_possible_cpus();
```

- 唯一调用点 `walt_rotation_checkpoint()` [core_ctl.c:792](../../kernel/kernel/sched/walt/core_ctl.c#L792) 位于
  `update_running_avg()` [core_ctl.c:753](../../kernel/kernel/sched/walt/core_ctl.c#L753) 的尾部，
  即**每个窗口滚动后由 core_ctl 的统计更新路径评估一次**
  （该函数由 `core_ctl_check()` [core_ctl.c:1129](../../kernel/kernel/sched/walt/core_ctl.c#L1129) 调用）
- `nr_big` 是各簇 `cluster_real_big_tasks()` 之和；条件是**大任务数 ≥ 所有 possible CPU 数**，
  即"大任务多到必须先摊开、让每个 CPU 都当一次大核"
- `sched_boost_type != NO_BOOST` 时强制关（[walt.h:342](../../kernel/kernel/sched/walt/walt.h#L342) 定义 `NO_BOOST`）
- `sysctl_sched_walt_rotate_big_tasks` 是 `unsigned int` 且**未显式初始化**
  （[sysctl.c:62](../../kernel/kernel/sched/walt/sysctl.c#L62) 只声明无初值）——
  [反直觉] BSS 归零，**默认关闭**。

### 5.3 轮转实际做什么：`walt_lb_check_for_rotation()`

走 `walt_lb_tick()` 的 tick 路径，且**只在最小容量簇的 CPU 上发起**：

```c
/* walt_lb_check_for_rotation()，walt_lb.c:134 */
if (!is_min_cluster_cpu(src_cpu))
        return;
wc = src_rq->clock;   /* 没持 rq 锁，用 tick 刚更新过的 clock */
```

两段挑选（[walt_lb.c:153-201](../../kernel/kernel/sched/walt/walt_lb.c#L153-L201)）：

1. 在**所有小簇 CPU** 找"等得最久的大任务"：条件
   `rq->misfit_task_load && walt_fair_task(rq->curr)`，取
   `wait = wc - wts->last_enqueued_ts` 最大者记为 `deserved_cpu`；
   若 `deserved_cpu != src_cpu` 立即返回 ——
   **只有"最该得到大核"的那个小簇 CPU 有资格发起轮转**
2. 在**非小簇 CPU** 找被换走的对象：`walt_fair_task(rq->curr)`、`rq->nr_running == 1`、
   且 `run = wc - wts->last_enqueued_ts >= WALT_ROTATION_THRESHOLD_NS`
   （16000000 ns = 16ms，[walt_lb.c:133](../../kernel/kernel/sched/walt/walt_lb.c#L133)），取 `run` 最大者

`[推测]` 16ms 对标窗口默认长度（见 [CONVENTIONS §4.1](../CONVENTIONS.md)），
含义是"这个任务已独占大核整整一个窗口，该让出来"。

然后是一次 **任务互换**而非单向迁移：

```c
/* walt_lb_check_for_rotation() 收尾 */
double_rq_lock(src_rq, dst_rq);
if (walt_fair_task(dst_rq->curr) &&
    !src_rq->active_balance && !dst_rq->active_balance &&
    cpumask_test_cpu(dst_cpu, src_rq->curr->cpus_ptr) &&
    cpumask_test_cpu(src_cpu, dst_rq->curr->cpus_ptr)) {
        get_task_struct(src_rq->curr);  get_task_struct(dst_rq->curr);
        mark_reserved(src_cpu);         mark_reserved(dst_cpu);
        wr->src_task = src_rq->curr;    wr->dst_task = dst_rq->curr;
        dst_rq->active_balance = 1;     src_rq->active_balance = 1;
}
double_rq_unlock(src_rq, dst_rq);
if (wr)
        queue_work_on(src_cpu, system_highpri_wq, &wr->w);
```

实际搬运在 workqueue 上（`walt_lb_rotate_work_func()` [walt_lb.c:99](../../kernel/kernel/sched/walt/walt_lb.c#L99)），
用原生 `migrate_swap()` 原子交换：

```c
/* walt_lb_rotate_work_func() */
migrate_swap(wr->src_task, wr->dst_task, wr->dst_cpu, wr->src_cpu);
put_task_struct(wr->src_task);  put_task_struct(wr->dst_task);
/* 之后清两个 rq 的 active_balance 与 reserved */
```

[反直觉] 两边都打 `active_balance = 1`，两个 CPU 的 active balance 判定同时被"占位"，
防止原生 `load_balance()` 的 active balance 路径插进来抢同一个 CPU。
`walt_lb_rotate_works` 是 `DEFINE_PER_CPU`（[walt_lb.c:97](../../kernel/kernel/sched/walt/walt_lb.c#L97)），
由 `walt_lb_rotate_work_init()` [walt_lb.c:122](../../kernel/kernel/sched/walt/walt_lb.c#L122) 在 `walt_lb_init()` 里 `INIT_WORK`。

### 5.4 轮转开启时 tick 不再走 misfit 上迁

```c
/* walt_lb_tick()，walt_lb.c:670 */
if (walt_rotation_enabled) {
        walt_lb_check_for_rotation(rq);
        goto out_unlock;
}
```

两种策略**互斥**：大任务已多到需要轮转时，单点 misfit 上迁没意义（搬上去也会被换下来），
于是把 tick 路径整体让给轮转。

### 5.5 轮转还会外溢到调频侧

`walt_rotation_enabled` 不只是 LB 开关：它进 `trace_sched_load_to_gov`
[walt.c:630](../../kernel/kernel/sched/walt/walt.c#L630)，并经
`walt_load->big_task_rotation = walt_rotation_enabled;`
[walt.c:666](../../kernel/kernel/sched/walt/walt.c#L666) 被 governor 读到
（[cpufreq_walt.c:313](../../kernel/kernel/sched/walt/cpufreq_walt.c#L313)、
[cpufreq_walt.c:344](../../kernel/kernel/sched/walt/cpufreq_walt.c#L344)）。
所以改 `sched_walt_rotate_big_tasks` 会同时改变**任务分布**和**频率选择**，调参别只盯 LB（见 [cpufreq.md](03-cpufreq.md)）。

---

## 6. misfit 与 upmigrate

### 6.1 misfit 判定不在 LB 侧

`rq->misfit_task_load` 由 `android_rvh_update_misfit_status` 写入
[walt.c:4722](../../kernel/kernel/sched/walt/walt.c#L4722)（原生调用点 [fair.c:4251](../../kernel/kernel/sched/fair.c#L4251)）：

```c
/* android_rvh_update_misfit_status() */
if (!p) { rq->misfit_task_load = 0; return; }
if (task_fits_max(p, rq->cpu))
        rq->misfit_task_load = 0;
else
        rq->misfit_task_load = task_util(p);   /* = wts->demand_scaled */
```

`task_fits_max()` [walt.h:810](../../kernel/kernel/sched/walt/walt.h#L810) **不是**原生的
`task_fits_capacity()`，里面带 boost 策略、`task_boost`、`walt_uclamp_boosted` 以及
`walt_should_kick_upmigrate(p, cpu)`。同一 hook 后半段还在 `wts->misfit` 上做差量记账，
`need_active_lb()`（§4.1）用的就是它。

### 6.2 tick 路径：WALT 自己的 misfit 主动迁移

`walt_lb_tick()` [walt_lb.c:643](../../kernel/kernel/sched/walt/walt_lb.c#L643) 由
`android_vh_scheduler_tick` [walt.c:4806](../../kernel/kernel/sched/walt/walt.c#L4806)
每 tick 调用（[walt.c:4830](../../kernel/kernel/sched/walt/walt.c#L4830)）。前半段：

```c
/* walt_lb_tick() */
if (available_idle_cpu(prev_cpu) && is_reserved(prev_cpu) && !rq->active_balance)
        clear_reserved(prev_cpu);      /* 预留位泄漏的兜底清理 */
if (!walt_fair_task(p)) return;        /* prio < MAX_RT_PRIO 或 idle，不管 */
walt_cfs_tick(rq);                     /* MVP 记账 */
if (!rq->misfit_task_load) return;
if (READ_ONCE(p->__state) != TASK_RUNNING || p->nr_cpus_allowed == 1) return;
```

然后拿全局裸自旋锁 `walt_lb_migration_lock`（[walt_lb.c:642](../../kernel/kernel/sched/walt/walt_lb.c#L642)，
只为串行化"重跑一遍 tick LB"）：

```c
/* walt_lb_tick() */
new_cpu = walt_find_energy_efficient_cpu(p, prev_cpu, 0, 1);
if (new_cpu < 0) goto out_unlock;
/* prevent active task migration to busy or same/lower capacity CPU */
if (!available_idle_cpu(new_cpu) || new_wrq->cluster->id <= prev_wrq->cluster->id)
        goto out_unlock;
rq->active_balance = 1;
rq->push_cpu = new_cpu;
prev_wrq->push_task = p;
mark_reserved(new_cpu);
stop_one_cpu_nowait(prev_cpu, stop_walt_lb_active_migration, rq,
                    &rq->active_balance_work);
if (!ret) clear_reserved(new_cpu); else wake_up_if_idle(new_cpu);
```

关键点：**用的是放置侧的能量模型** `walt_find_energy_efficient_cpu()`
[walt.h:843](../../kernel/kernel/sched/walt/walt.h#L843)（见 [placement.md](04-placement.md)），
只保留"目标必须空闲且簇更高"两条收敛条件；第 4 个实参传 1 `[待确认]`
（需对照函数签名确认其含义，本文按位置描述）。
`push_cpu` 是原生 `rq` 字段，`push_task` 是 `wrq->push_task`
[walt.h:99](../../kernel/kernel/sched/walt/walt.h#L99)，两者一起被 `stop_walt_lb_active_migration()` 消费；
搬成功后 `wake_up_if_idle(new_cpu)`（[core.c:3829](../../kernel/kernel/sched/core.c#L3829)），
否则目标 CPU 可能还在睡、不会来 pick。

### 6.3 `walt_should_kick_upmigrate()` 不是 LB 函数

[walt.h:747](../../kernel/kernel/sched/walt/walt.h#L747)：

```c
static inline bool walt_should_kick_upmigrate(struct task_struct *p, int cpu)
{
        struct walt_task_struct *wts = (struct walt_task_struct *) p->android_vendor_data1;
        struct walt_related_thread_group *rtg = wts->grp;

        if (is_suh_max() && rtg && rtg->id == DEFAULT_CGROUP_COLOC_ID &&
                            rtg->skip_min && wts->unfilter)
                return is_min_cluster_cpu(cpu);
        return false;
}
```

唯一使用者是 `task_fits_max()` 里的一行（[walt.h:822](../../kernel/kernel/sched/walt/walt.h#L822)）：

```c
/* task_fits_max()，walt.h:818-823 */
if (is_min_cluster_cpu(cpu)) {
        if (task_boost_policy(p) == SCHED_BOOST_ON_BIG ||
                        task_boost > 0 ||
                        walt_uclamp_boosted(p) ||
                        walt_should_kick_upmigrate(p, cpu))
                return false;
}
```

作用：**让"小簇 CPU 上的这个任务"被判为"放不下"**，于是 (a) `task_fits_max` 为假 →
`misfit_task_load` 置位 → 触发 §6.2 的 tick 上迁；(b) `_walt_can_migrate_task` 里
`!task_fits_max(p, dst_cpu)` 挡住下行迁移。三个前提缺一不可：
`is_suh_max()`、RTG 为 `DEFAULT_CGROUP_COLOC_ID`
（[walt.h:746](../../kernel/kernel/sched/walt/walt.h#L746)）且 `rtg->skip_min`、`wts->unfilter` 非零。

### 6.4 `sysctl_sched_min_task_util_for_colocation` 与 upmigrate 的间接关系

默认 **35**（[sysctl.c:69](../../kernel/kernel/sched/walt/sysctl.c#L69)，节点 [sysctl.c:678](../../kernel/kernel/sched/walt/sysctl.c#L678)），
在 `update_history()` [walt.c:1984](../../kernel/kernel/sched/walt/walt.c#L1984) 里被用：

```c
/* update_history()，walt.c:2054 */
if (demand_scaled > sysctl_sched_min_task_util_for_colocation)
        wts->unfilter = sysctl_sched_task_unfilter_period;
else if (wts->unfilter)
        wts->unfilter = max_t(int, 0, wts->unfilter - wrq->prev_window_size);
```

即任务的 `demand_scaled` 一旦超过 35 就获得一段"免过滤期"（时长由
`sysctl_sched_task_unfilter_period` 决定，默认 100000000 ns，[sysctl.c:1120](../../kernel/kernel/sched/walt/sysctl.c#L1120)）。
链条因此接上 §6.3：

```
demand_scaled > 35 → wts->unfilter != 0 → walt_should_kick_upmigrate() 可能为真
                   → task_fits_max() 为假 → misfit 置位 / 拒绝下行迁移
```

[反直觉] 名字叫 colocation，实际效果是**上迁许可阈值**：小于 35 的任务不值得为它打破 colocation
（RTG 聚拢），超过 35 才允许把它从被 colocate 的 CPU 上撬走。`[推测]` 这才是命名的由来。

---

## 7. 与 `walt_halt` 的交互

### 7.1 不存在 `walt_halt_lb_*` API

`grep -rn "walt_halt_lb" kernel/kernel/sched/walt/` **无任何匹配**。
LB 侧用的是两个通用件：

| 机制 | 定义 | 在 LB 里的用法 |
|---|---|---|
| `cpu_halted(cpu)` 宏 | [walt.h:1009](../../kernel/kernel/sched/walt/walt.h#L1009) | 拒绝迁入已 halt 的 CPU |
| `is_reserved` / `mark_reserved` / `clear_reserved` | [walt.h:899](../../kernel/kernel/sched/walt/walt.h#L899) / [907](../../kernel/kernel/sched/walt/walt.h#L907) / [915](../../kernel/kernel/sched/walt/walt.h#L915) | WALT 自有的"该 CPU 已被某次迁移预定"标志（`CPU_RESERVED`，存于 `wrq->walt_flags` [walt.h:106](../../kernel/kernel/sched/walt/walt.h#L106)） |

```c
/* _walt_can_migrate_task()，walt_lb.c:263-265 */
/* Don't detach task if dest cpu is halted */
if (cpu_halted(dst_cpu))
        return false;
```

```c
/* walt_newidle_balance()，walt_lb.c:838-839 */
if (cpu_halted(this_cpu))
        return;
```

第一条是**保护**：halt 的 CPU 马上要被 drain（`halt_cpus()` [walt_halt.c:276](../../kernel/kernel/sched/walt/walt_halt.c#L276)
会 `stop_one_cpu` 把任务搬空），此刻往里迁任务纯属白搬。第二条是**自保**。
`is_reserved` 语义不同，它是**短期互斥**，防止两个并发 WALT 迁移选中同一目标；
标记点见 §2.3 步骤 4、§4.1、§5.3、§6.2，清理点在各 stop 回调与工作函数末尾，
另在 `walt_lb_tick()` 开头有一处兜底（[walt_lb.c:653](../../kernel/kernel/sched/walt/walt_lb.c#L653)）。

### 7.2 反向：unhalt 后主动跑一次 newidle

这是 LB 与 halt 之间**唯一的直接耦合**。`start_cpus()`
[walt_halt.c:323](../../kernel/kernel/sched/walt/walt_halt.c#L323) 把 CPU 移出 `cpu_halt_mask` 后逐个 kick：

```c
/* start_cpus()，walt_halt.c:337-344 */
cpumask_clear_cpu(cpu, cpu_halt_mask);
/* kick the cpu so it can pull tasks
 * after the mask has been cleared.
 */
walt_smp_call_newidle_balance(cpu);
```

这条路径进 `walt_smp_newidle_balance()`，**`force_overload = true`**
（[walt_lb.c:1006](../../kernel/kernel/sched/walt/walt_lb.c#L1006)）。它正是用来绕过
`!READ_ONCE(this_rq->rd->overload)` 那道早退（§2.3 步骤 7）：刚解除 halt 的 CPU 上可能压根没任务，
`rd->overload` 大概率是 0，不强制这次 newidle 什么都拉不到，halt 就白解除了。
[反直觉] `walt_smp_call_newidle_balance()` 是**异步**投到目标 CPU 的 `nib_csd` 上执行，
不在调用者上下文。halt 侧的 stop/drain 机制见 [power-side.md](07-power-side.md)。

---

## 8. 另外两块被接管的东西

### 8.1 RT 抢先拉：`walt_balance_rt()`

[walt_lb.c:723](../../kernel/kernel/sched/walt/walt_lb.c#L723)，在 `walt_newidle_balance()` 里最先执行
（[walt_lb.c:854](../../kernel/kernel/sched/walt/walt_lb.c#L854)）：全局找第一个有 pushable RT 的 CPU，
`double_lock_balance()` 后 `pick_highest_pushable_task()`，然后

```c
/* walt_balance_rt() */
wallclock = max(this_rq->clock, src_rq->clock);
if (wallclock > wts->last_wake_ts &&
                wallclock - wts->last_wake_ts < WALT_RT_PULL_THRESHOLD_NS)
        goto unlock;              /* 刚被唤醒的 RT 不许搬 */
```

`WALT_RT_PULL_THRESHOLD_NS = 250000`（250 µs，[walt_lb.c:722](../../kernel/kernel/sched/walt/walt_lb.c#L722)）。
这是原生 `push_rt_task`/`pull_rt_task` 没有的反抖动措施：250 µs 内刚 wake 的 RT 不动它，让它自己跑。
原生 RT 均衡见 [rt-mvp.md](09-rt-mvp.md)。

### 8.2 `walt_nohz_balancer_kick()`

[walt_lb.c:1074](../../kernel/kernel/sched/walt/walt_lb.c#L1074)：

```c
/* walt_nohz_balancer_kick() */
*done = 1;
/* tick path migration takes care of misfit task.
 * so we have to check for nr_running >= 2 here. */
if (rq->nr_running >= 2 && cpu_overutilized(rq->cpu)) {
        *flags = NOHZ_KICK_MASK;
        trace_walt_nohz_balance_kick(rq);
}
```

`NOHZ_KICK_MASK` 定义在 [sched.h:2800](../../kernel/kernel/sched/sched.h#L2800)，
最终由 `walt_kick_cpu()` [walt.c:3255](../../kernel/kernel/sched/walt/walt.c#L3255) 发 IPI。
判据被压到极简，注释给了理由：**单任务的 misfit 由 §6.2 的 tick 路径负责**。
函数头长注释（[walt_lb.c:1066-1073](../../kernel/kernel/sched/walt/walt_lb.c#L1066-L1073)）
还解释了为何不做"挑哪个 CPU 起来"的优化：nohz idle 的第一个 CPU 会代表所有 CPU 做 LB，
而 kick 只有一次机会，挑不出来。

---

## 9. `is_migration`：`walt_irq_work` 的两条路

`walt_irq_work()` [walt.c:4213](../../kernel/kernel/sched/walt/walt.c#L4213) 用**一个 irq_work 指针**
区分两种工作：

```c
/* walt_irq_work() */
if (irq_work == &walt_migration_irq_work)
        is_migration = true;
cpumask_copy(&lock_cpus, cpu_possible_mask);
if (is_migration) {
        irq_work_restrict_to_mig_clusters(&lock_cpus);
        if (cpumask_empty(&lock_cpus))
                return;             /* 迁移工作已被前一次调用处理掉 */
}
... 锁住 lock_cpus 上所有 rq ...
__walt_irq_work_locked(is_migration, &lock_cpus);
... 解锁 ...
if (!is_migration) {
        wrq = (struct walt_rq *) this_rq()->android_vendor_data1;
        find_heaviest_topapp(wrq->window_start);
        rearrange_heavy(wrq->window_start);
        rearrange_pipeline_preferred_cpus(wrq->window_start);
        core_ctl_check(wrq->window_start);
}
```

`is_migration` 在 `__walt_irq_work_locked()` [walt.c:3992](../../kernel/kernel/sched/walt/walt.c#L3992) 里影响 4 件事：

| 差异点 | `is_migration = true` | `false`（rollover） |
|---|---|---|
| 锁范围 | `irq_work_restrict_to_mig_clusters()` [walt.c:4137](../../kernel/kernel/sched/walt/walt.c#L4137) 缩到涉及的簇 | 全部 possible CPU |
| 窗口报数 | 不更新 `walt_load_reported_window` | 更新 [walt.c:4004](../../kernel/kernel/sched/walt/walt.c#L4004) |
| governor 标志 | `WALT_CPUFREQ_IC_MIGRATION`（仅 `notif_pending` 的 CPU） | `WALT_CPUFREQ_ROLLOVER`（所有 CPU） |
| 额外工作 | 处理 `notif_pending` / `is_asym_migration` | 改 `sched_ravg_window`、`walt_update_irqload()` |

`WALT_CPUFREQ_CONTINUE`（[walt.h:322](../../kernel/kernel/sched/walt/walt.h#L322) 定义 `0x2`）
与 `is_migration` **正交**，它是批处理标志（[walt.c:4090-4093](../../kernel/kernel/sched/walt/walt.c#L4090-L4093)）：

```c
if (i == num_cpus)
        waltgov_run_callback(cpu_rq(cpu), wflag);
else
        waltgov_run_callback(cpu_rq(cpu), wflag | WALT_CPUFREQ_CONTINUE);
```

同簇多 CPU 分多次回调，除最后一个都带 `CONTINUE`，让 governor 知道"后面还有同一批的"从而不做重复聚合
（消费侧 [cpufreq_walt.c:438](../../kernel/kernel/sched/walt/cpufreq_walt.c#L438)，详见 [cpufreq.md](03-cpufreq.md)）。

[反直觉] **迁移路径不改 `sched_ravg_window`**（[walt.c:4112](../../kernel/kernel/sched/walt/walt.c#L4112) 的 `if (!is_migration)`）。
窗口长度变更必须在一个诚实的 rollover 上做，否则 CPU 计数器（`prs` / `crs`）会滚不干净
（源码注释有完整解释，见 [window-model.md](01-window-model.md)）。

---

## 10. 统计与可观测性

### 10.1 没有 `wrq->lb_*` 字段

`grep -n "lb_" walt.h` 只匹配到 `walt_lb_init` / `walt_lb_tick` 两个函数声明。
**WALT 没有 per-CPU 或 per-cluster 的"LB 统计"结构**，LB 消费的都是通用字段：
`wrq->walt_stats.cumulative_runnable_avg_scaled`（busiest 判据，[walt.h:77](../../kernel/kernel/sched/walt/walt.h#L77)）、
`wrq->walt_flags` 的 `CPU_RESERVED` 位（[walt.h:106](../../kernel/kernel/sched/walt/walt.h#L106)）、
`wrq->push_task`（[walt.h:99](../../kernel/kernel/sched/walt/walt.h#L99)）、
`walt_big_tasks()` [walt.c:515](../../kernel/kernel/sched/walt/walt.c#L515) 读的 `nr_big_tasks`（[walt.h:75](../../kernel/kernel/sched/walt/walt.h#L75)）。
[反直觉] `cluster->aggr_grp_load`（[walt.h:150](../../kernel/kernel/sched/walt/walt.h#L150)）名字像 LB 统计，
实际是**调频用的** RTG 聚合负载，LB 侧不读它。字段总表见 [03-data-structures.md](../00-overview/03-data-structures.md)。

### 10.2 tracepoint

| tracepoint | 定义 | 埋点 |
|---|---|---|
| `walt_lb_cpu_util` | [trace.h:928](../../kernel/kernel/sched/walt/trace.h#L928) | 三个 busiest 变体里每个候选 CPU 一次 |
| `walt_active_load_balance` | [trace.h:817](../../kernel/kernel/sched/walt/trace.h#L817) | `walt_lb_pull_tasks()` 与 `walt_lb_tick()` 发起 active balance 时 |
| `walt_find_busiest_queue` | [trace.h:842](../../kernel/kernel/sched/walt/trace.h#L842) | `walt_find_busiest_queue()` 每次 |
| `walt_newidle_balance` | [trace.h:888](../../kernel/kernel/sched/walt/trace.h#L888) | newidle 退出时（含 §2.3 的 `help_min_cap`） |
| `walt_nohz_balance_kick` | [trace.h:865](../../kernel/kernel/sched/walt/trace.h#L865) | `walt_nohz_balancer_kick()` 决定 kick 时 |

---

## 11. 遗留问题

1. **`help_min_cap` 是死变量** `[待确认]`：`should_help_min_cap()`
   [walt_lb.c:788](../../kernel/kernel/sched/walt/walt_lb.c#L788) 的结果只进 tracepoint，无分支消费。
2. **`force_overload` 的语义** `[待确认]`：`walt_lb_tick()` 调
   `walt_find_energy_efficient_cpu(p, prev_cpu, 0, 1)` 的第 4 个实参，
   本文按位置描述，未断言参数名。
3. **`1280/1024` 无 sysctl** `[待确认]`：§3.2 的 1.25 倍闸门硬编码。
4. **`walt_detach_task` 的 TODO**：源码自带 `//TODO can we just replace with detach_task in fair.c??`
   [walt_lb.c:22](../../kernel/kernel/sched/walt/walt_lb.c#L22)，已知技术债。
5. **`from_lower_cap` 自认是临时实现**：
   [walt_lb.c:561-567](../../kernel/kernel/sched/walt/walt_lb.c#L561-L567) 的注释
   "we really don't need this as a separate block. will refactor this after final testing is done."
6. **任务描述中提及但源码中不存在**的符号，已在上文标注：
   `sysctl_sched_newidle_balance`（§2.4）、`sysctl_sched_big_task_rotation_us` 与
   `rotate_heavy_to_random_cpu()`（§5.1）、`walt_halt_lb_*` API（§7.1）、`wrq->lb_*` 字段（§10.1）。

---

## 相关文档

- [01-baseline/load-balance.md](../01-baseline/05-load-balance.md) —— 原生 `load_balance()` / sched domain / active balance（本文前提）
- [02-integration-model.md](../00-overview/02-integration-model.md) —— 4 个 LB hook 的注册点与调用点总表
- [03-data-structures.md](../00-overview/03-data-structures.md) —— `walt_rq` / `walt_task_struct` / `walt_stats` 字段含义
- [04-data-flow.md](../00-overview/04-data-flow.md) —— 从 tick 到 LB / 调频 / core_ctl 的调用时序
- [window-model.md](01-window-model.md) —— `cumulative_runnable_avg_scaled` 与迁移簿记的来源
- [placement.md](04-placement.md) —— `walt_find_energy_efficient_cpu()`，被 §6.2 的 tick LB 复用
- [cpufreq.md](03-cpufreq.md) —— `WALT_CPUFREQ_CONTINUE` / `WALT_CPUFREQ_IC_MIGRATION` 的消费侧
- [power-side.md](07-power-side.md) —— `walt_halt` 与 hotplug 的区别、drain 机制
- [rt-mvp.md](09-rt-mvp.md) —— RT placement 与 MVP 抢占队列
- [groups-and-clusters.md](06-groups-and-clusters.md) —— RTG、colocation、`sysctl_sched_min_task_util_for_colocation`
- [04-open-questions.md](../03-comparison/04-open-questions.md) —— §11 条目的登记处
