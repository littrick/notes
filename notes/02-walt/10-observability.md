# WALT 可观测性：tunables、tracepoint 与观测方法

> **源码**：[sysctl.c](../../kernel/kernel/sched/walt/sysctl.c)、[trace.h](../../kernel/kernel/sched/walt/trace.h)、[walt_tp.c](../../kernel/kernel/sched/walt/walt_tp.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

本文是**参考手册**，不解释算法。每个 tunable 的算法作用请点进对应的主题文档
（[window-model.md](01-window-model.md)、[demand-prediction.md](02-demand-prediction.md) 等），
字段含义见 [03-data-structures.md](../00-overview/03-data-structures.md)。

本文的写法：**能查表解决的绝不写散文**。

---

## 0. 速查

| 想做的事 | 去哪 |
|---|---|
| 看/改所有 WALT 旋钮 | `/proc/sys/walt/`（§1.1） |
| 看 per-task 属性 | `/proc/sys/walt/sched_{wake_up_idle,group_id,low_latency,...}`（§1.12） |
| 打开 WALT 的 ftrace 事件 | `/sys/kernel/debug/tracing/events/**schedwalt**/`（§3.1，注意不是 `sched/`） |
| 看 WALT 现场 | `panic_on_walt_bug` 的 print 位（§3.5）→ dmesg 里的 `WALT RQ DUMP` |
| 调频相关 | `/sys/devices/system/cpu/cpufreq/policyN/` 下的 walt governor 属性（§1.15） |
| core_ctl | `/sys/devices/system/cpu/cpuN/core_ctl/`（§1.15） |

---

## 1. Tunables 全集

### 1.1 注册路径与目录结构

```
register_sysctl_table(walt_base_table)     ← walt_init() [walt.c:5126]
  /proc/sys/walt/                          (mode 0555，只读目录)
    ├── sched_user_hint                    ← walt_table[]  [sysctl.c:586]
    ├── sched_boost
    ├── ...                                 (共 60 个条目，见下)
    └── input_boost/                       (mode 0555)
        ├── input_boost_ms                 ← input_boost_sysctls[] [sysctl.c:555]
        ├── input_boost_freq
        └── sched_boost_on_input
```

`walt_init()` 是**延迟**执行的（`DECLARE_WORK(walt_init_work, walt_init)` [walt.c:5143](../../kernel/kernel/sched/walt/walt.c#L5143)），
所以 `/proc/sys/walt/` 并非在内核启动早期就存在——依赖它的 init 脚本需要先等待。
`register_sysctl_table()` 的返回值被 `kmemleak_not_leak(hdr)` 标记 [walt.c:5127](../../kernel/kernel/sched/walt/walt.c#L5127)。

`walt_base_table[]` 只有一项（`"walt"`，`mode = 0555`，`child = walt_table`），
定义在 `walt_base_table` [sysctl.c:1094](../../kernel/kernel/sched/walt/sysctl.c#L1094)。

**叶子节点总数：62** = `walt_table[]` **60** 项 − 1 个目录项 `input_boost`
+ `input_boost_sysctls[]` **3** 项。`grep -c '\.procname'` 在 `sysctl.c:586-1092`
区间得到 60，在 `sysctl.c:555-584` 区间得到 3，可直接复现。

按 `maxlen` 分类（同样可复现）：

| 形态 | 项数 | 说明 |
|---|---|---|
| 标量（`maxlen = sizeof(x)`） | **40** | 大部分 `sched_*` |
| 数组（`maxlen = sizeof(x) * N`） | **19** | 含 §1.12 的 8 个 per-task 接口、§1.9 的 4 个 hyst 数组、§1.5 的 4 个 margin 数组、§1.13 的 3 个 cluster 表 |
| 目录（无 `maxlen`） | **1** | `input_boost` |

校验命令：

```bash
awk 'NR>=586 && NR<=1092' sysctl.c | grep -c 'maxlen.*sizeof.*\*'   # 19
awk 'NR>=586 && NR<=1092' sysctl.c | grep maxlen | grep -vc '\*'    # 40
```

### 1.2 默认值的三段式 [反直觉]

新手最容易踩的坑：**绝大多数 tunable 的「默认值」不在定义处，而在 `walt_tunables()`**。

| 来源 | 何时生效 | 例子 |
|---|---|---|
| 定义处静态初始化 | 编译期 | `sysctl_sched_min_task_util_for_boost = 51` [sysctl.c:67](../../kernel/kernel/sched/walt/sysctl.c#L67) |
| BSS 零初始化 | 编译期，值 = 0 | 绝大多数 `sysctl_sched_*`（无 `= x`） |
| `walt_tunables()` | **运行时**，`walt_init` 里调用 | `sysctl_sched_coloc_busy_hyst_enable_cpus = 112` [sysctl.c:1135](../../kernel/kernel/sched/walt/sysctl.c#L1135) |
| 其它文件定义 | 编译期 | `sched_lib_name[]` [fixup.c:13](../../kernel/kernel/sched/walt/fixup.c#L13)、`sysctl_sched_force_lb_enable = 1` [walt_lb.c:787](../../kernel/kernel/sched/walt/walt_lb.c#L787) |

**因此读 `sysctl.c` 顶部的变量定义只能得到「静态默认」，运行时的实际初值必须看
`walt_tunables()` [sysctl.c:1103](../../kernel/kernel/sched/walt/sysctl.c#L1103)。**
下表的「默认值」列已合并两者，并标明来源。

### 1.3 窗口 / ravg

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_ravg_window_nr_ticks` | `sysctl_sched_ravg_window_nr_ticks` | `unsigned int` | `HZ / NR_WINDOWS_PER_SEC`（运行时，[sysctl.c:1124](../../kernel/kernel/sched/walt/sysctl.c#L1124)） | 窗口长度，单位 tick。合法值**仅** `{2,3,4,5,8}`，且 **HZ≠250 时写操作直接 `-EPERM`** |
| `sched_window_stats_policy` | `sysctl_sched_window_stats_policy` | `unsigned int __read_mostly` | `WINDOW_STATS_MAX_RECENT_AVG` = **2**（运行时，[sysctl.c:1122](../../kernel/kernel/sched/walt/sysctl.c#L1122)） | `demand` 从 5 个历史窗口取哪个统计量：0=RECENT 1=MAX 2=MAX_RECENT_AVG 3=AVG，范围 [0,4] |
| `sched_task_unfilter_period` | `sysctl_sched_task_unfilter_period` | `unsigned int` | **100000000** (100ms)（运行时，[sysctl.c:1120](../../kernel/kernel/sched/walt/sysctl.c#L1120)） | `wts->unfilter` 的初值/衰减周期，范围 [1, 200000000] |

`sched_ravg_window_nr_ticks` 的写路径是 `sched_ravg_window_handler`
[sysctl.c:148](../../kernel/kernel/sched/walt/sysctl.c#L148)，它会调用
`sched_window_nr_ticks_change()`；实际生效与 `trace_sched_ravg_window_change` 在
`__walt_irq_work_locked()` [walt.c:4118](../../kernel/kernel/sched/walt/walt.c#L4118)。

详见 [window-model.md §2](01-window-model.md)。

### 1.4 全局 boost 与输入 boost

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_boost` | `sysctl_sched_boost` | `unsigned int` | `FULL_THROTTLE_BOOST` = **1**（`walt_boost_init()` 强制设置，[boost.c:299](../../kernel/kernel/sched/walt/boost.c#L299)） | 全局 boost 请求。正值=enable，负值=disable，0=全部关闭。范围 [−3, 3] |
| `walt_rtg_cfs_boost_prio` | `sysctl_walt_rtg_cfs_boost_prio` | `unsigned int` | **99**（=禁用，[sysctl.c:72](../../kernel/kernel/sched/walt/sysctl.c#L72)） | RTG 任务的 CFS 抢占优先级，范围 [99,119]；99 表示不 boost |
| `input_boost/input_boost_ms` | `sysctl_input_boost_ms` | `unsigned int` | **40**（运行时，[sysctl.c:1143](../../kernel/kernel/sched/walt/sysctl.c#L1143)） | 输入事件后 boost 持续毫秒数，范围 [0,100000]；0 = 关闭 |
| `input_boost/input_boost_freq` | `sysctl_input_boost_freq[8]` | `unsigned int[8]` | 全 **0**（运行时，[sysctl.c:1145-1146](../../kernel/kernel/sched/walt/sysctl.c#L1145-L1146)） | per-CPU 输入 boost 的最小频率 (kHz)，0 = 该 CPU 不 boost。数组下标 = CPU |
| `input_boost/sched_boost_on_input` | `sysctl_sched_boost_on_input` | `unsigned int` | **0**（BSS） | 输入事件同时触发的 `sched_boost` 类型，透传给 `sched_set_boost()` [input-boost.c:152](../../kernel/kernel/sched/walt/input-boost.c#L152) |

**`sysctl_sched_boost` 的语义 [反直觉]**：该变量同时是「用户请求」和「生效值」。
`_sched_set_boost()` [boost.c:237](../../kernel/kernel/sched/walt/boost.c#L237) 先按
refcount 聚合出 `sched_effective_boost()`，**再用聚合结果覆写 `sysctl_sched_boost`**
[boost.c:254](../../kernel/kernel/sched/walt/boost.c#L254)。所以 `cat sched_boost`
读到的可能不是你刚写进去的值。boost 类型常量见 [walt.h:342-349](../../kernel/kernel/sched/walt/walt.h#L342-L349)。

### 1.5 放置（placement）

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_upmigrate` | `sysctl_sched_capacity_margin_up_pct[MAX_MARGIN_LEVELS]` | `unsigned int[2]` | **95** (≈5% margin)（运行时，[sysctl.c:1108](../../kernel/kernel/sched/walt/sysctl.c#L1108)） | 上行迁移的容量余量百分比，范围 [1,100]；写后换算成 `sched_capacity_margin_up[]`（定点 ×1024） |
| `sched_downmigrate` | `sysctl_sched_capacity_margin_dn_pct[MAX_MARGIN_LEVELS]` | `unsigned int[2]` | **85** (≈15% margin)（运行时，[sysctl.c:1109](../../kernel/kernel/sched/walt/sysctl.c#L1109)） | 下行迁移的容量余量百分比，范围 [1,100] |
| `sched_early_upmigrate` | `sysctl_sched_early_up[MAX_MARGIN_LEVELS]` | `unsigned int[2]` | **1077**（运行时，[sysctl.c:1110](../../kernel/kernel/sched/walt/sysctl.c#L1110)） | 早迁阈值（定点 ×1024），必须 ≥1024 且严格小于同簇 `early_down` |
| `sched_early_downmigrate` | `sysctl_sched_early_down[MAX_MARGIN_LEVELS]` | `unsigned int[2]` | **1204**（运行时，[sysctl.c:1111](../../kernel/kernel/sched/walt/sysctl.c#L1111)） | 早迁下滑阈值，必须 >1024 且严格大于同簇 `early_up` |
| `sched_group_upmigrate` | `sysctl_sched_group_upmigrate_pct` | `unsigned int __read_mostly` | **100**（运行时，[sysctl.c:1114](../../kernel/kernel/sched/walt/sysctl.c#L1114)） | RTG **组**上行迁移阈值（百分比），下限被 clamp 到 `group_downmigrate` |
| `sched_group_downmigrate` | `sysctl_sched_group_downmigrate_pct` | `unsigned int __read_mostly` | **95**（运行时，[sysctl.c:1116](../../kernel/kernel/sched/walt/sysctl.c#L1116)） | RTG 组下行迁移阈值，上限被 clamp 到 `group_upmigrate` |
| `sched_sync_hint_enable` | `sysctl_sched_sync_hint_enable` | `unsigned int` | **1**（[sysctl.c:73](../../kernel/kernel/sched/walt/sysctl.c#L73)） | 唤醒同步提示（wakee 切到 waker 所在 CPU）开关 |
| `sched_conservative_pl` | `sysctl_sched_conservative_pl` | `unsigned int` | **0**（BSS） | 保守的 pred_demand 参与调频策略 |
| `sched_min_task_util_for_boost` | `sysctl_sched_min_task_util_for_boost` | `unsigned int` | **51**（[sysctl.c:67](../../kernel/kernel/sched/walt/sysctl.c#L67)） | task_util 低于此值的任务在 CONSERVATIVE_BOOST 下不上大核，范围 [0,1000] |
| `sched_min_task_util_for_uclamp` | `sysctl_sched_min_task_util_for_uclamp` | `unsigned int` | **51**（[sysctl.c:68](../../kernel/kernel/sched/walt/sysctl.c#L68)） | uclamp_min 生效所需的最小 task_util |
| `sched_many_wakeup_threshold` | `sysctl_sched_many_wakeup_threshold` | `unsigned int` | `WALT_MANY_WAKEUP_DEFAULT` = **1000**（[sysctl.c:70](../../kernel/kernel/sched/walt/sysctl.c#L70)） | `sibling_count_hint` 达到此值算「many wakeup」，范围 [2,1000] |
| `sched_asym_cap_sibling_freq_match_pct` | `sysctl_sched_asym_cap_sibling_freq_match_pct` | `unsigned int __read_mostly` | **100**（运行时，[sysctl.c:1118](../../kernel/kernel/sched/walt/sysctl.c#L1118)） | 同簇异构兄弟核的频率匹配百分比，范围 [1,100] |
| `sched_suppress_region2` | `sysctl_sched_suppress_region2` | `unsigned int` | **0**（BSS） | 禁止 region2（跨簇能量评估）扫描，见 [walt_cfs.c:303](../../kernel/kernel/sched/walt/walt_cfs.c#L303) |
| `sched_asymcap_boost` | `sysctl_sched_asymcap_boost` | `unsigned int` | **0**（BSS） | 启动时也扫描 prime 簇做非对称容量 boost，见 [walt_cfs.c:275](../../kernel/kernel/sched/walt/walt_cfs.c#L275) |
| `sched_idle_enough` | `sysctl_sched_idle_enough` | `unsigned int` | **0**（BSS） | 允许 cluster packing 的 CPU/任务 util 上限；**0 = 关闭 packing**，见 [walt.h:1064](../../kernel/kernel/sched/walt/walt.h#L1064) |
| `sched_cluster_util_thres_pct` | `sysctl_sched_cluster_util_thres_pct` | `unsigned int` | **0**（BSS） | 允许 packing 的簇利用率上限（%）；**0 = 关闭 packing** |

`sched_upmigrate` / `sched_downmigrate` 由 `sched_updown_migrate_handler`
[sysctl.c:424](../../kernel/kernel/sched/walt/sysctl.c#L424) 处理，**强制 up ≥ dn**；
`early_*` 由 `sched_updown_early_migrate_handler`
[sysctl.c:491](../../kernel/kernel/sched/walt/sysctl.c#L491) 处理，**强制 up < dn**——
两组约束方向相反，写错方向会直接 `-EINVAL`。

> **`early_upmigrate` / `early_downmigrate` 在本快照中无读者**，见 §1.14。

### 1.6 共置（colocation）

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_min_task_util_for_colocation` | `sysctl_sched_min_task_util_for_colocation` | `unsigned int` | **35**（[sysctl.c:69](../../kernel/kernel/sched/walt/sysctl.c#L69)） | `demand_scaled` 超过此值才把任务计入 RTG 共置需求 / 刷新 `unfilter`，见 [walt.c:2054](../../kernel/kernel/sched/walt/walt.c#L2054) |
| `sched_coloc_downmigrate_ns` | `sysctl_sched_coloc_downmigrate_ns` | `unsigned int __read_mostly` | **0**（BSS） | RTG 共置保持时间（ns），配 `sched_hyst_min_coloc_ns` 一起判定 `skip_min` 是否维持，见 [walt.c:2897](../../kernel/kernel/sched/walt/walt.c#L2897) |
| `sched_hyst_min_coloc_ns` | `sysctl_sched_hyst_min_coloc_ns` | `unsigned int` | **80000000** (80ms)（[sysctl.c:77](../../kernel/kernel/sched/walt/sysctl.c#L77)） | `skip_min` 的最小保持时长下限 |
| `task_load_boost` | `wts->load_boost`（per-task，§1.12） | — | — | 单任务负载 boost，范围 [−90, 90]，折算为 `boosted_task_load = 1024 * val / 100` [sysctl.c:365](../../kernel/kernel/sched/walt/sysctl.c#L365) |

`sched_hyst_min_coloc_ns` 会被 `sched_set_preferred_cluster` tracepoint 一并打出
（[trace.h:337](../../kernel/kernel/sched/walt/trace.h#L337)），排查共置抖动时很有用。

### 1.7 负载均衡（LB）

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_walt_rotate_big_tasks` | `sysctl_sched_walt_rotate_big_tasks` | `unsigned int` | **0**（BSS） | 轮转大任务（把 big task 推到高端 CPU）；仅 `sched_boost_type == NO_BOOST` 时生效 [walt.c:4265](../../kernel/kernel/sched/walt/walt.c#L4265) |
| `sched_force_lb_enable` | `sysctl_sched_force_lb_enable` | `unsigned int __read_mostly` | **1**（[walt_lb.c:787](../../kernel/kernel/sched/walt/walt_lb.c#L787)） | 强制在 min cluster 也跑 newidle balance，见 [walt_lb.c:792](../../kernel/kernel/sched/walt/walt_lb.c#L792) |
| `sched_heavy_nr` | `sysctl_sched_heavy_nr` | `unsigned int` | **0**（BSS） | 「重载」rq 的 nr_running 阈值，范围 [0, `WALT_NR_CPUS`=8]，见 [walt.c:3678](../../kernel/kernel/sched/walt/walt.c#L3678) |
| `sched_skip_sp_newly_idle_lb` | `sysctl_sched_skip_sp_newly_idle_lb` | `unsigned int` | **1**（[sysctl.c:76](../../kernel/kernel/sched/walt/sysctl.c#L76)） | **本快照中无读者**，见 §1.14 |

### 1.8 RT

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_long_running_rt_task_ms` | `sysctl_sched_long_running_rt_task_ms` | `unsigned int` | **0**（BSS） | RT 任务运行超过此毫秒数即视为 long-running 并打印栈，范围 [0,2000]；0 = 关闭 |
| `walt_low_latency_task_threshold` | `sysctl_walt_low_latency_task_threshold` | `unsigned int` | **0**（BSS，注释标 "disabled by default" [sysctl.c:65](../../kernel/kernel/sched/walt/sysctl.c#L65)） | BINDER/PROCFS 类 low-latency 任务的 `task_util` 上限，见 [walt.h:462](../../kernel/kernel/sched/walt/walt.h#L462)；**0 表示所有这类任务都不算 low-latency** |

`sched_long_running_rt_task_ms` 的写路径是 `sched_long_running_rt_task_ms_handler`
[walt_rt.c:68](../../kernel/kernel/sched/walt/walt_rt.c#L68)，它只在写时唤醒
`long_running_rt_task_notifier` 的采样。

### 1.9 Busy hysteresis（三组，同一处理器）

三组 hysteresis 全部由 `sched_busy_hyst_handler`
[sched_avg.c:247](../../kernel/kernel/sched/walt/sched_avg.c#L247) 处理，
写成功后调用 `sched_update_hyst_times()` [sched_avg.c:162](../../kernel/kernel/sched/walt/sched_avg.c#L162) 重算 per-CPU 缓存。

| 组 | sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|---|
| busy | `sched_busy_hysteresis_enable_cpus` | `sysctl_sched_busy_hyst_enable_cpus` | `unsigned int`(位图) | **0**（BSS） | 使能 busy hyst 的 CPU 位图，范围 [0,255] |
| busy | `sched_busy_hyst_ns` | `sysctl_sched_busy_hyst` | `unsigned int` | **0**（BSS） | busy hyst 时长 (ns)，范围 [0, `NSEC_PER_SEC`] |
| coloc | `sched_coloc_busy_hysteresis_enable_cpus` | `sysctl_sched_coloc_busy_hyst_enable_cpus` | `unsigned int`(位图) | **112** = `0b1110000` (CPU4~6)（运行时，[sysctl.c:1135](../../kernel/kernel/sched/walt/sysctl.c#L1135)） | 使能 coloc hyst 的 CPU 位图 |
| coloc | `sched_coloc_busy_hyst_cpu_ns` | `sysctl_sched_coloc_busy_hyst_cpu[WALT_NR_CPUS]` | `unsigned int[8]` | **39000000** (39ms) / CPU（运行时，[sysctl.c:1129](../../kernel/kernel/sched/walt/sysctl.c#L1129)） | per-CPU coloc hyst 时长 |
| coloc | `sched_coloc_busy_hyst_max_ms` | `sysctl_sched_coloc_busy_hyst_max_ms` | `unsigned int` | **5000** (5s)（运行时，[sysctl.c:1139](../../kernel/kernel/sched/walt/sysctl.c#L1139)） | RTG 活跃时间上限 `MAX_RTGB_TIME` 的换算基（[sched_avg.c:35](../../kernel/kernel/sched/walt/sched_avg.c#L35)），范围 [0,100000] |
| coloc | `sched_coloc_busy_hyst_cpu_busy_pct` | `sysctl_sched_coloc_busy_hyst_cpu_busy_pct[8]` | `unsigned int[8]` | **10** (%) / CPU（运行时，[sysctl.c:1130](../../kernel/kernel/sched/walt/sysctl.c#L1130)） | coloc 触发所需的 CPU busy 百分比 |
| util | `sched_util_busy_hysteresis_enable_cpus` | `sysctl_sched_util_busy_hyst_enable_cpus` | `unsigned int`(位图) | **255** = 全部 CPU（运行时，[sysctl.c:1137](../../kernel/kernel/sched/walt/sysctl.c#L1137)） | 使能 util hyst 的 CPU 位图 |
| util | `sched_util_busy_hyst_cpu_ns` | `sysctl_sched_util_busy_hyst_cpu[8]` | `unsigned int[8]` | **5000000** (5ms) / CPU（运行时，[sysctl.c:1131](../../kernel/kernel/sched/walt/sysctl.c#L1131)） | per-CPU util hyst 时长 |
| util | `sched_util_busy_hyst_cpu_util` | `sysctl_sched_util_busy_hyst_cpu_util[8]` | `unsigned int[8]` | **15** / CPU（运行时，[sysctl.c:1132](../../kernel/kernel/sched/walt/sysctl.c#L1132)） | 系统总 util 门限（换算后），超过才触发 util hyst |

> 命名不一致 [反直觉]：**enable 用的是 "hysteresis"，时长用的是 "hyst"**
> （`sched_busy_hysteresis_enable_cpus` vs `sched_busy_hyst_ns`）。
> 写成 `sched_busy_hyst_enable_cpus` 会静默失败。

`update_busy_hyst_end_time()` [sched_avg.c:193](../../kernel/kernel/sched/walt/sched_avg.c#L193)
里有三个硬编码常量，不可调：`BUSY_NR_RUN = 3`、`BUSY_LOAD_FACTOR = 10`
[sched_avg.c:190-191](../../kernel/kernel/sched/walt/sched_avg.c#L190-L191)。

### 1.10 调频 / EM

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `sched_user_hint` | `sysctl_sched_user_hint` | `unsigned int` | **0**（[walt.c:53](../../kernel/kernel/sched/walt/walt.c#L53)） | 用户空间频率 hint，范围 [0, `sched_user_hint_max`=1000]；写入后 1 秒自动清零 [walt.c:4053-4055](../../kernel/kernel/sched/walt/walt.c#L4053-L4055) |
| `sched_ed_boost` | `sysctl_ed_boost_pct` | `unsigned int` | **0**（BSS） | early-detection 任务的调频 boost 百分比，范围 [0,100]；0 = 关闭 [cpufreq_walt.c:314](../../kernel/kernel/sched/walt/cpufreq_walt.c#L314) |
| `sched_em_inflate_pct` | `sysctl_em_inflate_pct` | `unsigned int` | **100**（[sysctl.c:83](../../kernel/kernel/sched/walt/sysctl.c#L83)） | EM 能耗放大百分比（`cpu==0` 且 util 超阈值时用），范围 [100,1000] |
| `sched_em_inflate_thres` | `sysctl_em_inflate_thres` | `unsigned int` | **1024**（[sysctl.c:84](../../kernel/kernel/sched/walt/sysctl.c#L84)） | 触发 EM 放大的 util 阈值，范围 [0,1024] |

`sched_user_hint` 的写路径是 `walt_proc_user_hint_handler`
[sysctl.c:125](../../kernel/kernel/sched/walt/sysctl.c#L125)：它会
`walt_irq_work_queue(&walt_migration_irq_work)` 立即触发一次重算，并设置
`sched_user_hint_reset_time = jiffies + HZ`。

`sched_em_inflate_*` 只在 `cpu == 0` 时生效 [walt_cfs.c:720](../../kernel/kernel/sched/walt/walt_cfs.c#L720)——
这是一个**看起来像 bug 的硬编码**，见 [walt_cfs.c:720-721](../../kernel/kernel/sched/walt/walt_cfs.c#L720-L721)。

### 1.11 调试与观测开关

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `panic_on_walt_bug` | `sysctl_panic_on_walt_bug` | `unsigned int` | `walt_debug_initial_values()` = **0x4544DE33**（[sysctl.c:74](../../kernel/kernel/sched/walt/sysctl.c#L74)） | WALT 断言行为位掩码，详见 §3.5 |
| `sched_enable_tp` | `sysctl_sched_dynamic_tp_enable` | `unsigned int` | **0**（BSS，[walt_tp.c:13](../../kernel/kernel/sched/walt/walt_tp.c#L13)） | 动态注册 `sched_overutilized` + `sched_switch_with_ctrs` 事件；0/1 切换 via `sched_dynamic_tp_handler` [walt_tp.c:130](../../kernel/kernel/sched/walt/walt_tp.c#L130) |
| `sched_lib_name` | `sched_lib_name[LIB_PATH_LENGTH]` | `char[512]` | **""**（[fixup.c:13](../../kernel/kernel/sched/walt/fixup.c#L13)） | 需要做 32-bit fixup 的库路径，`proc_dostring` |
| `sched_lib_mask_force` | `sched_lib_mask_force` | `unsigned int` | **0**（[fixup.c:14](../../kernel/kernel/sched/walt/fixup.c#L14)） | 强制应用 lib fixup 的 CPU 位图，范围 [0,255]，见 [fixup.c:82](../../kernel/kernel/sched/walt/fixup.c#L82) |
| `sched_task_read_pid` | `sysctl_task_read_pid` | `int` | **1**（[sysctl.c:89](../../kernel/kernel/sched/walt/sysctl.c#L89)） | 读取 per-task 属性（§1.12）时的目标 pid，范围 [1,INT_MAX] |

`sched_enable_tp` 是**唯一**能把 tracepoint 动态挂上的开关：置 1 时
`walt_register_dynamic_tp_events()` 会 `register_trace_sched_overutilized_tp()` 并
注册 `sched_switch` notifier 采集 PMU 计数器；置 0 时注销
[walt_tp.c:118-128](../../kernel/kernel/sched/walt/walt_tp.c#L118-L128)。

### 1.12 Per-task 接口（`sched_task_handler` 一组）

这一组全部由 `sched_task_handler` [sysctl.c:211](../../kernel/kernel/sched/walt/sysctl.c#L211)
处理，`maxlen = sizeof(unsigned int) * 2`，**读写都是 `"<pid> <value>"` 两个整数**：

```bash
cat  /proc/sys/walt/sched_group_id          # 读：用 sched_task_read_pid 指定的 pid
echo "1234 5" > /proc/sys/walt/sched_group_id   # 写：把 pid 1234 放进 RTG 5
```

参数枚举 `enum { TASK_BEGIN, WAKE_UP_IDLE, INIT_TASK_LOAD, GROUP_ID, PER_TASK_BOOST,
PER_TASK_BOOST_PERIOD_MS, LOW_LATENCY, PIPELINE, LOAD_BOOST }` [sysctl.c:199-209](../../kernel/kernel/sched/walt/sysctl.c#L199-L209)，
通过 `table->data` 强转传递。

| sysctl 文件 | `table->data` | 目标字段 | 写入范围 / 约束 | 含义 |
|---|---|---|---|---|
| `sched_wake_up_idle` | `WAKE_UP_IDLE`=1 | `wts->wake_up_idle` | ≥0 | 该任务唤醒后优先选 idle CPU（“wake up idle”） |
| `sched_init_task_load` | `INIT_TASK_LOAD`=2 | `wts->init_load_pct` | [0,100] | fork 时继承的初始负载百分比 |
| `sched_group_id` | `GROUP_ID`=3 | `sched_set_group_id()` | ≥0 | 加入/退出 RTG（相关线程组） |
| `sched_per_task_boost` | `PER_TASK_BOOST`=4 | `wts->boost` | `[TASK_BOOST_NONE, TASK_BOOST_END)` = [0,4) | 单任务 boost 类型；置 0 时同时清 `boost_period` |
| `sched_per_task_boost_period_ms` | `PER_TASK_BOOST_PERIOD_MS`=5 | `wts->boost_period` / `boost_expires` | ≥0；`boost==0 && val!=0` → `-EINVAL` | boost 持续毫秒（内部 ×1e6 存为 ns） |
| `sched_low_latency` | `LOW_LATENCY`=6 | `wts->low_latency \| WALT_LOW_LATENCY_PROCFS` | 0/1 | 标记为 low-latency 任务 |
| `sched_pipeline` | `PIPELINE`=7 | `add_pipeline()` / `remove_pipeline()` | 0/1；`TASK_DEAD` → `-EINVAL` | 流水线亲和（把整条链钉在同一 CPU 序列） |
| `task_load_boost` | `LOAD_BOOST`=8 | `wts->load_boost` / `boosted_task_load` | **[−90, 90]**（唯一允许负值的一项） | 任务负载人为加/减百分比 |

`TASK_BOOST_*` 取值见 [include/linux/sched/walt.h:32-38](../../kernel/include/linux/sched/walt.h#L32-L38)：
`NONE=0, ON_MID=1, ON_MAX=2, STRICT_MAX=3, END=4`。

**每一项写成功后都会打一条 `sched_task_handler` tracepoint**，带 6 层调用栈
[sysctl.c:373-374](../../kernel/kernel/sched/walt/sysctl.c#L373-L374)——审计谁改了
调度参数时非常有用（§2.10）。

### 1.13 cluster 关系表

| sysctl 文件 | 变量 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `cluster0_rel` | `sysctl_cluster_arr[0]` | `int[15]` | 全 0（BSS） | 簇 0 的「忽略簇」关系表，`sched_ignore_cluster_handler` [walt_cfs.c:167](../../kernel/kernel/sched/walt/walt_cfs.c#L167) |
| `cluster1_rel` | `sysctl_cluster_arr[1]` | `int[15]` | 全 0 | 同上 |
| `cluster2_rel` | `sysctl_cluster_arr[2]` | `int[15]` | 全 0 | 同上 |

**只允许写一次 [反直觉]**：handler 里有 `static int configured[3]`，
写入成功后置位，**后续写入直接 `return ret`（=0）丢弃而不报错**
[walt_cfs.c:181-192](../../kernel/kernel/sched/walt/walt_cfs.c#L181-L192)。
`echo x > clusterN_rel` 第二次以后会静默无效。
另有 `if (index >= num_sched_clusters - 1) return -EINVAL;`——最高簇的表不可写。

每簇 15 个 int 按 3 个一组解释（最多 5 组），且下标 0 与 2 组都必须是
`(1, 1024]` 的合法值，否则配置中断 [walt_cfs.c:194-213](../../kernel/kernel/sched/walt/walt_cfs.c#L194-L213)。

### 1.14 暴露但无人读取的 tunable [反直觉]

以下 tunable 在 `sysctl.c` 中**定义并注册**，但全树（`kernel/` + `include/`）
**没有任何读取点**。它们在本快照里是纯装饰：

| sysctl 文件 | 变量 | 定义 |
|---|---|---|
| `sched_early_upmigrate` | `sysctl_sched_early_up[]` | [sysctl.c:52](../../kernel/kernel/sched/walt/sysctl.c#L52) |
| `sched_early_downmigrate` | `sysctl_sched_early_down[]` | [sysctl.c:53](../../kernel/kernel/sched/walt/sysctl.c#L53) |
| `sched_skip_sp_newly_idle_lb` | `sysctl_sched_skip_sp_newly_idle_lb` | [sysctl.c:76](../../kernel/kernel/sched/walt/sysctl.c#L76) |

验证方法（可直接复现）：

```bash
cd kernel/kernel/sched/walt
grep -rn "sysctl_sched_early_up\|sysctl_sched_early_down" .   # 只有 sysctl.c 自己
grep -rn "sysctl_sched_skip_sp_newly_idle_lb" .               # 同上
```

`early_up/down` 只在 handler 内互相校验（`up < down`），校验完就存进数组，从不被消费。
若在 issue 中看到「调 early_upmigrate 没效果」，答案就在这里——
不是算法问题，是**这一版把它们摘掉了**。

> 对比：`sched_early_up` 常被误认为与 `sched_upmigrate` 配对使用。
> 实际生效的迁移余量是 `sched_capacity_margin_up[]`
> （由 `sched_upmigrate` 换算而来），见 [walt.h:578](../../kernel/kernel/sched/walt/walt.h#L578)、
> [walt.h:790-798](../../kernel/kernel/sched/walt/walt.h#L790-L798)。

### 1.15 WALT 目录之外的 tunables

这些不在 `/proc/sys/walt/` 下，但调 WALT 时同样要动。

**a) walt governor（cpufreq sysfs）** — `/sys/devices/system/cpu/cpufreq/policyN/`

属性列表在 `waltgov_attributes[]` [cpufreq_walt.c:834](../../kernel/kernel/sched/walt/cpufreq_walt.c#L834)：

| 属性 | 含义 |
|---|---|
| `up_rate_limit_us` / `down_rate_limit_us` | 调频速率限制 [cpufreq_walt.c:551-552](../../kernel/kernel/sched/walt/cpufreq_walt.c#L551-L552) |
| `hispeed_load` / `hispeed_freq` | 超载即跳到 `hispeed_freq` |
| `rtg_boost_freq` | RTG 活跃时的最低频率 |
| `pl` | pred_demand load 参与调频的开关 |
| `boost` | conservative boost 的调频 boost |
| `adaptive_low_freq` / `adaptive_high_freq` | 自适应频率窗口，可用 `cpufreq_walt_set_adaptive_freq()` 从内核内改 [cpufreq_walt.c:699](../../kernel/kernel/sched/walt/cpufreq_walt.c#L699)（已 `EXPORT_SYMBOL` [cpufreq_walt.c:717](../../kernel/kernel/sched/walt/cpufreq_walt.c#L717)） |
| `target_load_thresh` / `target_load_shift` | 目标负载门限及移位 |

**b) core_ctl（cpu device sysfs）** — `/sys/devices/system/cpu/cpuN/core_ctl/`
（`kobject_add(&cluster->kobj, &dev->kobj, "core_ctl")` [core_ctl.c:1521](../../kernel/kernel/sched/walt/core_ctl.c#L1521)）

属性列表在 `default_attrs[]` [core_ctl.c:453-467](../../kernel/kernel/sched/walt/core_ctl.c#L453-L467)，
每个属性由 `core_ctl_attr_rw()` / `core_ctl_attr_ro()` 宏声明
[core_ctl.c:440-451](../../kernel/kernel/sched/walt/core_ctl.c#L440-L451)：

| 属性 | 权限 | 默认值 | 含义 |
|---|---|---|---|
| `min_cpus` | rw | 1（[core_ctl.c:1491](../../kernel/kernel/sched/walt/core_ctl.c#L1491)） | 该簇最少在线 CPU |
| `max_cpus` | rw | `num_cpus`（[core_ctl.c:1492](../../kernel/kernel/sched/walt/core_ctl.c#L1492)） | 该簇最多在线 CPU |
| `offline_delay_ms` | rw | 100（[core_ctl.c:1496](../../kernel/kernel/sched/walt/core_ctl.c#L1496)） | 下线延迟 |
| `busy_up_thres` | rw | **0**（BSS 零初始化） | 判定「忙」的 busy 百分比阈值数组 |
| `busy_down_thres` | rw | **0**（BSS 零初始化） | 判定「闲」的阈值数组 |
| `task_thres` | rw | `UINT_MAX`（[core_ctl.c:1497](../../kernel/kernel/sched/walt/core_ctl.c#L1497)） | 任务数阈值 |
| `nr_prev_assist_thresh` | rw | `UINT_MAX`（[core_ctl.c:1498](../../kernel/kernel/sched/walt/core_ctl.c#L1498)） | 前簇 assist 生效阈值 |
| `need_cpus` | **ro** | 初始 `num_cpus` | core_ctl 算出的目标在线数 |
| `active_cpus` | **ro** | — | 当前在线数 |
| `global_state` | **ro** | — | 整个系统的 core_ctl 状态快照 |
| `not_preferred` | rw | 0 | 该簇不被优先使用 |
| `enable` | rw | `true` | 该簇 core_ctl 总开关 |

`busy_up_thres` / `busy_down_thres` 是 `struct cluster_data` 的数组字段
（[core_ctl.c:31-32](../../kernel/kernel/sched/walt/core_ctl.c#L31-L32)），
而全局的 `cluster_state[]` 是**纯 BSS**（`static struct cluster_data
cluster_state[MAX_CLUSTERS];` 无初始化器 [core_ctl.c:69](../../kernel/kernel/sched/walt/core_ctl.c#L69)），
`core_ctl_probe()` 的初始化段 [core_ctl.c:1490-1502](../../kernel/kernel/sched/walt/core_ctl.c#L1490-L1502)
也**没有**给这两个字段赋初值——**依赖用户空间 init 脚本写入**。
不写就恒为 0，于是 `c->busy_pct >= 0` 恒真，`is_busy` 永远为 true
（[core_ctl.c:912-916](../../kernel/kernel/sched/walt/core_ctl.c#L912-L916)）。`[推测]`

**c) preemptirq 调试** — `/proc/sys/preemptirq/`
（`register_sysctl("preemptirq", preemptirq_long_table)` [preemptirq_long.c:165](../../kernel/kernel/sched/walt/preemptirq_long.c#L165)）

| sysctl 文件 | 变量 | 默认值 | 含义 |
|---|---|---|---|
| `preemptoff_tracing_threshold_ns` | `sysctl_preemptoff_tracing_threshold_ns` | **1000000** (1ms)（[preemptirq_long.c:18](../../kernel/kernel/sched/walt/preemptirq_long.c#L18)） | 关抢占超时阈值；handler 是 `proc_dointvec`，**无范围约束** [preemptirq_long.c:121-127](../../kernel/kernel/sched/walt/preemptirq_long.c#L121-L127) |
| `irqsoff_tracing_threshold_ns` | `sysctl_irqsoff_tracing_threshold_ns` | **5000000** (5ms)（[preemptirq_long.c:19](../../kernel/kernel/sched/walt/preemptirq_long.c#L19)） | 关中断超时阈值，范围 [500000, 100000000] |
| `irqsoff_dmesg_output_enabled` | `sysctl_irqsoff_dmesg_output_enabled` | **0**（BSS，[preemptirq_long.c:20](../../kernel/kernel/sched/walt/preemptirq_long.c#L20)） | 是否额外 printk |
| `irqsoff_crash_sentinel_value` | `sysctl_irqsoff_crash_sentinel_value` | **0**（BSS，[preemptirq_long.c:21](../../kernel/kernel/sched/walt/preemptirq_long.c#L21)） | 触发 crash 的哨兵值，与 `IRQSOFF_SENTINEL`=0x0fffDEAD [preemptirq_long.c:16](../../kernel/kernel/sched/walt/preemptirq_long.c#L16) 配合 |
| `irqsoff_crash_threshold_ns` | `sysctl_irqsoff_crash_threshold_ns` | **10000000** (10ms)（[preemptirq_long.c:22](../../kernel/kernel/sched/walt/preemptirq_long.c#L22)） | 超时即 crash 的阈值，范围 [1000000, 100000000] |

注意这五个是 `static` 变量，只在 `CONFIG_SCHED_WALT_DEBUG` 模块中注册
（`obj-$(CONFIG_SCHED_WALT_DEBUG) += sched-walt-debug.o` [Makefile:9](../../kernel/kernel/sched/walt/Makefile#L9)）。

---

## 2. Tracepoint 全集

### 2.1 声明、TRACE_SYSTEM 与导出

| 项 | 事实 |
|---|---|
| 事件定义文件 | `trace.h`（1544 行，38 个 `TRACE_EVENT` + 1 个 `DECLARE_EVENT_CLASS` / 4 个 `DEFINE_EVENT`） |
| `TRACE_SYSTEM` | **`schedwalt`** [trace.h:8](../../kernel/kernel/sched/walt/trace.h#L8) |
| `CREATE_TRACE_POINTS` | 在 `trace.c` [trace.c:83-84](../../kernel/kernel/sched/walt/trace.c#L83-L84) —— 不是 `walt_tp.c` |
| 事件名字符串表 | `task_event_names[]` [walt.c:27](../../kernel/kernel/sched/walt/walt.c#L27)（6 项）、`migrate_type_names[]` [walt.c:36](../../kernel/kernel/sched/walt/walt.c#L36)（4 项） |
| `printk` 辅助 | `__window_print()` [trace.c:17](../../kernel/kernel/sched/walt/trace.c#L17)、`__window_data()` [trace.c:8](../../kernel/kernel/sched/walt/trace.c#L8)、`__get_update_sum()` [trace.c:63](../../kernel/kernel/sched/walt/trace.c#L63) |

> **重要更正**：把 WALT 的 tracepoint 导出给模块用的 `EXPORT_TRACEPOINT_SYMBOL`
> **在本目录中一处也没有**（`grep -rn EXPORT_TRACEPOINT_SYMBOL kernel/sched/walt/`
> → **0**）。整个 `kernel/` 树里有 891 处 `EXPORT_TRACEPOINT_SYMBOL*`，但
> 逐一过滤后**没有任何一处**是 `schedwalt` / `core_ctl_*` / `halt_cpus*` /
> `walt_*` / `*ravg*` / `*mvp*` / `*pred_demand*` 事件名
> （`grep -rn EXPORT_TRACEPOINT_SYMBOL . | grep -iE "walt|core_ctl|halt_cpus|ravg|mvp|pred_demand"` → 空）。
> `walt_tp.c` 里只有一处
> `CREATE_TRACE_POINTS` [walt_tp.c:10-11](../../kernel/kernel/sched/walt/walt_tp.c#L10-L11)，
> 而且是给 **`perf_trace_counters.h`** 用的，不是给 `trace.h` 用的。
> 因此 `schedwalt` 事件**只对内核内调用者可见，不可从模块调用**。
> 如果你是照着别的内核版本找 `EXPORT_TRACEPOINT_SYMBOL`，在这里会白找。

`TRACE_SYSTEM` 决定 ftrace 目录名，因此路径是
`events/schedwalt/`，**不是** `events/sched/`：

| 头文件 | `TRACE_SYSTEM` | ftrace 目录 |
|---|---|---|
| `trace.h` | `schedwalt` | `events/schedwalt/` |
| `perf_trace_counters.h` | `perf_trace_counters` [perf_trace_counters.h:7](../../kernel/kernel/sched/walt/perf_trace_counters.h#L7) | `events/perf_trace_counters/` |
| `preemptirq_long.h` | `preemptirq_long` [preemptirq_long.h:7](../../kernel/kernel/sched/walt/preemptirq_long.h#L7) | `events/preemptirq_long/` |

### 2.2 窗口 / rollover

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `walt_window_rollover` | `run_walt_irq_work_rollover()` [walt.c:2283](../../kernel/kernel/sched/walt/walt.c#L2283) | 每个窗口**全局只打一次**（`atomic64_cmpxchg` 成功者才打）。时间戳抖动 = 窗口节拍不稳；用 `window_start` 差算实际窗口长度 |
| `sched_ravg_window_change` | `__walt_irq_work_locked()` [walt.c:4118](../../kernel/kernel/sched/walt/walt.c#L4118) | 窗口长度被改（`sched_ravg_window_nr_ticks` 生效点）。改窗口后所有历史环被截断，是性能毛刺的常见嫌疑 |
| `sched_get_nr_running_avg` | `sched_get_nr_running_avg()` [sched_avg.c:113](../../kernel/kernel/sched/walt/sched_avg.c#L113) | `nr`, `nr_misfit`, `nr_max`, `nr_scaled`；core_ctl / 调频的输入数据 |
| `sched_busy_hyst_time` | `update_busy_hyst_end_time()` [sched_avg.c:240](../../kernel/kernel/sched/walt/sched_avg.c#L240) | 三组 hysteresis 命中时的实际取值（`hyst_time` / `coloc_hyst_time` / `util_hyst_time` 分开打出），调 §1.9 时用来确认哪个条件触发 |

### 2.3 任务 ravg（`sum` / `demand` / `pred_demand`）

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `sched_update_task_ravg` | `walt_update_task_ravg()` [walt.c:2317](../../kernel/kernel/sched/walt/walt.c#L2317) | **重量级全量事件**：`crs/prs/nt_crs/nt_prs`、`curr_window`/`prev_window` 及 per-CPU 展开、`grp_*`、`curr_top`/`prev_top`、`active_time`。排查记账不平、group 时间丢失的首选 |
| `sched_update_task_ravg_mini` | `walt_update_task_ravg()` [walt.c:2319](../../kernel/kernel/sched/walt/walt.c#L2319) | 上者的**瘦身版**，字段少一半（无 per-CPU 数组、无 `coloc_demand`、无 `active_time`）。**两者在同一条函数里背靠背触发**，是给低开销长期采样用的；只需要趋势时开这个 |
| `sched_update_history` | `update_history()` [walt.c:2062](../../kernel/kernel/sched/walt/walt.c#L2062) | 窗口滚动时任务的 5 项 `sum_history` + `sum_history_util` + `demand`/`coloc_demand`/`pred_demand_scaled` + `nr_big_tasks`。**验证 demand 取 MAX 语义的唯一直接证据** |
| `sched_update_pred_demand` | `get_pred_busy()` [walt.c:1311](../../kernel/kernel/sched/walt/walt.c#L1311) | 打入 16 个 `busy_buckets` 快照 + `start`/`first`/`final` 遍历位置 + 预测值。调 [demand-prediction.md](02-demand-prediction.md) 的核心工具 |
| `sched_get_task_cpu_cycles` | `update_task_rq_cpu_cycles()` [walt.c:2264](../../kernel/kernel/sched/walt/walt.c#L2264) | `cycles`/`exec_time`/算出的 `freq`/`legacy_freq`/`max_freq`。**频率归一化的现场**——`task_exec_scale` 算错时在这里看 |
| `sched_migration_update_sum` | `transfer_busy_time()` [walt.c:3519](../../kernel/kernel/sched/walt/walt.c#L3519) | 任务出入 RTG 或跨 rq 迁移时的 `src/dst` `cs/ps/nt_cs/nt_ps` 八个量。**迁移后计数不守恒就从这里查** |

> `sched_update_task_ravg` 与 `_mini` 同时触发，**两个都开会双倍开销**。
> 生产抓取建议只开 `_mini`，问题复现后再补开全量。`[推测]`

### 2.4 放置（placement）

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `sched_task_util` | `walt_find_energy_efficient_cpu()` [walt_cfs.c:1137](../../kernel/kernel/sched/walt/walt_cfs.c#L1137) | **放置的总出口**。一行给出 `util`/`prev_cpu`/`candidates`/`best_energy_cpu`/`sync`/`need_idle`/`fastpath`/`placement_boost`/`latency`/`is_rtg`/`rtg_skip_min`/`unfilter`/`affinity`/`task_boost`/`low_latency`/`load_boost`/`pipeline_cpu` |
| `sched_find_best_target` | `walt_find_best_target()` [walt_cfs.c:644](../../kernel/kernel/sched/walt/walt_cfs.c#L644) | FBT 的决策过程：`order_index`/`end_index`/`skip`/`most_spare_cap`/`least_nr_cpu` |
| `sched_compute_energy` | `walt_find_energy_efficient_cpu()` [walt_cfs.c:1086](../../kernel/kernel/sched/walt/walt_cfs.c#L1086) 与 [walt_cfs.c:1116](../../kernel/kernel/sched/walt/walt_cfs.c#L1116) | per-cluster `sum_util`/`max_util`/`cost` + `prev_energy`/`eval_energy`/`best_energy`。**EAS 决策为什么选这个 CPU** |
| `sched_cpu_util` | `walt_find_best_target()` [walt_cfs.c:473](../../kernel/kernel/sched/walt/walt_cfs.c#L473)（`lowest_mask=NULL`）与 `walt_rt_energy_aware_wake_cpu()` [walt_rt.c:120](../../kernel/kernel/sched/walt/walt_rt.c#L120) | 单 CPU 的完整状态快照：`cpu_util`/`cpu_util_cum`/`capacity*`/`irqload`/`halted`/`reserved`/`high_irq_load`/`nr_rtg_hp`/`prs_gprs`/`lowest_mask`/`thermal_pressure`。**一票难求的「这个 CPU 当时什么样」** |
| `sched_select_task_rt` | `walt_select_task_rq_rt()` [walt_rt.c:335](../../kernel/kernel/sched/walt/walt_rt.c#L335) | RT 放置出口，`fastpath` 标出是否走了快路径 |
| `sched_enq_deq_task` | `android_rvh_enqueue_task()` [walt.c:4669](../../kernel/kernel/sched/walt/walt.c#L4669) 与 `android_rvh_dequeue_task()` [walt.c:4719](../../kernel/kernel/sched/walt/walt.c#L4719) | enqueue/dequeue 全量流，含 `prio`/`demand`/`pred_demand_scaled`/`affine`/`mvp`/`is_compat_t`。**时序类问题的骨架** |
| `sched_set_preferred_cluster` | `_set_preferred_cluster()` [walt.c:2969](../../kernel/kernel/sched/walt/walt.c#L2969) | RTG 的 `skip_min` 翻转时刻 + `total_demand`/`downmigrate_ts`/`start_ktime_ts`，并顺带打出当时的 `sysctl_sched_hyst_min_coloc_ns` |
| `sched_overutilized` | `sched_overutilized()` callback [walt_tp.c:114](../../kernel/kernel/sched/walt/walt_tp.c#L114) | **需要 `sched_enable_tp=1` 才会注册** [walt_tp.c:120](../../kernel/kernel/sched/walt/walt_tp.c#L120)。输出 overutilized 布尔 + CPU span 位图 |

`sched_task_util` 的调用点被 `if (trace_sched_task_util_enabled())` 包住
[walt_cfs.c:999](../../kernel/kernel/sched/walt/walt_cfs.c#L999)，
`sched_compute_energy` 同样 [walt_cfs.c:1076](../../kernel/kernel/sched/walt/walt_cfs.c#L1076)、
[walt_cfs.c:1096](../../kernel/kernel/sched/walt/walt_cfs.c#L1096)。

### 2.5 调频（cpufreq）

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `waltgov_next_freq` | `get_next_freq()` [cpufreq_walt.c:258](../../kernel/kernel/sched/walt/cpufreq_walt.c#L258)（其中会调 `get_adaptive_high_freq()` [cpufreq_walt.c:231](../../kernel/kernel/sched/walt/cpufreq_walt.c#L231)） | `raw_freq` → `freq` 的最终决定，含 `policy_min/max`、`cached_raw_freq`、`need_update`、`rt_util`、`driving_cpu`、`reason` 位掩码 |
| `waltgov_util_update` | `waltgov_update_freq()` [cpufreq_walt.c:432](../../kernel/kernel/sched/walt/cpufreq_walt.c#L432)（`waltgov_next_freq_shared()` [cpufreq_walt.c:364](../../kernel/kernel/sched/walt/cpufreq_walt.c#L364) 的调用者） | 调频看到的三条 util 输入：`util` / `avg_cap` / `max_cap` / `nl` / `pl` / `rtgb` / `flags` |
| `sched_load_to_gov` | `freq_policy_load()` [walt.c:629](../../kernel/kernel/sched/walt/walt.c#L629) | **WALT → governor 的交接点**。`aggr_grp_load`/`tt_load`/`freq_aggr`/`rq_ps`/`grp_rq_ps`/`nt_ps`/`grp_nt_ps`/`pl`/`big_task_rotation`/`user_hint`/**`reasons` 位掩码** |
| `sched_set_boost` | `_sched_set_boost()` [boost.c:256](../../kernel/kernel/sched/walt/boost.c#L256) | boost 生效值变化（已聚合，非用户请求值） |
| `update_cpu_capacity` | `update_cpu_capacity_helper()` [walt.c:4190](../../kernel/kernel/sched/walt/walt.c#L4190) | `capacity_orig` 被热压制改动时打点，含 `arch_capacity` / `thermal_cap` / `max_freq` / `max_possible_freq` |

`sched_load_to_gov` 的 `reasons` 位掩码定义见 [walt.c:590-627](../../kernel/kernel/sched/walt/walt.c#L590-L627)，
是调试「为什么加了频」最直接的一手信息。

### 2.6 core_ctl

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `core_ctl_eval_need` | `eval_need()` [core_ctl.c:954](../../kernel/kernel/sched/walt/core_ctl.c#L954) | 每簇的在线数决策：`last_need`/`new_need`/`active_cpus`/`adj_now`/`adj_possible`/`updated` |
| `core_ctl_eval_need_32bit` | `eval_need_32bit()` [core_ctl.c:1016](../../kernel/kernel/sched/walt/core_ctl.c#L1016) | 32-bit 任务专用的同一套决策（字段完全相同） |
| `core_ctl_set_busy` | `eval_need()` [core_ctl.c:918](../../kernel/kernel/sched/walt/core_ctl.c#L918) | 单 CPU 的 busy×3 状态机翻转：`busy`/`old_is_busy`/`is_busy` + `high_irqload` |
| `core_ctl_update_nr_need` | `update_running_avg()` [core_ctl.c:782](../../kernel/kernel/sched/walt/core_ctl.c#L782) | `nr_need`/`prev_misfit_need`/`nrrun`/`max_nr`/`nr_prev_assist` |
| `core_ctl_set_boost` | `core_ctl_set_boost()` [core_ctl.c:1079](../../kernel/kernel/sched/walt/core_ctl.c#L1079) | boost refcount 与返回值；确认 FULL_THROTTLE_BOOST 是否真的锁住了 core_ctl |
| `core_ctl_notif_data` | `core_ctl_call_notifier()` [core_ctl.c:1115](../../kernel/kernel/sched/walt/core_ctl.c#L1115) | 通知给外部模块的数据：`nr_big`/`ta_load`/`ta_util[3]`/`cur_cap[3]` |

### 2.7 负载均衡（LB）

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `walt_active_load_balance` | `walt_lb_pull_tasks()` [walt_lb.c:398](../../kernel/kernel/sched/walt/walt_lb.c#L398) 与 `walt_lb_tick()` [walt_lb.c:702](../../kernel/kernel/sched/walt/walt_lb.c#L702) | 主动迁移的 (pid, misfit, src→dst)。`misfit` 是判断是否该迁的关键 |
| `walt_find_busiest_queue` | `walt_find_busiest_queue()` [walt_lb.c:1063](../../kernel/kernel/sched/walt/walt_lb.c#L1063) | 选出的 busiest CPU 与 `src_mask` |
| `walt_nohz_balance_kick` | `walt_nohz_balancer_kick()` [walt_lb.c:1087](../../kernel/kernel/sched/walt/walt_lb.c#L1087) | nohz idle balance 被踢起的 rq 状态 |
| `walt_newidle_balance` | `walt_newidle_balance()` [walt_lb.c:991](../../kernel/kernel/sched/walt/walt_lb.c#L991) | newidle 的完整现场：`busy_cpu`/`pulled`/`nr_running`/`rt_nr_running`/`nr_iowait`/`help_min_cap`/`avg_idle`/`enough_idle`/`overload` |
| `walt_lb_cpu_util` | 三个 busiest 选择函数各一处：[walt_lb.c:473](../../kernel/kernel/sched/walt/walt_lb.c#L473)（`walt_lb_find_busiest_similar_cap_cpu`）、[walt_lb.c:505](../../kernel/kernel/sched/walt/walt_lb.c#L505)（`walt_lb_find_busiest_from_higher_cap_cpu`）、[walt_lb.c:574](../../kernel/kernel/sched/walt/walt_lb.c#L574)（`walt_lb_find_busiest_from_lower_cap_cpu`） | 每个候选 CPU 的 `nr_running`/`cfs_nr_running`/`nr_big`/`nr_rtg_hp`/`cpu_util`/`capacity_orig`。**注意一次迁移可能连打多条** |

### 2.8 RT 与 MVP

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `sched_select_task_rt` | `walt_select_task_rq_rt()` [walt_rt.c:335](../../kernel/kernel/sched/walt/walt_rt.c#L335) | 见 §2.4 |
| `walt_cfs_deactivate_mvp_task` | `walt_cfs_account_mvp_runtime()` [walt_cfs.c:1338](../../kernel/kernel/sched/walt/walt_cfs.c#L1338) | MVP（最高优先级 CFS 任务）退出时 `exec > limit` 的违规证据 |
| `walt_cfs_mvp_pick_next` | `walt_cfs_replace_next_task_fair()` [walt_cfs.c:1543](../../kernel/kernel/sched/walt/walt_cfs.c#L1543) | MVP 重新被选中 |
| `walt_cfs_mvp_wakeup_preempt` | `walt_cfs_check_preempt_wakeup()` [walt_cfs.c:1479](../../kernel/kernel/sched/walt/walt_cfs.c#L1479) | 唤醒任务抢占当前 MVP |
| `walt_cfs_mvp_wakeup_nopreempt` | `walt_cfs_check_preempt_wakeup()` [walt_cfs.c:1475](../../kernel/kernel/sched/walt/walt_cfs.c#L1475) | 唤醒任务**未能**抢占当前 MVP（MVP 保护生效） |

注意后两者与 `walt_cfs_mvp_pick_next` **出自不同的 hook**：
`walt_cfs_check_preempt_wakeup`（`android_rvh_check_preempt_wakeup`）
与 `walt_cfs_replace_next_task_fair`（`android_rvh_replace_next_task_fair`），
分别注册于 [walt_cfs.c:1555-1556](../../kernel/kernel/sched/walt/walt_cfs.c#L1555-L1556)。
`walt_cfs_tick()` [walt_cfs.c:1399](../../kernel/kernel/sched/walt/walt_cfs.c#L1399) 本身不发 MVP 事件。

这四个由同一个 `DECLARE_EVENT_CLASS(walt_cfs_mvp_task_template, ...)`
[trace.h:1315](../../kernel/kernel/sched/walt/trace.h#L1315) 派生，字段一致：
`comm`/`pid`/`prio`/`mvp_prio`/`cpu`/`exec`/`limit`。

### 2.9 停机 / 热插拔 / 分组

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `halt_cpus_start` | `halt_cpus()` [walt_halt.c:284](../../kernel/kernel/sched/walt/walt_halt.c#L284) 与 `start_cpus()` [walt_halt.c:329](../../kernel/kernel/sched/walt/walt_halt.c#L329) | 停机请求发起时：`req_cpus` / `halt_cpus` / `halt` |
| `halt_cpus` | `halt_cpus()` [walt_halt.c:314](../../kernel/kernel/sched/walt/walt_halt.c#L314) 与 `start_cpus()` [walt_halt.c:346](../../kernel/kernel/sched/walt/walt_halt.c#L346) | 停机**完成**：额外带 `time`（µs）与 `success`。`halt_cpus_start` ↔ `halt_cpus` 的时间差 = 停机延迟 |
| `sched_cgroup_attach` | `android_rvh_cpu_cgroup_attach()` [walt.c:3310](../../kernel/kernel/sched/walt/walt.c#L3310)（不是 `_online()`；后者在同文件 [walt.c:3279](../../kernel/kernel/sched/walt/walt.c#L3279)） | 任务进出 cgroup 时尝试加入 RTG 的结果 `grp_id`/`ret` |

### 2.10 审计

| tracepoint | 触发点 | 观察用途 |
|---|---|---|
| `sched_task_handler` | `sched_task_handler()` [sysctl.c:373](../../kernel/kernel/sched/walt/sysctl.c#L373) | **每一次** per-task sysctl 写入（§1.12），带调用进程的 6 层调用栈（`CALLER_ADDR0..5`）。追踪「谁把某进程设成了 low_latency / 加了 load_boost」 |

`param` 字段就是 §1.12 的参数枚举值（1..8）。

### 2.11 其它（perf 计数器 / preemptirq）

不属于 `schedwalt`，但在同一目录里：

| tracepoint | 定义 | 触发点 | 用途 |
|---|---|---|---|
| `sched_switch_with_ctrs` | [perf_trace_counters.h:62](../../kernel/kernel/sched/walt/perf_trace_counters.h#L62) | `tracectr_notifier()` [walt_tp.c:74](../../kernel/kernel/sched/walt/walt_tp.c#L74) | 每次上下文切换带上 PMU 周期/事件计数增量 + AMU（CYC/INST）。**需要 `sched_enable_tp=1`** 且 **每秒才刷一次 `sched_switch_ctrs_cfg`** [walt_tp.c:76](../../kernel/kernel/sched/walt/walt_tp.c#L76) |
| `sched_switch_ctrs_cfg` | [perf_trace_counters.h:174](../../kernel/kernel/sched/walt/perf_trace_counters.h#L174) | `tracectr_notifier()` [walt_tp.c:77](../../kernel/kernel/sched/walt/walt_tp.c#L77) | 声明 `CTR0..CTR5` 当前配置的 event type，否则 `with_ctrs` 的列无法解释 |
| `irq_disable_long` | [preemptirq_long.h:46](../../kernel/kernel/sched/walt/preemptirq_long.h#L46) | `test_irq_disable_long()` [preemptirq_long.c:65](../../kernel/kernel/sched/walt/preemptirq_long.c#L65) | 关中断超 `irqsoff_tracing_threshold_ns` 时打出 `delta`(ns) + 4 层栈 |
| `preempt_disable_long` | [preemptirq_long.h:51](../../kernel/kernel/sched/walt/preemptirq_long.h#L51) | `test_preempt_disable_long()` [preemptirq_long.c:117](../../kernel/kernel/sched/walt/preemptirq_long.c#L117) | 关抢占超 `preemptoff_tracing_threshold_ns` 时同上（带 pid/ncsw 过滤防误报） |

---

## 3. 观测方法

### 3.1 打开 tracepoint

**路径勘误 [反直觉]**：常见写法是 `/sys/kernel/debug/tracing/events/sched/`，
但 WALT 的 `TRACE_SYSTEM` 是 `schedwalt`，所以要用：

```bash
# 二选一，取决于内核挂载方式
TR=/sys/kernel/debug/tracing      # tracefs 挂在 debugfs 下
TR=/sys/kernel/tracing            # tracefs 独立挂载

ls $TR/events/schedwalt/          # 全部 WALT 事件在这里
ls $TR/events/perf_trace_counters/
ls $TR/events/preemptirq_long/
```

只开某几个事件：

```bash
echo 0 > $TR/tracing_on
echo > $TR/trace
echo 1 > $TR/events/schedwalt/walt_window_rollover/enable
echo 1 > $TR/events/schedwalt/sched_update_task_ravg_mini/enable
echo 1 > $TR/tracing_on
# ... 复现问题 ...
echo 0 > $TR/tracing_on
cat $TR/trace
```

按字段过滤：

```bash
echo 'pid == 1234' > $TR/events/schedwalt/sched_task_util/filter
echo 'comm == "surfaceflinger"' > $TR/events/schedwalt/sched_enq_deq_task/filter
echo 'is_rtg == 1' > $TR/events/schedwalt/sched_task_util/filter
echo 'misfit == 1' > $TR/events/schedwalt/walt_active_load_balance/filter
```

> **filter 的键名是 `TP_STRUCT__entry` 的字段名**（即 `__entry->` 后面的标识符），
> 与 `TP_printk` 的打印标签**可能不一致 [反直觉]**。核对方法：
> 打开 `$TR/events/schedwalt/<event>/format`，`field:` 后面那一串才是合法键名。
> `sched_task_util` 一个事件里就有**两处**不一致
> [trace.h:1177](../../kernel/kernel/sched/walt/trace.h#L1177)：
> 字段 `uclamp_boosted` 打印成 `stune_boosted`，字段 `cpus_allowed`
> 打印成 `affinity`。写成 `stune_boosted` / `affinity` 都会 `-EINVAL`。
> 上例中 `pid` / `comm` / `is_rtg` / `misfit` 四处已核对为合法字段名。

打开动态事件（`sched_overutilized` 与 PMU 计数）：

```bash
echo 1 > /proc/sys/walt/sched_enable_tp
```

### 3.2 ftrace 一行流

```bash
# 1) 窗口节拍是否规律（rollover 间隔应≈窗口长度）
TR=/sys/kernel/tracing
echo 1 > $TR/events/schedwalt/walt_window_rollover/enable
cat $TR/trace_pipe | head -50

# 2) 看某个任务的需求预测桶分布
echo 1 > $TR/events/schedwalt/sched_update_pred_demand/enable
echo 'pid == 1234' > $TR/events/schedwalt/sched_update_pred_demand/filter

# 3) 找「为什么这个任务跑到了小核」——放置出口一行搞定
echo 1 > $TR/events/schedwalt/sched_task_util/enable
echo 'pid == 1234' > $TR/events/schedwalt/sched_task_util/filter

# 4) 调频抖动
echo 1 > $TR/events/schedwalt/waltgov_next_freq/enable
echo 1 > $TR/events/schedwalt/waltgov_util_update/enable

# 5) 用 trace-cmd 抓成可离线分析的格式
trace-cmd record -e schedwalt -e perf_trace_counters -o walt.dat sleep 10
trace-cmd report walt.dat | head
```

`trace-cmd record -e schedwalt` 会展开成该子系统下所有事件——**注意
`sched_update_task_ravg` 全量版开销很大**，长期采样请改用逐个 `-e` 指定。

### 3.3 WALT 调试转储

三个 printk 级别的转储函数，全部走 `printk_deferred()`（避免在持 rq lock 时死锁）：

| 函数 | 位置 | 输出 |
|---|---|---|
| `walt_task_dump(p)` | [walt.c:192](../../kernel/kernel/sched/walt/walt.c#L192) | 单任务全字段：`state`/`cpu`/`policy`/`prio`/`mark_start`/`demand`/`coloc_demand`/`prev_cpu`/`new_cpu`/`misfit`/`prev_on_rq`/`mvp_prio`/`iowaited`/`curr_window`+per-CPU 展开/`prev_window`+展开/`last_sleep_ts`/`last_wake_ts`/`unfilter`/`grp`/`on_rq` |
| `walt_rq_dump(cpu)` | [walt.c:243](../../kernel/kernel/sched/walt/walt.c#L243) | 单个 CPU：`nr_running` + 当前任务，`latest_clock`/`window_start`/`prev_window_size`/`curr_runnable_sum`/`prev_runnable_sum`/`nt_*`/`task_exec_scale`/`grp_time.*`，`load_subs[NUM_TRACKED_WINDOWS=2]` 的 `window_start`/`subs`/`new_subs`，然后调 `walt_task_dump(当前任务)`，最后 `sched_capacity_margin_up/down[cpu]` |
| `walt_dump()` | [walt.c:288](../../kernel/kernel/sched/walt/walt.c#L288) | **全机器**：打印 `Sched clock` / `Time last window changed` / `global_ws`，然后对每个在线 CPU 调 `walt_rq_dump()`，再打 `max_possible_cluster_id` |

`walt_dump()` 用下面这对界标包起来，dmesg 里直接 grep 即可：

```
============ WALT RQ DUMP START ==============
============ WALT RQ DUMP END ==============
```

```bash
dmesg | sed -n '/WALT RQ DUMP START/,/WALT RQ DUMP END/p'
```

**`walt_dump()` 只有两个调用者**：`WALT_PANIC` 宏 [walt.h:1165](../../kernel/kernel/sched/walt/walt.h#L1165)
（它本身只在 `WALT_BUG` 里被调 [walt.h:1207](../../kernel/kernel/sched/walt/walt.h#L1207)）。
也就是说 **`WALT RQ DUMP` 只在致命断言触发时才出现**，不是一个可以按需触发的调试开关。
要主动触发，只能把对应 feature 的 panic 位置 1 然后复现（§3.5）。

### 3.4 `in_sched_bug`

定义在 [walt.c:304](../../kernel/kernel/sched/walt/walt.c#L304)，声明 [walt.h:1142](../../kernel/kernel/sched/walt/walt.h#L1142)。

它是一个**单比特重入锁**，用在 `WALT_PANIC` 里：

```c
#define WALT_PANIC(condition)				\
({							\
	if (unlikely(!!(condition)) && !in_sched_bug) {	\
		in_sched_bug = 1;			\
		walt_dump();				\
		BUG_ON(condition);			\
	}						\
})
```

作用与要点：

- `walt_dump()` 本身会在遍历 CPU 时读大量字段，**很可能再次触发断言**。
  `in_sched_bug` 保证第二次进入时直接跳过，避免无限递归/栈溢出。
- **它只置 1，从不复位**——一旦进过一次就永久为 1。所以 dmesg 里若出现
  `WALT RQ DUMP` 后面紧跟 `BUG`，这是设计行为，不是死循环。
- `in_sched_bug` 为 1 之后，**所有后续 `WALT_BUG` 都静默失效**（print 也会被跳过吗？
  不会——`WALT_BUG` 的 print 分支不检查 `in_sched_bug`，只有 `WALT_PANIC` 检查）。
  `[推测]` 因此 panic 之后仍可能继续看到 `WALT-BUG ...` 打印。
- 类型是 `int` 而非 `atomic` / `bool`：**无锁、非原子**，只是「够用」的近似。`[反直觉]`

### 3.5 `panic_on_walt_bug` 位掩码

这是 WALT 观测里信息密度最高的一个旋钮，但**位布局不是直读的**。

`enum WALT_DEBUG_FEAT` [walt.h:1151-1159](../../kernel/kernel/sched/walt/walt.h#L1151-L1159)：

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | `WALT_BUG_UPSTREAM` | 上游（非 WALT）不变量被破坏，通常是 WALT 与 CFS 主干的契约 |
| 1 | `WALT_BUG_WALT` | WALT 自身记账不变量被破坏 |
| 2 | `WALT_BUG_NONCRITICAL` | 可容忍的偏差（默认只打印不 panic） |
| 3 | `WALT_BUG_UNUSED` | 占位 |
| — | `WALT_DEBUG_FEAT_NR` = **4** | 低 4 bit = panic 位图，高 4 bit = print 位图 |

位布局 [walt.h:1173-1174](../../kernel/kernel/sched/walt/walt.h#L1173-L1174)：

| bit | 作用 |
|---|---|
| `0..3` | panic 位图：`walt_debug_bitmask_panic(x) = 1 << x` |
| `4..7` | print 位图：`walt_debug_bitmask_print(x) = 1 << (x + 4)` |
| `8..31` | **哨兵**：必须等于 `WALT_PANIC_SENTINEL` 的高 24 bit |

**默认值**（`walt_debug_initial_values()` [walt.h:1177-1182](../../kernel/kernel/sched/walt/walt.h#L1177-L1182)）：

```
WALT_PANIC_SENTINEL (0x4544DE00)
  | panic(UPSTREAM=0)  = 1 << 0  = 0x00000001
  | print(UPSTREAM=0)  = 1 << 4  = 0x00000010
  | panic(WALT=1)      = 1 << 1  = 0x00000002
  | print(WALT=1)      = 1 << 5  = 0x00000020
  ─────────────────────────────────────────
  0x4544DE33
```

即**默认：UPSTREAM 与 WALT 两类都打印 + panic；NONCRITICAL 不打印不 panic**。

关键机制（`is_walt_sentinel()` [walt.h:1191-1196](../../kernel/kernel/sched/walt/walt.h#L1191-L1196)）：

```c
if (unlikely((sysctl_panic_on_walt_bug & 0xFFFFFF00) == WALT_PANIC_SENTINEL))
	return true;
```

**高 24 bit 必须严格等于 `0x4544DE`，否则整套 WALT_BUG 静默失效 [反直觉]。**
所以：

- 想全部关闭：写入 `0`（不是 `0x4544DE00`）——哨兵不匹配 → 什么都不做。
- 想开 NONCRITICAL 的打印：`0x4544DE00 | (1<<6)` = `0x4544DE40`。
- **绝对不能**在已有值上做 `|=`。任何把高 24 bit 改掉的写操作会让断言整体失效，
  而且**不会有任何提示**。

```bash
# 默认
cat /proc/sys/walt/panic_on_walt_bug            # 1162141235 = 0x4544DE33

# 全关（排查性能问题时先关掉 panic）
echo 0 > /proc/sys/walt/panic_on_walt_bug

# 只打印不 panic（NONCRITICAL 也打）
echo $((0x4544DE00 | 1<<0 | 1<<4 | 1<<1 | 1<<5 | 1<<2 | 1<<6)) > /proc/sys/walt/panic_on_walt_bug
```

> **范围约束**：该节点的 `extra1/extra2 = SYSCTL_ZERO / SYSCTL_INT_MAX`
> （`panic_on_walt_bug` 表项 [sysctl.c:889](../../kernel/kernel/sched/walt/sysctl.c#L889)，
> 下界/上界 [sysctl.c:894-895](../../kernel/kernel/sched/walt/sysctl.c#L894-L895)），
> 而 `SYSCTL_INT_MAX` 就是 `INT_MAX` (2147483647)。默认值
> `0x4544DE33` = **1162141235**，小于 `INT_MAX`，因此读写正常。
> `[待确认]` 但变量类型与 handler 的 `int` 语义不一致（`proc_dointvec_minmax`
> 按有符号 `int` 解析并回显，变量本身当作 `unsigned int` 用位运算）——
> 只要哨兵位（bit 31）恒为 0 就不会出问题，但这是一个 typo 易发区。

配套的搜索关键词：dmesg 里 grep `WALT-BUG`（打印前缀，[walt.h:1203](../../kernel/kernel/sched/walt/walt.h#L1203)）。

`WALT_BUG` 的触发点一览（`grep -n "WALT_BUG(" *.c`，共 **36** 处：
`walt.c` 33 + `walt_cfs.c` 2 + `walt_halt.c` 1）：

| 类别 | 位置 |
|---|---|
| cpu cycle / 记账 | [walt.c:322](../../kernel/kernel/sched/walt/walt.c#L322)、[walt.c:326](../../kernel/kernel/sched/walt/walt.c#L326)、[walt.c:334](../../kernel/kernel/sched/walt/walt.c#L334)、[walt.c:1058](../../kernel/kernel/sched/walt/walt.c#L1058)、[walt.c:1129](../../kernel/kernel/sched/walt/walt.c#L1129) |
| rollover 负值 | [walt.c:739](../../kernel/kernel/sched/walt/walt.c#L739)、[walt.c:744](../../kernel/kernel/sched/walt/walt.c#L744)、[walt.c:749](../../kernel/kernel/sched/walt/walt.c#L749)、[walt.c:754](../../kernel/kernel/sched/walt/walt.c#L754) |
| 任务状态机 | [walt.c:855](../../kernel/kernel/sched/walt/walt.c#L855)、[walt.c:865](../../kernel/kernel/sched/walt/walt.c#L865)、[walt.c:876](../../kernel/kernel/sched/walt/walt.c#L876)、[walt.c:887](../../kernel/kernel/sched/walt/walt.c#L887) |
| 迁移 | [walt.c:1044](../../kernel/kernel/sched/walt/walt.c#L1044)、[walt.c:1731](../../kernel/kernel/sched/walt/walt.c#L1731)、[walt.c:2325](../../kernel/kernel/sched/walt/walt.c#L2325) |
| `transfer_busy_time()` [walt.c:3344](../../kernel/kernel/sched/walt/walt.c#L3344) 内的守恒检查（**最密集的一处**） | [walt.c:3366](../../kernel/kernel/sched/walt/walt.c#L3366)、[walt.c:3394](../../kernel/kernel/sched/walt/walt.c#L3394)、[walt.c:3403](../../kernel/kernel/sched/walt/walt.c#L3403)、[walt.c:3413](../../kernel/kernel/sched/walt/walt.c#L3413)、[walt.c:3424](../../kernel/kernel/sched/walt/walt.c#L3424)、[walt.c:3452](../../kernel/kernel/sched/walt/walt.c#L3452)、[walt.c:3461](../../kernel/kernel/sched/walt/walt.c#L3461)、[walt.c:3471](../../kernel/kernel/sched/walt/walt.c#L3471)、[walt.c:3481](../../kernel/kernel/sched/walt/walt.c#L3481) |
| 亲和性 / 入队出队 | [walt.c:4540](../../kernel/kernel/sched/walt/walt.c#L4540)、[walt.c:4546](../../kernel/kernel/sched/walt/walt.c#L4546)、[walt.c:4628](../../kernel/kernel/sched/walt/walt.c#L4628)、[walt.c:4633](../../kernel/kernel/sched/walt/walt.c#L4633)、[walt.c:4639](../../kernel/kernel/sched/walt/walt.c#L4639)、[walt.c:4691](../../kernel/kernel/sched/walt/walt.c#L4691)、[walt.c:4699](../../kernel/kernel/sched/walt/walt.c#L4699) |
| 其它 | [walt.c:5122](../../kernel/kernel/sched/walt/walt.c#L5122)、[walt_halt.c:198](../../kernel/kernel/sched/walt/walt_halt.c#L198)、[walt_cfs.c:1507](../../kernel/kernel/sched/walt/walt_cfs.c#L1507)、[walt_cfs.c:1538](../../kernel/kernel/sched/walt/walt_cfs.c#L1538) |
| lockdep 辅助（宏，非调用点） | `walt_lockdep_assert()` [walt.h:1212](../../kernel/kernel/sched/walt/walt.h#L1212) |

按第一个实参（`WALT_BUG_*` 类别）统计：`WALT_BUG_WALT` **27** 处、
`WALT_BUG_UPSTREAM` **8** 处、`WALT_BUG_NONCRITICAL` **1** 处
（[walt.c:4639](../../kernel/kernel/sched/walt/walt.c#L4639)，`android_rvh_enqueue_task()`
里的 "Non Kthread Started on halted cpu"）、`WALT_BUG_UNUSED` **0** 处（枚举里有定义但无使用）。
校验：`grep -o "WALT_BUG(WALT_BUG_WALT" *.c | wc -l`。27 + 8 + 1 = 36 ✓

> 这直接对应 §3.5 的默认掩码：默认只 panic+print `UPSTREAM`(0) 与 `WALT`(1)，
> `NONCRITICAL`(2) 那**唯一一处**默认既不打印也不 panic——即「非 kthread 被 enqueue
> 到已 halt 的 CPU」在默认配置下完全静默，要观测必须显式置 print(2) 位
> （`0x4544DE00 | (1<<6)`）。

`WALT_PANIC()` 的直接调用点（共 8 处，全部在 `walt.c`）：

| 位置 | 所在函数 | 触发条件（`WALT_PANIC()` 之前那个 `if`） |
|---|---|---|
| [walt.c:419](../../kernel/kernel/sched/walt/walt.c#L419) | `update_window_start()` [walt.c:406](../../kernel/kernel/sched/walt/walt.c#L406) | `wallclock < wrq->latest_clock`（sched clock 回退） |
| [walt.c:426](../../kernel/kernel/sched/walt/walt.c#L426) | `update_window_start()` [walt.c:406](../../kernel/kernel/sched/walt/walt.c#L406) | `delta < 0`，即 `wallclock < wrq->window_start` |
| [walt.c:1889](../../kernel/kernel/sched/walt/walt.c#L1889) | `update_cpu_busy_time()` [walt.c:1678](../../kernel/kernel/sched/walt/walt.c#L1678) | `!is_idle_task(p)`（IRQ busy time 只能记到 idle 任务上） |
| [walt.c:2256](../../kernel/kernel/sched/walt/walt.c#L2256) | `update_task_rq_cpu_cycles()` [walt.c:2202](../../kernel/kernel/sched/walt/walt.c#L2202) | `(s64)time_delta < 0`（cpu cycle 时间倒流） |
| [walt.c:2629](../../kernel/kernel/sched/walt/walt.c#L2629)、[walt.c:2635](../../kernel/kernel/sched/walt/walt.c#L2635) | `init_cpu_array()` [walt.c:2622](../../kernel/kernel/sched/walt/walt.c#L2622) | `kcalloc()` 返回 NULL（`__GFP_NOFAIL` 兜底） |
| [walt.c:2644](../../kernel/kernel/sched/walt/walt.c#L2644) | `build_cpu_array()` [walt.c:2639](../../kernel/kernel/sched/walt/walt.c#L2639) | `!cpu_array`（拓扑初始化顺序错） |
| [walt.c:2759](../../kernel/kernel/sched/walt/walt.c#L2759) | `walt_update_cluster_topology()` [walt.c:2726](../../kernel/kernel/sched/walt/walt.c#L2726) | `!policy`（CPU policy 尚未初始化就被调用，注释明说 "simply BUG()"） |

**注意这 8 处的 `condition` 多是字面 `1`**（上面 419/426/2629/2635/2644 都是
`WALT_PANIC(1)`），真正判断在**外层 `if`** 里。所以 `BUG_ON(condition)`
展开后是 `BUG_ON(1)`——**无条件 panic**，dmesg 里的 `WALT-BUG ...` 那行
printk 才是唯一的现场信息。

### 3.6 其它入口点

| 入口 | 位置 | 说明 |
|---|---|---|
| `/proc/sys/walt/*` | `register_sysctl_table()` [walt.c:5126](../../kernel/kernel/sched/walt/walt.c#L5126) | §1 全部旋钮 |
| `/proc/sys/preemptirq/*` | `register_sysctl()` [preemptirq_long.c:165](../../kernel/kernel/sched/walt/preemptirq_long.c#L165) | §1.15c |
| `/sys/devices/system/cpu/cpufreq/policyN/*` | `waltgov_attributes[]` [cpufreq_walt.c:834](../../kernel/kernel/sched/walt/cpufreq_walt.c#L834) | §1.15a |
| `/sys/devices/system/cpu/cpuN/core_ctl/*` | `kobject_add()` [core_ctl.c:1521](../../kernel/kernel/sched/walt/core_ctl.c#L1521) | §1.15b |
| `android_rvh_schedule_bug` | `walt_debug_init()` [walt_debug.c:19](../../kernel/kernel/sched/walt/walt_debug.c#L19) | 注册后任何 `schedule()` 走 bug 路径即 `BUG()`；**仅 `CONFIG_SCHED_WALT_DEBUG`** |
| preemptirq 跳线 | `walt_debug_init()` → `preemptirq_long_init()` [walt_debug.c:23](../../kernel/kernel/sched/walt/walt_debug.c#L23) | 注册 `android_rvh_irqs_disable/enable`、`preempt_disable/enable` 四个 hook |

**本目录没有 debugfs 接口**（`grep -rn debugfs *.c *.h` 无结果）。
所有「调试开关」都走 sysctl / sysfs / tracepoint 三条路。

### 3.7 一条完整的排查流水线

```bash
# 0) 关掉会 panic 的断言，避免边抓边崩
echo 0 > /proc/sys/walt/panic_on_walt_bug

# 1) 开动态事件（overutilized + PMU 计数）
echo 1 > /proc/sys/walt/sched_enable_tp

# 2) 只开低开销事件
TR=/sys/kernel/tracing
echo 0 > $TR/tracing_on
echo > $TR/trace
for e in walt_window_rollover sched_update_task_ravg_mini sched_task_util \
         waltgov_next_freq sched_busy_hyst_time sched_enq_deq_task; do
  echo 1 > $TR/events/schedwalt/$e/enable
done
echo 1 > $TR/tracing_on
sleep 10
echo 0 > $TR/tracing_on

# 3) 定向分析
grep -c walt_window_rollover $TR/trace        # 节拍数，和窗口长度对账
grep sched_task_util $TR/trace | awk '{print $NF}' | sort | uniq -c | sort -rn
cat $TR/trace > /tmp/walt-$(date +%s).log

# 4) 若已 panic，收现场
dmesg | sed -n '/WALT RQ DUMP START/,/WALT RQ DUMP END/p'
```

---

## 4. 遗留问题

以下条目本文未能从源码完全证实，**均已在 [04-open-questions.md](../03-comparison/04-open-questions.md)
登记**（编号见下）：

1. `[待确认]` §1.14 的 `early_up/down` 与 `skip_sp_newly_idle_lb` 无读者——
   无法从源码判断是刻意移除还是移植遗漏，需比对上游提交历史。
   → **Q-30**
2. `[待确认]` §3.5 的 `panic_on_walt_bug` 读写在 `int`/`unsigned int` 之间混用，
   `0x4544DE33` 作为有符号 `int` 的解释是否与设计意图一致，需实验确认。
   → **Q-14**
3. `[推测]` §1.15b 的 `busy_up_thres` / `busy_down_thres` 零初值由用户空间补齐，
   证据是 `cluster_state[]` 无静态初始化且无内核内默认赋值路径；
   具体写入者需查 `init.rc` / `core_ctl` 的 vendor 配置（不在本仓库内）。
   → **Q-20**
4. `[待确认]` §2.3 的 `sched_update_task_ravg` 与 `_mini` 背靠背触发的设计意图
   （"低开销长期采样"）为推测，未见注释佐证；两者的实际开销比需实测。
   → **Q-32**

## 5. 本文未覆盖的内容

按 [CONVENTIONS.md §6](../CONVENTIONS.md) 的职责划分，以下内容**不在本文范围**，
仅给出指路：

| 内容 | 归属 |
|---|---|
| 每个 tunable 影响什么算法、调大调小的后果 | [window-model.md](01-window-model.md)、[placement.md](04-placement.md)、[cpufreq.md](03-cpufreq.md)、[boost.md](08-boost.md)、[groups-and-clusters.md](06-groups-and-clusters.md)、[load-balance.md](05-load-balance.md)、[rt-mvp.md](09-rt-mvp.md) |
| `walt_rq` / `walt_task_struct` 字段含义 | [03-data-structures.md](../00-overview/03-data-structures.md) |
| hook 注册与调用点 | [02-integration-model.md](../00-overview/02-integration-model.md) |
| 与 baseline（原生 CFS/EAS）的逐项差异 | [01-base-vs-walt.md](../03-comparison/01-base-vs-walt.md) |

---

## 6. 相关文档

- 字段含义（`walt_rq` / `walt_task_struct` 的所有字段）→ [03-data-structures.md](../00-overview/03-data-structures.md)
- hook 注册与调用点 → [02-integration-model.md](../00-overview/02-integration-model.md)
- 函数调用时序 → [04-data-flow.md](../00-overview/04-data-flow.md)
- 窗口与 rollover 算法 → [window-model.md](01-window-model.md)
- `pred_demand` / 16 桶直方图 → [demand-prediction.md](02-demand-prediction.md)
- boost 机制与控制路径 → [boost.md](08-boost.md)
- RTG / 共置 / cluster 关系 → [groups-and-clusters.md](06-groups-and-clusters.md)
- 调频侧算法 → [cpufreq.md](03-cpufreq.md)
- 放置侧算法 → [placement.md](04-placement.md)
- 负载均衡侧算法 → [load-balance.md](05-load-balance.md)
- RT 与 MVP → [rt-mvp.md](09-rt-mvp.md)
- 功耗侧（core_ctl / walt_halt） → [power-side.md](07-power-side.md)
- 与 baseline 的对比 → [01-base-vs-walt.md](../03-comparison/01-base-vs-walt.md)
- 本文遗留的疑点（Q-14 / Q-20 / Q-30 / Q-32） → [04-open-questions.md](../03-comparison/04-open-questions.md)
- 写作约定与引用规范 → [CONVENTIONS.md](../CONVENTIONS.md)
