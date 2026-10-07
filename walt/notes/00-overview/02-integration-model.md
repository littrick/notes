# WALT 集成模型：Android Vendor Hook

> **源码**：[walt.c](../../kernel/kernel/sched/walt/walt.c)、[walt_cfs.c](../../kernel/kernel/sched/walt/walt_cfs.c)、[walt_lb.c](../../kernel/kernel/sched/walt/walt_lb.c)、[walt_rt.c](../../kernel/kernel/sched/walt/walt_rt.c)、[walt_halt.c](../../kernel/kernel/sched/walt/walt_halt.c)、[fixup.c](../../kernel/kernel/sched/walt/fixup.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

---

## 1. 为什么是 hook，而不是 ifdef 补丁

传统 vendor 内核的做法是直接改 `fair.c` / `core.c`，用 `#ifdef CONFIG_SCHED_WALT`
把 WALT 逻辑插进原生调度器。Qualcomm 在这里选择了另一条路。

### 1.1 证据：原生文件里没有 WALT

```bash
grep -rn "walt_" kernel/kernel/sched/fair.c kernel/kernel/sched/core.c \
             kernel/kernel/sched/pelt.c  kernel/kernel/sched/sched.h
# → 无任何匹配
```

`fair.c` / `core.c` / `pelt.c` / `sched.h` 中：

- **没有 `walt_` 符号**
- **没有 `CONFIG_SCHED_WALT` 条件编译**

WALT 的全部代码位于 [kernel/kernel/sched/walt/](../../kernel/kernel/sched/walt/)，
通过注册 `trace_android_rvh_*` / `trace_android_vh_*` 回调接入。

### 1.2 设计动机

| | ifdef 补丁 | vendor hook |
|---|---|---|
| 原生文件 | 被修改 | **零修改** |
| GKI 兼容 | 破坏 ABI | 符合 GKI |
| 可否共存 | 需编译期二选一 | **运行期共存** |
| 调试对照 | 需重新编译 | 可运行时禁用 |

**最重要的推论**：因为原生 PELT + schedutil 路径**完整保留且同时运行**，
可以在**不重新编译**的情况下取得 baseline 数据。这是
[03-comparison/](../03-comparison/) 对照实验的基础。

---

## 2. 挂载机制：`android_vendor_data1`

WALT 的状态不存在自己的全局表里，而是挂在 GKI 预留的 vendor data 上。

### 2.1 预留区

| 结构体 | 声明 | 位置 | 大小 |
|---|---|---|---|
| `struct task_struct` | `ANDROID_VENDOR_DATA_ARRAY(1, 64)` | [sched.h:1497](../../kernel/include/linux/sched.h#L1497) | 512 B |
| `struct rq` | `ANDROID_VENDOR_DATA_ARRAY(1, 96)` | [sched.h:1133](../../kernel/kernel/sched/sched.h#L1133) | 768 B |
| `struct task_group` | `ANDROID_VENDOR_DATA_ARRAY(1, 4)` | [sched.h:443](../../kernel/kernel/sched/sched.h#L443) | 32 B（**受 `CONFIG_UCLAMP_TASK_GROUP` 保护**）|

### 2.2 取回：`wts_to_ts()`

[include/linux/sched/walt.h:157](../../kernel/include/linux/sched/walt.h#L157)：

```c
#define wts_to_ts(wts) ({ \
        void *__mptr = (void *)(wts); \
        ((struct task_struct *)(__mptr - \
            offsetof(struct task_struct, android_vendor_data1))); })
```

**关键点**：`walt_task_struct` 是**内联**在 task_struct 的预留数组里的，
不是指针指向的外部内存（详见 [data-structures.md §0](03-data-structures.md#0-挂载机制结构体内联在-gki-预留区)）。
所以从 `wts` 反推 `task_struct` 只需要减去字段偏移。

---

## 3. 统计口径

统计 WALT 的 hook 数量有三个容易混淆的口径。**本文档的一切计数以下表为准。**

| 口径 | 数量 | 说明 |
|---|---:|---|
| `register_trace_*` **调用总数** | **55** | 含重复注册；不含 `unregister_trace_*` |
| 唯一 tracepoint 名称 | **53** | 55 − 2 次重复 |
| **唯一 Android hook 名称** | **50** | 53 − 3 个非 Android tracepoint |
| `register_walt_hooks()` 内注册 | **27 Android + 1 非 Android** | [walt.c:4981](../../kernel/kernel/sched/walt/walt.c#L4981) |

### 3.1 容易数错的地方

1. **`unregister_trace_` 包含 `register_trace_` 子串**。用
   `grep -c register_trace_` 会把注销调用也算进去。正确做法是加词边界：
   `grep -rhoE "\bregister_trace_[a-z0-9_]+"`。

2. **有 4 个非 Android tracepoint**混在其中：

   | tracepoint | 注册处 | 归属 |
   |---|---|---|
   | `cpu_frequency_limits` | [walt.c:5008](../../kernel/kernel/sched/walt/walt.c#L5008) | 标准内核 tracepoint |
   | `sched_switch` | walt_rt.c:84、walt_tp.c:98 | 标准内核 tracepoint（注册 2 次）|
   | `sched_overutilized_tp` | walt_tp.c:120 | 标准内核 tracepoint |

   统计「vendor hook 数量」时应排除它们。

3. **`android_vh_scheduler_tick` 被注册两次**：
   [walt.c:4997](../../kernel/kernel/sched/walt/walt.c#L4997) 和 walt_rt.c:85。
   这是唯一重复的 hook，两个回调**都会被调用**（见 §5.2）。

4. **调试模块的 hook 默认不编入**。`preemptirq_long.c`（4 个）、
   `walt_debug.c`（1 个）属于 `CONFIG_SCHED_WALT_DEBUG`，
   `walt_tp.c` 的注册是运行期动态的（由 `sysctl_sched_dynamic_tp_enable` 控制）。
   只关心生产路径时应排除这 5 个。

> **只统计生产路径的 Android hook**：50 − 5 = **45**。
> 这很可能就是 TASK.md 中「45 个 hook」的来源。

### 3.2 两种 hook 类型

从 `include/trace/hooks/*.h` 的定义语义区分：

| 宏 | 前缀 | 语义 |
|---|---|---|
| `DECLARE_RESTRICTED_HOOK` | `android_rvh_` | **受限**：单回调，性能开销小 |
| `DECLARE_HOOK` | `android_vh_` | **多回调**：可多个模块注册 |

WALT 绝大多数用 `android_rvh_`（受限），说明它在设计上假定
**这些 hook 点只服务 WALT**——抢占式的性能敏感路径。

---

## 4. 注册侧

### 4.1 入口链

`register_walt_hooks()` 并**不**是直接在 initcall 里调用的，中间隔了一层 workqueue：

```
module_init(walt_module_init)            walt.c:5169
  └─ walt_module_init()                  walt.c:5153
       ├─ register_trace_android_vh_update_topology_flags_workfn()  walt.c:5160
       └─ schedule_work(&walt_init_work)   （延后到 workqueue）
            └─ walt_init()               walt.c:5074 / 5143
                 ├─ register_walt_hooks()   walt.c:5094
                 ├─ walt_fixup_init()       walt.c:5095
                 ├─ walt_lb_init()          walt.c:5096
                 ├─ walt_rt_init()          walt.c:5097
                 ├─ walt_cfs_init()         walt.c:5098
                 └─ walt_halt_init()        walt.c:5099
```

> **为什么走 workqueue** [推测]：WALT 初始化需要读取 CPU 拓扑、容量等信息，
> 这些在早期 initcall 阶段可能还没就绪。延后到 workqueue 可以拿到完整的
> `topology_flags`。这也解释了 `android_vh_update_topology_flags_workfn`
> 回调（walt.c:5144）本身也会重新 schedule 这个 work——拓扑变化时需要重跑。

### 4.2 各模块注册函数

| 函数 | 定义 | 注册的 hook |
|---|---|---|
| `register_walt_hooks` | [walt.c:4981](../../kernel/kernel/sched/walt/walt.c#L4981) | 27 Android + `cpu_frequency_limits` |
| `walt_cfs_init` | [walt_cfs.c:1546](../../kernel/kernel/sched/walt/walt_cfs.c#L1546) | 6 |
| `walt_lb_init` | [walt_lb.c:1117](../../kernel/kernel/sched/walt/walt_lb.c#L1117) | 4 |
| `walt_rt_init` | [walt_rt.c:366](../../kernel/kernel/sched/walt/walt_rt.c#L366) | 2（另 2 个在 sysctl handler 里懒注册）|
| `walt_halt_init` | [walt_halt.c:584](../../kernel/kernel/sched/walt/walt_halt.c#L584) | 4（**在 `#ifdef CONFIG_HOTPLUG_CPU` 内**）|
| `walt_fixup_init` | [fixup.c:89](../../kernel/kernel/sched/walt/fixup.c#L89) | 1 |
| `walt_debug_init` | [walt_debug.c:19](../../kernel/kernel/sched/walt/walt_debug.c#L19) | 1（debug）|
| `preemptirq_long_init` | [preemptirq_long.c:163](../../kernel/kernel/sched/walt/preemptirq_long.c#L163) | 4（debug）|

### 4.3 懒注册

`walt_rt.c` 的两个 tracepoint（`sched_switch`、`android_vh_scheduler_tick`）
注册在 **sysctl handler** `sched_long_running_rt_task_ms_handler`
（[walt_rt.c:68](../../kernel/kernel/sched/walt/walt_rt.c#L68)）里——即
**只有用户写了 `sysctl_sched_long_running_rt_task_ms` 才会注册**。

这带来一个排查陷阱：**没看到 RT 看门狗的 hook 不代表代码有问题**，
先确认该 sysctl 是否被写过。`walt_tp.c` 的动态 tracepoint 同理。

---

## 5. 完整 hook 表

> 「调用点」为原生内核中 `trace_android_*()` 的位置。全部经脚本核验（2026-09-17）。

### 5.1 核心模块 walt.c（27 Android）

| Hook | 回调定义 | 回调作用 | 原生调用点 |
|---|---|---|---|
| `android_rvh_wake_up_new_task` | walt.c:4498 | 新任务 WALT 负载初始化 + 入组 | [core.c:4644](../../kernel/kernel/sched/core.c#L4644) |
| `android_rvh_update_cpu_capacity` | walt.c:4197 | 重算容量（扣除 RT 压力）| [fair.c:8755](../../kernel/kernel/sched/fair.c#L8755) |
| `android_rvh_sched_cpu_starting` | walt.c:4518 | CPU 上线清请求 | [core.c:9444](../../kernel/kernel/sched/core.c#L9444) |
| `android_rvh_sched_cpu_dying` | walt.c:4525 | CPU 下线清请求 | [core.c:9518](../../kernel/kernel/sched/core.c#L9518) |
| `android_rvh_set_task_cpu` | walt.c:4532 | **迁移减法记账** + 亲和/32位检查 | [core.c:3175](../../kernel/kernel/sched/core.c#L3175) |
| `android_rvh_new_task_stats` | walt.c:4551 | `mark_task_starting` | [core.c:4664](../../kernel/kernel/sched/core.c#L4664) |
| `android_rvh_account_irq_start` | walt.c:4558 | IRQ 开始：更新 idle 任务的周期计数 | [cputime.c:86](../../kernel/kernel/sched/cputime.c#L86) |
| `android_rvh_account_irq_end` | walt.c:4582 | IRQ 结束：以 `IRQ_UPDATE` 更新 ravg | [cputime.c:88](../../kernel/kernel/sched/cputime.c#L88) |
| `android_rvh_flush_task` | walt.c:4604 | 任务退出清理 | [core.c:5086](../../kernel/kernel/sched/core.c#L5086) |
| `android_rvh_update_misfit_status` | walt.c:4722 | 重算 misfit 负载 | [fair.c:4251](../../kernel/kernel/sched/fair.c#L4251) |
| `android_rvh_after_enqueue_task` | walt.c:4611 | **入队记账**（CFS + cumulative avg）| [core.c:2044](../../kernel/kernel/sched/core.c#L2044) |
| `android_rvh_after_dequeue_task` | walt.c:4672 | **出队记账** | [core.c:2066](../../kernel/kernel/sched/core.c#L2066) |
| `android_rvh_try_to_wake_up` | walt.c:4760 | 唤醒时 ravg 更新 + preferred cluster | [core.c:4260](../../kernel/kernel/sched/core.c#L4260) |
| `android_rvh_tick_entry` | walt.c:4790 | 每 tick ravg 更新 + early detection | [core.c:5454](../../kernel/kernel/sched/core.c#L5454) |
| `android_vh_scheduler_tick` | walt.c:4806 | 首 tick 窗口初始化 + `walt_lb_tick` | [core.c:5475](../../kernel/kernel/sched/core.c#L5475) |
| `android_rvh_schedule` | walt.c:4833 | **上下文切换时更新 prev/next 的 ravg** | [core.c:6508](../../kernel/kernel/sched/core.c#L6508) |
| `android_rvh_cpu_cgroup_attach` | walt.c:3287 | cgroup attach 设置共置组 ID | [core.c:10314](../../kernel/kernel/sched/core.c#L10314) |
| `android_rvh_cpu_cgroup_online` | walt.c:3279 | cgroup online 更新 tg 指针 | [core.c:10232](../../kernel/kernel/sched/core.c#L10232) |
| `android_rvh_update_cpus_allowed` | walt.c:4854 | cpuset 变化时恢复缓存亲和集 | [cpuset.c:1142](../../kernel/kernel/cgroup/cpuset.c#L1142) |
| `android_rvh_sched_setaffinity` | walt.c:4876 | 缓存用户设置的亲和集 | [core.c:8291](../../kernel/kernel/sched/core.c#L8291) |
| `android_rvh_sched_getaffinity` | walt.c:4866 | 从 getaffinity 中屏蔽 halted CPU | [core.c:8352](../../kernel/kernel/sched/core.c#L8352) |
| `android_rvh_sched_fork_init` | walt.c:4897 | `__sched_fork_init` | [core.c:4380](../../kernel/kernel/sched/core.c#L4380) |
| `android_rvh_ttwu_cond` | walt.c:4905 | 用 many-wakeup 阈值门控 ttwu | [core.c:3906](../../kernel/kernel/sched/core.c#L3906) |
| `android_rvh_sched_exec` | walt.c:4913 | 强制 `cond=true`（exec 后必重平衡）| [core.c:5296](../../kernel/kernel/sched/core.c#L5296) |
| `android_rvh_build_perf_domains` | walt.c:4920 | 强制构建 perf domain | [topology.c:370](../../kernel/kernel/sched/topology.c#L370) |
| `android_rvh_do_sched_yield` | walt.c:4965 | yield 时撤销 MVP / 清 RT 到达时间 | [core.c:8408](../../kernel/kernel/sched/core.c#L8408) |
| `android_rvh_update_thermal_stats` | walt.c:4925 | 热变化时更新容量 | [arch_topology.c:172](../../kernel/drivers/base/arch_topology.c#L172) |
| `android_vh_update_topology_flags_workfn` | walt.c:5144 | 拓扑变化时重新 schedule 初始化 work | [arch_topology.c:229](../../kernel/drivers/base/arch_topology.c#L229) |
| `cpu_frequency_limits` *(非 Android)* | walt.c:4506 | 记录簇最高频 + 更新容量 | [cpufreq.c:2590](../../kernel/drivers/cpufreq/cpufreq.c#L2590) |

### 5.2 CFS 模块 walt_cfs.c（6）

| Hook | 回调定义 | 回调作用 | 原生调用点 |
|---|---|---|---|
| `android_rvh_select_task_rq_fair` | walt_cfs.c:1148 | **任务放置主入口** | [fair.c:7229](../../kernel/kernel/sched/fair.c#L7229) |
| `android_vh_binder_wakeup_ilocked` | walt_cfs.c:1165 | 设置/清除 `WALT_LOW_LATENCY_BINDER` | [binder.c:572](../../kernel/drivers/android/binder.c#L572) |
| `android_vh_binder_set_priority` | walt_cfs.c:1191 | binder 服务端 boost 到 STRICT_MAX | [binder.c:851](../../kernel/drivers/android/binder.c#L851) |
| `android_vh_binder_restore_priority` | walt_cfs.c:1207 | 恢复 binder 任务 boost | [binder.c:3854](../../kernel/drivers/android/binder.c#L3854) 等 3 处 |
| `android_rvh_check_preempt_wakeup` | walt_cfs.c:1428 | **MVP 感知的抢占决策** | [fair.c:7513](../../kernel/kernel/sched/fair.c#L7513) |
| `android_rvh_replace_next_task_fair` | walt_cfs.c:1492 | **pick-next 时替换为 MVP 任务** | [fair.c:7646](../../kernel/kernel/sched/fair.c#L7646) |

### 5.3 RT 模块 walt_rt.c（3 Android）

| Hook | 回调定义 | 回调作用 | 原生调用点 |
|---|---|---|---|
| `android_vh_scheduler_tick` *(第 2 次注册)* | walt_rt.c:27 | 长跑 FIFO RT 检测 | [core.c:5475](../../kernel/kernel/sched/core.c#L5475) |
| `android_rvh_select_task_rq_rt` | walt_rt.c:234 | RT 任务放置 | [rt.c:1518](../../kernel/kernel/sched/rt.c#L1518) |
| `android_rvh_find_lowest_rq` | walt_rt.c:339 | 选 packing/能量感知 CPU，排除 halted | [rt.c:1844](../../kernel/kernel/sched/rt.c#L1844) |
| `sched_switch` *(非 Android)* | walt_rt.c:16 | 记录 RT 任务到达时间戳 | [core.c:6535](../../kernel/kernel/sched/core.c#L6535) |

### 5.4 负载均衡模块 walt_lb.c（4）

| Hook | 回调定义 | 回调作用 | 原生调用点 |
|---|---|---|---|
| `android_rvh_sched_nohz_balancer_kick` | walt_lb.c:1074 | 决定 nohz kick 标志 | [fair.c:10761](../../kernel/kernel/sched/fair.c#L10761) |
| `android_rvh_can_migrate_task` | walt_lb.c:1091 | **WALT 簇迁移门控** | [fair.c:8125](../../kernel/kernel/sched/fair.c#L8125) |
| `android_rvh_find_busiest_queue` | walt_lb.c:1023 | WALT busiest 队列选择 | [fair.c:9925](../../kernel/kernel/sched/fair.c#L9925) |
| `android_rvh_sched_newidle_balance` | walt_lb.c:1110 | **`walt_newidle_balance`** | [fair.c:11163](../../kernel/kernel/sched/fair.c#L11163) |

### 5.5 Halt 模块 walt_halt.c（4，受 `CONFIG_HOTPLUG_CPU` 保护）

| Hook | 回调定义 | 回调作用 | 原生调用点 |
|---|---|---|---|
| `android_rvh_get_nohz_timer_target` | walt_halt.c:455 | nohz timer 目标避开 halted | [core.c:1045](../../kernel/kernel/sched/core.c#L1045) |
| `android_rvh_set_cpus_allowed_by_task` | walt_halt.c:516 | 目标 CPU 避开 halted | [core.c:2942](../../kernel/kernel/sched/core.c#L2942) |
| `android_rvh_rto_next_cpu` | walt_halt.c:544 | RT 溢出目标避开 halted | [rt.c:2192](../../kernel/kernel/sched/rt.c#L2192) |
| `android_rvh_is_cpu_allowed` | walt_halt.c:563 | 禁止在 halted CPU 上运行 | [core.c:2270](../../kernel/kernel/sched/core.c#L2270) |

### 5.6 其余（fixup / debug）

| Hook | 回调定义 | 作用 | 原生调用点 |
|---|---|---|---|
| `android_rvh_show_max_freq` | fixup.c:76 | 对 sched-lib 应用加倍最高频 | [cpufreq.c:705](../../kernel/drivers/cpufreq/cpufreq.c#L705) |
| `android_rvh_schedule_bug` | walt_debug.c:14 | schedule 时 `BUG()`（debug）| [core.c:5731](../../kernel/kernel/sched/core.c#L5731) |
| `android_rvh_irqs_disable` | preemptirq_long.c:42 | 记录 irqs-off 时间戳（debug）| [trace_preemptirq.c:76](../../kernel/kernel/trace/trace_preemptirq.c#L76) |
| `android_rvh_irqs_enable` | preemptirq_long.c:54 | 测量 irqs-off 时长（debug）| [trace_preemptirq.c:35](../../kernel/kernel/trace/trace_preemptirq.c#L35) |
| `android_rvh_preempt_disable` | preemptirq_long.c:84 | 记录 preempt-off 时间戳（debug）| [trace_preemptirq.c:153](../../kernel/kernel/trace/trace_preemptirq.c#L153) |
| `android_rvh_preempt_enable` | preemptirq_long.c:93 | 测量 preempt-off 时长（debug）| [trace_preemptirq.c:144](../../kernel/kernel/trace/trace_preemptirq.c#L144) |

---

## 6. 按调用点分布看集成深度

按**原生文件**统计 WALT 所注册 hook 的 `trace_android_*()` 调用点数量
（脚本实测，2026-09-17；已排除其它子系统注册的 Android hook）：

| 原生文件 | 生产路径 | 调试路径 | 说明 |
|---|---:|---:|---|
| `kernel/sched/core.c` | **23** | 2 | 主战场：enqueue/dequeue/tick/schedule/affinity |
| `kernel/sched/fair.c` | **10** | — | placement + LB |
| `drivers/android/binder.c` | 6 | — | binder 低延迟与 boost |
| `kernel/sched/rt.c` | 3 | — | RT 放置与溢出 |
| `kernel/sched/cputime.c` | 2 | — | IRQ 记账 |
| `drivers/base/arch_topology.c` | 2 | — | 拓扑/热 |
| `kernel/sched/topology.c` | 1 | — | perf domain |
| `kernel/cgroup/cpuset.c` | 1 | — | cpuset |
| `drivers/cpufreq/cpufreq.c` | 1 | — | `show_max_freq` |
| `kernel/trace/trace_preemptirq.c` | — | 8 | preempt/irq 长关闭检测 |
| **合计** | **49** | **10** | 总计 **59** |

> 生产路径 49 个调用点对应 45 个 hook 名称——**调用点数 ≠ hook 数**：
> 部分 hook（如 `binder_restore_priority`）在原生代码里有多处调用。

**读法**：WALT 的集成**重心在 `core.c`**（23 个，占生产路径 47%），
而非直觉上的 `fair.c`。这符合它的设计——WALT 关心的是「任务的运行时间」，
而运行时间的记账点（enqueue/dequeue/context switch/tick）都在 `core.c`。

> 统计脚本注意：`grep -c "trace_android_"` 会把**其它子系统**注册的
> Android hook 一并算入（`core.c` 中此类调用点共 52 个，而 WALT 只占 23 个）。
> 必须先取出 WALT 注册的 hook 名集合，再逐个反查调用点。

---

## 7. 按功能分类

把 **45 个生产路径 hook** 按它们服务的数据流环节归类（每 hook 唯一归属）：

| 环节 | hook 数 | 成员 |
|---|---:|---|
| **负载/需求记账** | 9 | `set_task_cpu`(迁移记账)、`account_irq_start`、`account_irq_end`、`after_enqueue_task`、`after_dequeue_task`、`try_to_wake_up`、`tick_entry`、`vh_scheduler_tick`、`schedule` |
| **CPU 上下线 / halt** | 5 | `sched_cpu_starting`、`sched_cpu_dying`、`is_cpu_allowed`、`get_nohz_timer_target`、`rto_next_cpu` |
| **放置决策** | 4 | `select_task_rq_fair`、`select_task_rq_rt`、`find_lowest_rq`、`replace_next_task_fair` |
| **负载均衡** | 4 | `sched_newidle_balance`、`find_busiest_queue`、`can_migrate_task`、`nohz_balancer_kick` |
| **亲和性** | 4 | `sched_setaffinity`、`sched_getaffinity`、`update_cpus_allowed`、`set_cpus_allowed_by_task` |
| **容量 / 拓扑 / 热** | 4 | `update_cpu_capacity`、`update_thermal_stats`、`build_perf_domains`、`update_topology_flags_workfn` |
| **任务生命周期** | 4 | `wake_up_new_task`、`new_task_stats`、`flush_task`、`sched_fork_init` |
| **binder 低延迟** | 3 | `binder_wakeup_ilocked`、`binder_set_priority`、`binder_restore_priority` |
| **抢占 / MVP** | 2 | `check_preempt_wakeup`、`do_sched_yield` |
| **cgroup** | 2 | `cpu_cgroup_attach`、`cpu_cgroup_online` |
| **唤醒门控** | 2 | `ttwu_cond`、`sched_exec` |
| **其它** | 2 | `update_misfit_status`、`show_max_freq` |
| **合计** | **45** | |

调试路径另有 5 个：`schedule_bug`、`irqs_disable`、`irqs_enable`、
`preempt_disable`、`preempt_enable`。

**最重要的读法**：**记账类（9）与 CPU 生命周期类（5）合计占 14/45 ≈ 31%**，
而决策类（放置 4 + 均衡 4 + 抢占 2 = 10）只占 22%。
WALT 的代码量重心在**数据的正确采集**，而非决策算法本身。

---

## 8. 验证方法

本文档的所有 `file:line` 均经脚本核验。复现方式：

```bash
cd kernel

# 1. 统计注册（注意词边界，排除 unregister_）
grep -rhoE "\bregister_trace_[a-z0-9_]+" kernel/kernel/sched/walt/*.c |
    sort | uniq -c | sort -rn

# 2. 找某 hook 的原生调用点
grep -rn "trace_android_rvh_select_task_rq_fair(" kernel/

# 3. 核对某个行号
sed -n '7229p' kernel/sched/fair.c
```

---

## 9. 相关文档

- 挂载结构体的字段 → [data-structures.md](03-data-structures.md)
- 数据如何流动 → [data-flow.md](04-data-flow.md)
- 各 hook 触发的算法 → [02-walt/](../02-walt/)
