# 未决问题登记

> **最后核对**：2026-09-17
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **用途**：汇总全套笔记中所有 `[待确认]` / `[推测]` / 存疑条目，作为后续验证的工单清单。

每条给出：**编号** / **一句话结论** / **证据** / **验证方法**。
`状态` 列：`OPEN` = 未验证；`NOTE-ONLY` = 只是缺文档，非代码疑点。

---

## 0. 怎么用这份文档

笔记正文里的 `[待确认]` 标记表示「**我读了源码，但结论不能从源码完全确定**」。
它有两种来源：

| 来源 | 含义 | 典型验证方法 |
|---|---|---|
| **注释-代码不一致** | 源码注释说的和代码做的不一样 | 读代码即可判定，本表直接给结论 |
| **死代码 / 无调用者** | 符号存在但无人用 | 全树 grep |
| **竞态 / 时序** | 需要实际运行才知道 | ftrace / 真机实验 |
| **外部初始化** | 值由用户态或 out-of-tree 模块提供 | 查 `init.rc` / vendor 配置 |
| **超出本仓库** | 需要比对上游提交历史 | `git log` / lore.kernel.org |

**已在本表给出确定结论的条目**（标注 `已判定`），笔记正文中的
`[待确认]` 可以视为已关闭。

---

## 1. 已判定（读码即可定论）

这些是我在写笔记过程中发现、并通过进一步读码**已经得出结论**的条目。
它们不是「未决」，而是「原以为是疑点，实际是明确的事实/缺陷」。

### Q-01 · WALT 的迁移阈值是 3.1%，注释写 6% `已判定`

- **结论**：`walt_find_energy_efficient_cpu()` 的实际阈值是
  `(prev_energy - best_energy) <= prev_energy >> 5`，即 **1/32 ≈ 3.125%**。
  源码注释写的是 "at least 6%"。
