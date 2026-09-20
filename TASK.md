# WALT学习笔记

## 基本路径

- kernel源码根: `kernel/`
- scheduler目录: [kernel/kernel/sched/](kernel/kernel/sched/) （软链 `./sched`）
- WALT目录: [kernel/kernel/sched/walt/](kernel/kernel/sched/walt/)
- 笔记目录: [notes/](notes/)

内核版本: **5.15.211** (Qualcomm, sm8550/lineage-21)

---

## 源码架构速览

> 本节是任务列表的事实依据。所有结论均来自源码，写文档时以此为准。

### 关键结论一：WALT 通过 Android vendor hook 集成，而非 ifdef 补丁

`fair.c` / `core.c` / `pelt.c` / `sched.h` 中 **没有任何 `walt_` 符号，也没有 `CONFIG_SCHED_WALT` 条件编译**。
WALT 全部代码位于 [kernel/kernel/sched/walt/](kernel/kernel/sched/walt/)，通过注册 45 个
`trace_android_rvh_*` / `trace_android_vh_*` 回调接入原生调度器。

- 注册入口: [walt.c:4981](kernel/kernel/sched/walt/walt.c#L4981) `register_walt_hooks()`，以及各模块的
  `walt_cfs_init` / `walt_lb_init` / `walt_rt_init` / `walt_halt_init`
- 挂载方式: WALT 状态挂在 GKI 预留的 `android_vendor_data1` 指针上
  （`struct task_struct` → `walt_task_struct`，`struct rq` → `walt_rq`）
- **推论**: 理解 WALT 的第一步是理解 hook 点，而不是读 fair.c。原生路径保持未修改，两者可共存。

### 关键结论二：模块职责与直觉不同

| 文件 | 行数 | 实际职责 |
|---|---|---|
| [walt.c](kernel/kernel/sched/walt/walt.c) | 5174 | **核心**：窗口/负载/需求计算引擎、簇拓扑、分组(colocation)、迁移簿记、容量更新 |
| [walt_cfs.c](kernel/kernel/sched/walt/walt_cfs.c) | 1557 | **CFS 任务 placement**（`walt_find_best_target`、能量模型、MVP 机制） |
| [walt_lb.c](kernel/kernel/sched/walt/walt_lb.c) | 1134 | **负载均衡**（newidle balance、active migration、big task rotation） |
| [cpufreq_walt.c](kernel/kernel/sched/walt/cpufreq_walt.c) | 1164 | `walt` governor（调频），替代 schedutil |
| [core_ctl.c](kernel/kernel/sched/walt/core_ctl.c) | 1551 | 基于 WALT 负载的 CPU 热插拔/上下线守护线程 |
| [walt_halt.c](kernel/kernel/sched/walt/walt_halt.c) | 613 | CPU "halt"（临时停用CPU并迁走任务，非热插拔、非EAS） |
| [trace.h](kernel/kernel/sched/walt/trace.h) | 1544 | 50+ 个 `schedwalt` tracepoint |
| [sysctl.c](kernel/kernel/sched/walt/sysctl.c) | 1147 | 全部 tunable 定义与 handler |
| [walt_rt.c](kernel/kernel/sched/walt/walt_rt.c) | 380 | RT 任务 placement + 长跑 RT 看门狗 |
| [sched_avg.c](kernel/kernel/sched/walt/sched_avg.c) | 338 | **注意**：不是负载追踪，是 nr_running / busy-hyst / cluster-util 统计 |
| [boost.c](kernel/kernel/sched/walt/boost.c) | 300 | 全局调度 boost 状态机 + colocation 分组标志 |
| [input-boost.c](kernel/kernel/sched/walt/input-boost.c) | 300 | 输入事件驱动的 min-freq (PM QoS) + boost |

### 关键结论三：三处易错点

1. **负载计算不在 `sched_avg.c`**，在 `walt.c`（`walt_update_task_ravg`，[walt.c:2288](kernel/kernel/sched/walt/walt.c#L2288)）。
2. **placement 不在 `walt.c`**，在 `walt_cfs.c`（[walt_cfs.c:1149](kernel/kernel/sched/walt/walt_cfs.c#L1149) `walt_select_task_rq_fair`）。
3. **调频算法跨两个文件**：负载侧在 `walt.c`（`freq_policy_load` :590 / `cpu_util_freq_walt` :680），
   映射侧在 `cpufreq_walt.c`（`walt_map_util_freq` :208 / `waltgov_walt_adjust` :305）。

### 关键结论四：PELT 未被移除

`pelt.c` 与 `fair.c` 的 PELT 路径完整保留并持续计算。WALT 仅在 `cpu_util_cum()`
([walt.h:445](kernel/kernel/sched/walt/walt.h#L445)) 中把 PELT `util_avg` 作为**次要信号**读取。
原生 `schedutil` governor 也仍在树中。这是 base / WALT 对照的关键。

---

## 基本任务

### 阶段 0：基础设施（先做，其余任务的索引基础）

- [x] 文档骨架与索引：确立 `notes/` 目录结构、术语表、交叉引用约定
- [x] **WALT 集成模型**：逐一梳理 45 个 vendor hook 的注册点与内核侧调用点
  - 核对结果：全树 **50** 个唯一 Android hook 名（51 次注册，1 个重复）；
    「45」对应**只计生产路径**的口径（50 − 5）。见 [integration-model.md](notes/00-overview/02-integration-model.md)
  - [x] 注册侧：`register_walt_hooks` [walt.c:4981](kernel/kernel/sched/walt/walt.c#L4981) + 各模块 init
  - [x] 调用侧：fair.c / core.c / cputime.c / topology.c / cpuset.c / arch_topology.c 中的 hook 调用行
  - [x] `android_vendor_data1` 挂载与 `wts_to_ts()` 取回机制
- [x] **核心数据结构**（字段级）
  - [x] `struct walt_task_struct` [include/linux/sched/walt.h:58](kernel/include/linux/sched/walt.h#L58)
  - [x] `struct walt_rq` [walt.h:98](kernel/kernel/sched/walt/walt.h#L98)
  - [x] `struct walt_sched_cluster` [walt.h:138](kernel/kernel/sched/walt/walt.h#L138)
  - [x] `struct walt_sched_stats` / `group_cpu_time` / `load_subtractions`
- [x] **数据流动总图**：从调度事件 → vendor hook → `walt_update_task_ravg` → 三个消费方
  （placement / cpufreq / core_ctl）的全链路时序图

### 阶段 1：原生 Linux Scheduler（baseline）

- [x] 调度核心框架（为下列算法提供上下文）
  - [x] 调度类与 `sched_class` 接口：enqueue/dequeue/pick_next/task_tick
  - [x] 主流程：`schedule()` [core.c:6614](kernel/kernel/sched/core.c#L6614) / `__schedule` [core.c:6415](kernel/kernel/sched/core.c#L6415)、`try_to_wake_up` [core.c:4110](kernel/kernel/sched/core.c#L4110)
  - [x] `struct rq` / `struct cfs_rq` 关键字段
- [x] **负载计算算法**：PELT
  - [x] 几何衰减模型与 `decay_load` [pelt.c:35](kernel/kernel/sched/pelt.c#L35)
  - [x] `___update_load_sum` / `___update_load_avg` [pelt.c:183](kernel/kernel/sched/pelt.c#L183)
  - [x] 各调度类负载：`update_rt_rq_load_avg` / `update_dl_rq_load_avg` / `update_irq_load_avg`
  - [x] util_est 预测 [fair.c:3966](kernel/kernel/sched/fair.c#L3966)
- [x] **调频的频率计算算法**：schedutil
  - [x] `sugov_get_util` → `effective_cpu_util` [cpufreq_schedutil.c:201](kernel/kernel/sched/cpufreq_schedutil.c#L201)
  - [x] `map_util_freq`（1.25× 拐点映射） [cpufreq_schedutil.c:177](kernel/kernel/sched/cpufreq_schedutil.c#L177)
  - [x] iowait boost、rate limit、fast switch vs kthread
- [x] **任务 placement 算法**
  - [x] `select_task_rq_fair` [fair.c:7214](kernel/kernel/sched/fair.c#L7214)
  - [x] EAS：`find_energy_efficient_cpu` [fair.c:7019](kernel/kernel/sched/fair.c#L7019)
  - [x] `compute_energy` [fair.c:6910](kernel/kernel/sched/fair.c#L6910) 与性能域 perf_domain
  - [x] wake_affine / sync wakeup
- [x] **负载均衡算法**
  - [x] `load_balance` [fair.c:10153](kernel/kernel/sched/fair.c#L10153) / `find_busiest_group` / `find_busiest_queue` [fair.c:9917](kernel/kernel/sched/fair.c#L9917)
  - [x] `newidle_balance` 与 nohz idle balance
  - [x] active load balance [fair.c:10464](kernel/kernel/sched/fair.c#L10464)、sched domain 层级 [topology.c](kernel/kernel/sched/topology.c)

### 阶段 2：WALT

- [x] **负载计算算法**（窗口模型）
  - [x] 窗口定义与滚动：`update_window_start` [walt.c:406](kernel/kernel/sched/walt/walt.c#L406)、`rollover_cpu_window` [walt.c:1604](kernel/kernel/sched/walt/walt.c#L1604)、`rollover_task_window` [walt.c:1494](kernel/kernel/sched/walt/walt.c#L1494)
  - [x] 总入口 `walt_update_task_ravg` [walt.c:2288](kernel/kernel/sched/walt/walt.c#L2288) 与 6 种事件 `enum task_event` [walt.h:40](kernel/kernel/sched/walt/walt.h#L40)
  - [x] 任务需求：`update_task_demand` [walt.c:2127](kernel/kernel/sched/walt/walt.c#L2127) 的三段式窗口切分
  - [x] CPU 忙时：`update_cpu_busy_time` [walt.c:1678](kernel/kernel/sched/walt/walt.c#L1678)、`curr/prev_runnable_sum`、`nt_*` 新任务和
  - [x] 频率归一化：`scale_exec_time` [walt.c:1566](kernel/kernel/sched/walt/walt.c#L1566)、`task_exec_scale` 与 cycle counter
  - [x] 历史与 demand 策略：`update_history` [walt.c:1984](kernel/kernel/sched/walt/walt.c#L1984)、`sum_history` 环形缓冲、`WINDOW_STATS_*` 四种策略
  - [x] 迁移簿记：`migrate_busy_time_subtraction/addition` [walt.c:1021](kernel/kernel/sched/walt/walt.c#L1021)、`load_subtractions` 机制
- [x] **需求预测算法**（与原列表不同，需单列）
  - [x] 16 桶直方图：`busy_to_bucket` [walt.c:1220](kernel/kernel/sched/walt/walt.c#L1220)、`bucket_increase` [walt.c:1196](kernel/kernel/sched/walt/walt.c#L1196)
  - [x] `get_pred_busy` [walt.c:1256](kernel/kernel/sched/walt/walt.c#L1256)、`update_task_pred_demand` [walt.c:1321](kernel/kernel/sched/walt/walt.c#L1321)
  - [x] `pred_demands_sum_scaled` 的 rq 聚合与消费方
- [x] **调频的频率计算算法**（跨两文件）
  - [x] 负载侧：`freq_policy_load` [walt.c:590](kernel/kernel/sched/walt/walt.c#L590)（aggr_grp_load / ksoftirqd / top_task / user hint）
  - [x] `cpu_util_freq_walt` [walt.c:680](kernel/kernel/sched/walt/walt.c#L680) 与 asym-cap sibling 混合
  - [x] governor 触发路径：`waltgov_add_callback` / `waltgov_run_callback`（由窗口滚动驱动，非 `update_util`）
  - [x] 映射：`walt_map_util_freq` [cpufreq_walt.c:208](kernel/kernel/sched/walt/cpufreq_walt.c#L208)（target_load_thresh / shift）
  - [x] boost 阶梯：`waltgov_walt_adjust` [cpufreq_walt.c:305](kernel/kernel/sched/walt/cpufreq_walt.c#L305) 的 HISPEED / RTG / NWD / PL / ED / BTR 六种 reason
  - [x] `waltgov_calc_avg_cap` 平均容量与 up/down 分离限速
  - [x] 与原生 schedutil 的结构差异对照表（iowait boost 被移除等）
- [x] **任务 placement 算法**
  - [x] 入口 `walt_select_task_rq_fair` [walt_cfs.c:1149](kernel/kernel/sched/walt/walt_cfs.c#L1149)
  - [x] `walt_find_energy_efficient_cpu` [walt_cfs.c:933](kernel/kernel/sched/walt/walt_cfs.c#L933) 的 fastpath 顺序（pipeline → sync → packing → prev_cpu → energy）
  - [x] `walt_find_best_target` [walt_cfs.c:370](kernel/kernel/sched/walt/walt_cfs.c#L370) 候选选择与 `walt_should_reject_fbt_cpu` [walt_cfs.c:340](kernel/kernel/sched/walt/walt_cfs.c#L340) 拒绝条件
  - [x] `walt_get_indicies` [walt_cfs.c:221](kernel/kernel/sched/walt/walt_cfs.c#L221) 的 boost 分层与 cluster 顺序
  - [x] WALT 能量模型：`create_util_to_cost` [walt_cfs.c:42](kernel/kernel/sched/walt/walt_cfs.c#L42)、`walt_em_cpu_energy` [walt_cfs.c:740](kernel/kernel/sched/walt/walt_cfs.c#L740)
  - [x] 容量模型：`cpu_array` 构建 [walt.c:2639](kernel/kernel/sched/walt/walt.c#L2639)、`update_cpu_capacity_helper` [walt.c:4161](kernel/kernel/sched/walt/walt.c#L4161)、capacity margin
  - [x] sync / many-wakeup 判定：`walt_is_many_wakeup` [walt_cfs.c:153](kernel/kernel/sched/walt/walt_cfs.c#L153)、`walt_ttwu_cond`
- [x] **负载均衡算法**
  - [x] `walt_newidle_balance` [walt_lb.c:805](kernel/kernel/sched/walt/walt_lb.c#L805)（完全替换上游 newidle LB）
  - [x] busiest CPU 选择 [walt_lb.c:463-640](kernel/kernel/sched/walt/walt_lb.c#L463)：同簇 / 向上簇 / 向下簇三种方向
  - [x] `walt_lb_pull_tasks` [walt_lb.c:289](kernel/kernel/sched/walt/walt_lb.c#L289) 三趟拉取与 active balance
  - [x] 迁移门控 `_walt_can_migrate_task` [walt_lb.c:235](kernel/kernel/sched/walt/walt_lb.c#L235)
  - [x] big task rotation：`walt_lb_tick` [walt_lb.c:643](kernel/kernel/sched/walt/walt_lb.c#L643)、`walt_lb_check_for_rotation` [walt_lb.c:134](kernel/kernel/sched/walt/walt_lb.c#L134)、`migrate_swap`
  - [x] 周期 LB hook：`walt_find_busiest_queue` / `walt_nohz_balancer_kick` [walt_lb.c:1074](kernel/kernel/sched/walt/walt_lb.c#L1074)
- [x] **分组与优先簇**（WALT 特有，原列表缺失）
  - [x] Related Thread Group [walt.c:3016](kernel/kernel/sched/walt/walt.c#L3016) 与 `grp->load` 聚合
  - [x] colocation：`uclamp_task_colocated` [walt.c:3101](kernel/kernel/sched/walt/walt.c#L3101)、`sysctl_sched_min_task_util_for_colocation`
  - [x] preferred cluster 与 up/down migrate 门限 [walt.c:2917](kernel/kernel/sched/walt/walt.c#L2917)
  - [x] pipeline / heavy 任务重排 [walt.c:3591-3991](kernel/kernel/sched/walt/walt.c#L3591)
- [x] **CPU 电源侧**（原列表缺失）
  - [x] `core_ctl`：`core_ctl_check` [core_ctl.c:1129](kernel/kernel/sched/walt/core_ctl.c#L1129)、`eval_need` [core_ctl.c:889](kernel/kernel/sched/walt/core_ctl.c#L889) 上下线判定
  - [x] `walt_halt`：halt vs hotplug 的区别、`migrate_tasks` 排空 [walt_halt.c:76](kernel/kernel/sched/walt/walt_halt.c#L76)
  - [x] 两者与 placement/LB 的 `cpu_halted()` 交互
- [x] **boost 机制**（原列表缺失）
  - [x] 全局 boost 状态机 [boost.c](kernel/kernel/sched/walt/boost.c)：4 种 boost 与 refcount
  - [x] per-task boost：`set_task_boost` [walt.c:109](kernel/kernel/sched/walt/walt.c#L109)
  - [x] input boost：PM QoS min-freq 通路 [input-boost.c:129](kernel/kernel/sched/walt/input-boost.c#L129)
- [x] **RT 与 MVP 机制**（原列表缺失）
  - [x] RT placement：`walt_select_task_rq_rt` [walt_rt.c:234](kernel/kernel/sched/walt/walt_rt.c#L234)
  - [x] MVP（Most-Valuable-Task）抢占队列 [walt_cfs.c:1256](kernel/kernel/sched/walt/walt_cfs.c#L1256) 与 `check_preempt_wakeup` / `replace_next_task_fair` 覆写
- [x] **可观测性**（文档要求已提及，此处单列）
  - [x] tunables 全集与含义：`sysctl.c`，按模块归类
  - [x] tracepoint 全集与字段：`schedwalt` 事件组 [trace.h](kernel/kernel/sched/walt/trace.h)
  - [x] 典型观测脚本：抓取窗口负载 / placement 决策 / 调频 reason

### 阶段 3：对照与收敛

- [x] **base vs WALT 对照表**：负载计算 / 调频 / placement / LB 四个维度逐项对比
- [x] **调频详细对照**：`cpufreq_schedutil.c` vs `cpufreq_walt.c` 函数级差异
- [x] 术语表：RTG、colocation、MVP、pipeline/heavy、halt、rotation、pred demand、top task
- [x] 索引与交叉引用检查：确认所有 `file:line` 链接可跳转
- [x] 遗留问题清单：待确认的行为与猜测

---

## 文档要求

- 层次分明，有 overview 与引用索引
- 解析核心数据结构（字段级，说明来源与消费方）
- 解析数据流动过程（配时序/流程说明）
- 引用源码文件与行号，可点击跳转
- 文档内容与源码相互对照，不确定处显式标注
- 有新的问题和要求时，即时更新文档
- 附带关键的 tunables 和 tracepoint 解析

## 产出物约定

- 每个主题一个文件，放在 `notes/` 下对应子目录
- 每份文档头部注明：对应的源码文件、内核版本、最后核对日期
- 行号会随源码变更失效，引用时带上函数名以便重新定位