- **对比**：原生 EAS 的 `find_energy_efficient_cpu()` 用
  `(prev_energy - best_energy) * 16 < prev_energy`，即 **1/16 = 6.25%**
  （见 [placement-eas.md §3](../01-baseline/04-placement-eas.md#3-find_energy_efficient_cpu)）。
- **影响**：WALT **比原生更愿意迁移**——省 3% 就搬。
- **证据**：[walt_cfs.c:1124-1129](../../kernel/kernel/sched/walt/walt_cfs.c#L1124-L1129)
- **需验证**：是否有意为之（`>> 4` 才是 6%）。查 Qualcomm 提交历史。
- **状态**：`OPEN`（数值已确定，意图未定）

### Q-02 · `max_task_load()` 是死代码 `已判定`

- **结论**：`max_task_load()` 在
  [walt.h:728](../../kernel/kernel/sched/walt/walt.h#L728) 定义，
  **全树零调用者**（`grep -rn "max_task_load" kernel/` 只命中定义本身）。
- **连带**：`nr_big_tasks` 的真实判据**不是**
  `demand_scaled > 0.5 * max_task_load()`，而是容量相对的
  **`!task_fits_max(p, rq->cpu)`**。
- **证据**：增量维护在 `adjust_misfit_task_accounting()`
  [walt.c:4442](../../kernel/kernel/sched/walt/walt.c#L4442)，
  由 `android_rvh_update_misfit_status()`
  [walt.c:4755](../../kernel/kernel/sched/walt/walt.c#L4755) 驱动，
  `misfit` 值来自 [walt.c:4743-4746](../../kernel/kernel/sched/walt/walt.c#L4743-L4746)。
- **已修复**：[data-structures.md §3](../00-overview/03-data-structures.md) 的过时括注已更正。
- **状态**：`CLOSED`

### Q-03 · `eval_need_32bit()` 写错字段 `已判定（疑为 bug）`

- **结论**：`eval_need_32bit()` 在
  [core_ctl.c:1012](../../kernel/kernel/sched/walt/core_ctl.c#L1012)
  **写** `cluster->need_cpus`，却**读** `last_need_32bit_cpus`。
  对照正确的 `eval_need()`（[core_ctl.c:950](../../kernel/kernel/sched/walt/core_ctl.c#L950)），
  这里应当是写 `need_32bit_cpus`。
- **影响**：32 位任务活跃时，core_ctl 的核数决策可能偏离预期。
- **证据**：字段读写不对称。
- **状态**：`OPEN`（需真机确认影响面）

### Q-04 · `input-boost.c` 的多个宏与 kobject 是死代码 `已判定`

- **结论**：`input_boost_attr_rw` / `show_one` / `store_one` 三个宏
  以及 `input_boost_kobj` 均无使用者；本树**无 `input_boost_*` kobject 节点**。
  真正的入口只有 `/proc/sys/walt/input_boost/*`。
- **另**：`input-boost.c` **没有** exit 路径，也**没有**
  `walt_input_boost_init()`（真名是 `input_boost_init()`）。
- **证据**：[input-boost.c:21-40](../../kernel/kernel/sched/walt/input-boost.c#L21-L40) 及全树 grep。
- **状态**：`CLOSED`

### Q-05 · `set_task_boost()` 无树内调用者 `已判定`

- **结论**：本树内**没有任何 `set_task_boost()` 调用者**，
  只有定义 + `EXPORT_SYMBOL`。
- **另**：其注释说 `boost` 应为 `0/1/2`，但代码接受 `0..3`。
- **推断**：供 out-of-tree 模块（vendor 驱动）使用。
- **状态**：`OPEN`（调用方归属超出本仓库）

### Q-06 · `boost.c` 没有 kthread / workqueue / timer `已判定`

- **结论**：`grep -n "kthread\|workqueue\|work_struct\|timer" boost.c` 返回空。
  `boost.c` 是**纯 refcount 状态机**，没有异步上下文。
- **另**：`boost.c` 内**没有任何超时/衰减**逻辑。
  超时只存在于 input-boost 的 `delayed_work` 和 `per_task_boost()` 的惰性过期。
- **状态**：`CLOSED`

### Q-07 · `sched_boost` 开机默认就是 1 `已判定`

- **结论**：`walt_boost_init()` 在 `walt_init()` 末尾
  [walt.c:5131](../../kernel/kernel/sched/walt/walt.c#L5131)
  **无条件**调用 `sched_set_boost(FULL_THROTTLE_BOOST)`。
- **连带影响**（三条，都能从源码直接推出）：
  1. `is_ed_enabled()` [walt.c:471](../../kernel/kernel/sched/walt/walt.c#L471)
     就是 `boost_policy != SCHED_BOOST_NONE` —— **任意 boost 都会开启 Early Detection**。
  2. FT boost 下 `task_sched_boost()` [walt.h:517](../../kernel/kernel/sched/walt/walt.h#L517)
     对**所有**任务返回 true，**cgroup 过滤完全失效**。
  3. `sysctl_sched_boost_on_input` **从未被初始化**（BSS 零），
     默认 0 = input boost 不联动全局 boost。
- **状态**：`CLOSED`（事实确定；是否为出厂意图需产品确认）

### Q-08 · `android_rvh_effective_cpu_util` 全树无注册者 `已判定`

- **结论**：该 hook **已声明、已导出、已调用，但没有任何注册者**。

  | 环节 | 位置 | 存在 |
  |---|---|---|
  | DECLARE | `kernel/include/trace/hooks/sched.h:402` | ✅ |
  | EXPORT | [vendor_hooks.c:121](../../kernel/kernel/sched/vendor_hooks.c#L121) | ✅ |
  | CALL | [core.c:7328](../../kernel/kernel/sched/core.c#L7328) | ✅ |
  | REGISTER | —— | ❌ |

- **验证命令**（结果为空即成立）：

  ```bash
  grep -rn 'register_trace_android_rvh_effective_cpu_util' .
  ```

- **推论**：`new_util` 恒为 `ULONG_MAX`，
  [core.c:7330](../../kernel/kernel/sched/core.c#L7330) 的短路分支**永不进入**，
  `effective_cpu_util()` 第 2-8 步**全是活代码**。
  WALT **不**通过这个 hook 接管 util——它用
  `cpu_util_freq_walt()` [walt.c:680](../../kernel/kernel/sched/walt/walt.c#L680)，
  唯一调用者是 `waltgov_get_util()`
  [cpufreq_walt.c:284](../../kernel/kernel/sched/walt/cpufreq_walt.c#L284)。
- **已修复的错误结论**：本仓库早期版本在
  [03-schedutil.md §3.2](../01-baseline/03-schedutil.md#32-第-1-步一个未生效的-vendor-hook-短路点-关键)、
  [02-pelt.md §7](../01-baseline/02-pelt.md)、
  [02-cpufreq-diff.md §2.1](02-cpufreq-diff.md)、
  [base-vs-walt.md §8](01-base-vs-walt.md)、
  [03-cpufreq.md §2.2](../02-walt/03-cpufreq.md) 中断言「WALT 短路了
  `effective_cpu_util()`，第 2-8 步是死代码」，**该断言是错的**，已全部更正。
- **`[待确认]` 的部分**：符号是 `EXPORT_TRACEPOINT_SYMBOL_GPL` 导出的，
  一个**树外** vendor 模块仍可在运行时注册它，届时短路会生效。
  因此它是「设计上存在、本树未启用」的接管点，而非死代码。
  要确认需检查设备上 `lsmod` / BSP 私有模块。
- **状态**：`CLOSED`（树内事实）；`[待确认]` 的树外可能性保持 `OPEN`

### Q-09 · `walt_map_util_freq` 的频率缓存比较可能永久失效 `OPEN`

- **描述**：[cpufreq_walt.c:262](../../kernel/kernel/sched/walt/cpufreq_walt.c#L262)
  的缓存短路比较的是**整形后**的 `freq`：

  ```c
  if (wg_policy->cached_raw_freq && freq == wg_policy->cached_raw_freq &&
      !wg_policy->need_freq_update)
          return 0;
  ```

  但 `cached_raw_freq` 在
  [cpufreq_walt.c:136](../../kernel/kernel/sched/walt/cpufreq_walt.c#L136)
  被赋的是 adaptive **之前**的 `raw_freq`。
  当 adaptive 档位生效时（`freq` 被改写为 `adaptive_low`/`adaptive_high`），
  两边**永不可能相等**，缓存短路失效。
- **影响**：adaptive 场景下每次窗口滚动都会走完整的
  `cpufreq_driver_resolve_freq()`；不过 `waltgov_update_next_freq()`
  的「同频跳过」[cpufreq_walt.c:128](../../kernel/kernel/sched/walt/cpufreq_walt.c#L128)
  仍会挡住实际写频率，所以**性能影响有限，不构成功能 bug**。
- **待确认**：这是刻意设计（adaptive 生效说明状态已变，缓存本就不该命中）
  还是笔误（应当比较 `raw_freq`）。需对照 Qualcomm 其他版本或运行时验证。
- **状态**：`OPEN`

---

## 2. 需要真机 / ftrace 验证

### Q-10 · binder boost 与惰性过期的竞态 `OPEN`

- **描述**：`binder_restore_priority_hook()`
  [walt_cfs.c:1207](../../kernel/kernel/sched/walt/walt_cfs.c#L1207)
  **写他人的 `wts->boost`**，而对方可能正在 `per_task_boost()` 里清 0。
- **风险**：`wts->boost` 是普通字段，无锁保护。
- **验证**：高 binder 负载下用 ftrace 抓 `wts->boost` 的写序列；
  或代码审查确认 `per_task_boost()` 的调用上下文与 binder hook 是否可能并发。
- **可达性**：未知。

### Q-11 · `core_ctl` / `walt_halt` 未检查 `is_reserved()` `OPEN`

- **描述**：`core_ctl.c` 与 `walt_halt.c` 都**没有**检查 `is_reserved()`
  （hyp_core_ctl 保留给 hypervisor 的核），而 `walt_lb.c:841` **有**。
- **风险**：core_ctl 可能去 halt 一个已被 hypervisor 保留的核。
- **验证**：读 `hyp_core_ctl.c` 确认保留核是否同时被从 `cpu_online_mask` 摘除；
  若是，则 `core_ctl` 看不到它们，问题不存在。
- **相关**：[power-side.md §7](../02-walt/07-power-side.md)。

### Q-12 · `nr_big_prod_sum` 累加口径不对称 `OPEN`

- **描述**：累加用 `walt_big_tasks()`（**含** 32 位，
  [sched_avg.c:293](../../kernel/kernel/sched/walt/sched_avg.c#L293)），
  而收尾补算用 `walt_big_64bit_tasks()`（**不含** 32 位，
  [sched_avg.c:98](../../kernel/kernel/sched/walt/sched_avg.c#L98)）。
- **影响**：32 位任务活跃时 `nr_misfit`（暴露给用户的平均大任务数）可能有偏差。
- **验证**：跑一个 32 位大任务负载，对比 `/proc/sys/walt/...` 的
  `nr_misfit` 与实时 `nr_big_tasks`。

### Q-13 · input boost 的 150ms 限流 vs 40ms boost 时长 `OPEN`

- **描述**：触摸限流是 150ms
  （[input-boost.c:57](../../kernel/kernel/sched/walt/input-boost.c#L57)），
  而默认 boost 时长是 40ms。
- **影响**：**限流窗口大于 boost 时长**，实际是脉冲式生效，
  连续触摸不会持续 boost。
- **验证**：连续触摸并抓 `freq_qos` 约束的生效时间序列。
- **备注**：input boost 走 `freq_qos` 的 `FREQ_QOS_MIN` **硬约束**，
  会**碾压** walt governor 的输出。

### Q-14 · `panic_on_walt_bug` 的 `int`/`unsigned int` 混用 `OPEN`

- **描述**：[observability.md §3.5](../02-walt/10-observability.md) 的该 sysctl
  读写在 `int` / `unsigned int` 之间混用，
  哨兵值 `0x4544DE33` 作为**有符号** `int` 的解释是否与设计意图一致。
- **验证**：写入 `0x4544DE33` 读回，确认符号扩展行为。

### Q-15 · `sched_boost_disable_all()` 与 core_ctl refcount 的交互 `OPEN`

- **描述**：`sched_boost_disable_all()`
  [boost.c:225](../../kernel/kernel/sched/walt/boost.c#L225)
  会清掉**别的子系统**的 refcount。
- **问题**：用户写 `sched_boost=0` 后，pipeline 的保核核数是否按预期回退？
- **验证**：真机写 `sched_boost=0`，观测在线核数与 pipeline 任务分布。

### Q-16 · `for_each_sched_entity` + `set_next_entity` 的 CFS_BANDWIDTH TODO `OPEN`

- **描述**：[walt_cfs.c:1527](../../kernel/kernel/sched/walt/walt_cfs.c#L1527)
  有上游 TODO：`If CFS_BANDWIDTH is enabled, we might pick from a throttled cfs_rq`。
- **影响**：本树**未启用** CFS bandwidth 时无影响。
- **验证**：确认 `CONFIG_CFS_BANDWIDTH` 未开；若开了需 trace 验证一次。

### Q-17 · heavy 排序用 `demand`，topapp 排序用 `demand_scaled` `OPEN`

- **描述**：`rearrange_heavy()` 比较 **`wts->demand`**
  （[walt.c:3863](../../kernel/kernel/sched/walt/walt.c#L3863)），
  而 `find_heaviest_topapp()` 比较 **`demand_scaled`**
  （[walt.c:3729](../../kernel/kernel/sched/walt/walt.c#L3729)）。
- **问题**：异频/异容量场景下两者排序是否可能不一致？是否有意为之？
- **背景**：`demand` 是频率归一化前的量，`demand_scaled` 是归一化后的
  （见 [window-model.md §8](../02-walt/01-window-model.md#8-频率归一化scale_exec_time)）。
  在同一簇内两者单调关系一致；**跨簇比较时可能不一致**。
- **验证**：构造异频负载，对比两个排序的实际输出。
- **相关**：[groups-and-clusters.md §12](../02-walt/06-groups-and-clusters.md)

### Q-18 · `sysctl_sched_coloc_downmigrate_ns` 默认 0 `OPEN`

- **描述**：该 sysctl 在 `sysctl.c` 中**声明时未给初值**，默认 0
  会让 `update_best_cluster()` 走「**立即下迁**」分支。
- **关联**：`update_best_cluster()`
  （[walt.c:2876](../../kernel/kernel/sched/walt/walt.c#L2876)）
  的滞回是**非对称**的——进入 `skip_min` 立即生效，
  离开需同时满足 `hyst_min_coloc_ns` **和** `coloc_downmigrate_ns`。
  `coloc_downmigrate_ns = 0` 意味着后一半条件自动满足。
- **问题**：是否由用户态初始化脚本设置？
- **验证**：查 `init.rc` / vendor 配置（**不在本仓库内**）；与 Q-20 同源。
- **相关**：[06-groups-and-clusters.md](../02-walt/06-groups-and-clusters.md)

### Q-19 · `get_rtgb_active_time()` 的消费方未逐一追踪 `OPEN`

- **描述**：`get_rtgb_active_time()`
  （[walt.c:3530](../../kernel/kernel/sched/walt/walt.c#L3530)）
  的消费方未全部追到。
- **推测影响**：可能与 RTG boost 的持续时间统计有关
  （[08-boost.md](../02-walt/08-boost.md) / [03-cpufreq.md](../02-walt/03-cpufreq.md) 的
  `rtgb_active` 通道）。
- **验证**：全树追踪该函数的返回值最终流向。

---

## 3. 需要外部信息（超出本仓库）

### Q-20 · 用户态对 WALT sysctl 的初始化 `OPEN`

- **描述**：`cluster_state[]` 的 `busy_up_thres` / `busy_down_thres`
  **零初值**且**无内核内默认赋值路径**，只能由用户空间补齐。
- **证据**：[observability.md §1.15b](../02-walt/10-observability.md)。
- **验证**：查 `init.rc` / vendor 配置（**不在本仓库内**）。
- **相关**：`sysctl_sched_heavy_nr`（默认 0 = 关闭）等开关同样需要用户态打开。

### Q-21 · `input_boost_init()` 返回值被丢弃 `OPEN`

- **描述**：cpufreq policy 缺失时的降级行为未定义；
  `input_boost_wq` 已创建但 handler 未注册，处于「半初始化」状态。
- **验证**：需要看调用方（`walt_init()`）的返回值处理约定。

### Q-22 · `CONFIG_HOTPLUG_CPU` 是否为 y `OPEN`

- **描述**：假设 arm64 默认 y（`arch/arm64/configs/` 下无显式设置）。
- **风险**：若关闭，`walt_halt.c` **整个为空**，`__cpu_halt_mask` 未定义
  → **链接失败**。
- **验证**：查实际使用的 `defconfig`。

### Q-23 · `is_migration` 取自 driving CPU 而非被 boost 的 CPU `OPEN`

- **描述**：`waltgov_next_freq_shared()`
  [cpufreq_walt.c:397](../../kernel/kernel/sched/walt/cpufreq_walt.c#L397)
  对 policy 内**每个** CPU 调用 `waltgov_walt_adjust()`，
  但 `is_migration` 来自 `wg_cpu->flags`
  [cpufreq_walt.c:310](../../kernel/kernel/sched/walt/cpufreq_walt.c#L310)，
  而 `wg_cpu` 是**发起本次回调的那个 CPU**——
  按 `WALT_CPUFREQ_CONTINUE` 协议，只有簇内**最后一个** CPU 会走到这里
  （见 [11-freq-pipeline.md §4](../02-walt/11-freq-pipeline.md)）。
- **影响**：当被 boost 的 `j_wg_cpu` 与 `wg_cpu` 不是同一个 CPU 时，
  HISPEED 的 `!is_migration` 门限
  [cpufreq_walt.c:329](../../kernel/kernel/sched/walt/cpufreq_walt.c#L329)
  可能用到的是另一个 CPU 的迁移状态。
- **待确认**：迁移场景下 `flags` 是否簇内统一（若统一则无影响）。
  可在真机上用 `trace_waltgov_util_update` 对比 `flags` 与 `cpu`。
- **状态**：`OPEN`

---

## 4. 源码自带的技术债（非疑点，记录备查）

这些是**源码注释自己承认**的临时实现，不是我们的推测：

| 编号 | 内容 | 位置 |
|---|---|---|
| D-01 | `//TODO can we just replace with detach_task in fair.c??` | [walt_lb.c:22](../../kernel/kernel/sched/walt/walt_lb.c#L22) |
| D-02 | `from_lower_cap` 分支自认是临时实现：<br>`"we really don't need this as a separate block. will refactor this after final testing is done."` | [walt_lb.c:561-567](../../kernel/kernel/sched/walt/walt_lb.c#L561-L567) |
| D-03 | `walt_lb.c` 里复制了 `sysctl_sched_migration_cost` 的语义而**不复用该变量** | [walt_lb.c:803](../../kernel/kernel/sched/walt/walt_lb.c#L803) |
| D-04 | `max_newidle_lb_cost` 的成本模型在 WALT 的 newidle 替换路径上**不再生效**（因为原生 newidle 被 `done` 短路） | [fair.c:11163-11165](../../kernel/kernel/sched/fair.c#L11163-L11165) |

---

## 5. 被否证的题目假设

写这批笔记时，任务描述里提到的一些符号**在本树不存在**。
记录在此，避免以后重复踩坑：

| 假设的符号 | 实际情况 |
|---|---|
| `walt_update_task_ravg_stats()` | 不存在。真名 `fixup_cumulative_runnable_avg()` [walt.c:307](../../kernel/kernel/sched/walt/walt.c#L307) / `fixup_walt_sched_stats_common()` [walt.c:342](../../kernel/kernel/sched/walt/walt.c#L342) |
| `walt_newidle_balance()` | 存在，但在 **walt_lb.c:805**，**不在 walt_cfs.c** |
| `rotate_heavy_to_random_cpu()` | 不存在。实际是 `walt_lb_check_for_rotation()` [walt_lb.c:134](../../kernel/kernel/sched/walt/walt_lb.c#L134) |
| `sysctl_sched_big_task_rotation_us` | 不存在。实际开关是**布尔**的 `sysctl_sched_walt_rotate_big_tasks`（[sysctl.c:650](../../kernel/kernel/sched/walt/sysctl.c#L650)），且未初始化 = BSS 0 = **默认关闭** |
| `sysctl_sched_newidle_balance` | 不存在，newidle 没有运行时开关 |
| `walt_halt_lb_*` API | 不存在。halt↔LB 通过内联 `cpu_halted()` 检查（walt_lb.c:264/838）+ unhalt 时 `walt_smp_call_newidle_balance()` |
| `wrq->lb_*` 字段 | 不存在，也没有 per-cluster LB 统计 |
| `struct core_ctl_cluster` | 不存在。实际是 `struct cluster_data` [core_ctl.c:26](../../kernel/kernel/sched/walt/core_ctl.c#L26) |
| `core_ctl_do_offline/do_online()` | 不存在。实际是 `do_core_ctl()` |
| `core_ctl` 字段 `nr_need_cpus` / `nr_run` | 不存在。实际是 `need_cpus` / `nrrun` |
| core_ctl 通过 `cpu_up()/cpu_down()` 热插拔 | **否**。core_ctl 已不热插拔，改为通过 **walt_halt 的 halt/pause** 作动。`offline_delay_ms` / `cpus_paused_by_us` 是遗留命名 |
| halt = 迁移 + **idle 注入** | **否**。`grep -rni idle_inject` 零命中。halt = 置 mask + 排空 rq（`halt_drain_rqs` kthread 里的 `stop_one_cpu`），CPU 自然 idle |
| schedutil 的 `map_util_freq()` 含 1.25× 余量 | **否**。1.25× 是 `map_util_perf()` = `util + (util >> 2)`，在 `map_util_freq()` **之前**应用 |
| `sched_util_freq_margin` | 本树**不存在** |
| `newidle_balance()` | 本树改名为 `sched_balance_newidle()` [fair.c:11154](../../kernel/kernel/sched/fair.c#L11154) |
| `sched_balance_trigger()` | 上游已移除，本树不存在 |
| `struct sched_avg` / `struct util_est` 在 `kernel/sched/sched.h` | **否**，在 [include/linux/sched.h](../../kernel/include/linux/sched.h) |
| `update_load_avg()` / `update_cfs_rq_load_avg()` 在 `pelt.h` | **否**，是 fair.c 里的 `static inline` |
| `/proc/sys/kernel/sched_*` 注册表 | 本裁剪子树中**不存在**，只有 debugfs 暴露 |

---

## 6. 已登记但仅属「缺文档」的条目

### Q-30 · `early_up/down` 与 `skip_sp_newly_idle_lb` 无读者 `OPEN`

- **描述**：[observability.md §1.14](../02-walt/10-observability.md) 中这两个 sysctl
  **没有读者**。是刻意移除还是移植遗漏？
- **验证**：比对上游提交历史（超出本仓库）。
- **状态**：`OPEN`

### Q-31 · `help_min_cap` / `force_overload` / `1280/1024` `OPEN`

- **`help_min_cap`**：`should_help_min_cap()`
  [walt_lb.c:788](../../kernel/kernel/sched/walt/walt_lb.c#L788)
  的结果**只进 tracepoint**，无分支消费 → 死变量。
- **`force_overload` 语义**：`walt_lb_tick()` 调
  `walt_find_energy_efficient_cpu(p, prev_cpu, 0, 1)` 的**第 4 个实参**，
  其参数名未在声明处断言。
- **`1280/1024`**：§3.2 的 1.25 倍闸门是**硬编码**，未找到对应 sysctl。
- **状态**：`OPEN`

### Q-32 · `sched_update_task_ravg` 与 `_mini` 背靠背触发 `OPEN`

- **描述**：[observability.md §2.3](../02-walt/10-observability.md) 中两者背靠背触发，
  设计意图不明（是否有意用 `_mini` 覆盖前者的 trace 输出？）。
- **状态**：`OPEN`

---

## 7. 相关文档

- 基线 vs WALT 对照 → [base-vs-walt.md](01-base-vs-walt.md)
- 调频逐函数差异 → [cpufreq-diff.md](02-cpufreq-diff.md)
- 各专题的遗留问题章节 → [../02-walt/](../02-walt/) 下各文档的「遗留问题」
