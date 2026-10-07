# Boost：全局状态机、per-task boost 与 input boost

> **源码**：[boost.c](../../kernel/kernel/sched/walt/boost.c)、[input-boost.c](../../kernel/kernel/sched/walt/input-boost.c)
> **内核版本**：5.15.211 (Qualcomm, sm8550/lineage-21)
> **最后核对**：2026-09-17

---

## 0. 先给结论：这个树里有什么、没有什么

WALT 的 "boost" 在社区内核里是个含义极其混乱的词——同一个词至少指四件互不相关的事。
在动代码之前先把边界划清楚，本树（5.15.211 / sm8550）的实际情况是：

| # | 机制 | 作用对象 | 实现位置 | 主要效果 |
|---|---|---|---|---|
| 1 | **全局 boost 状态机** | 整机 | `boost.c`（300 行，全部） | 置位 `boost_policy`、开频率聚合、把任务往大核放 |
| 2 | **per-cgroup boost** | 任务组 | `walt.h` 的 `task_sched_boost()` | 决定某 cgroup 是否参与当前 boost 类型 |
| 3 | **per-task boost** | 单任务 | `wts->boost` / `boost_period` | 让单个任务（及 binder 链）被当作大任务 |
| 4 | **per-task load boost** | 单任务负载 | `wts->load_boost` | 放大 `scale_exec_time()` 的产出，把任务"喂胖" |
| 5 | **input boost** | 整机（触摸期） | `input-boost.c`（300 行，全部） | 用 `freq_qos` 抬频率下限，可选附带触发 #1 |

**必须明确的否证**（上一版笔记的常见错误）：

- **`boost.c` 里没有 kthread，也没有 workqueue，更没有 timer。**
  `grep -n "kthread\|workqueue\|work_struct\|timer" boost.c` 返回空。
  整个文件是一个**纯 refcount 状态机**，进入/退出回调同步执行。
  任务描述中设想的 `boost_kthread` 在本树**不存在**。
- **`boost.c` 里没有任何超时/衰减逻辑。** 超时只在两个地方有，且都是别处的：
  input-boost 的 `delayed_work`（[input-boost.c:55](../../kernel/kernel/sched/walt/input-boost.c#L55)），
  和 per-task boost 的惰性过期（[walt.h:655](../../kernel/kernel/sched/walt/walt.h#L655)）。
  boost.c 顶部的注释把责任说得很直白：
  > "Any entity enabling boost is responsible for disabling it as well."
  > —— [boost.c:11-16](../../kernel/kernel/sched/walt/boost.c#L11-L16)
- **boost 不写 `wrq->boost`**——本树**没有这个字段**（`grep "wrq->boost"` 返回空）。
  boost 影响频率是**间接**的，路径见 §3。
- **没有 `walt_input_boost_init()`。** 函数名是 `input_boost_init()`
  [input-boost.c:264](../../kernel/kernel/sched/walt/input-boost.c#L264)。

---

## 1. 为什么要有 boost：三种不同层次的"抢资源"

WALT 的常规决策都建立在「任务的历史负载」上——这天然是**滞后**的。
但有几类场景，负载信号来不及反映用户的真实意图：

1. **用户主观觉得卡**：手指刚触到屏幕，但触摸线程还没累积出高负载。
   这时需要**绕过负载模型，直接抬频率下限**——这是 input boost（§6）的动机。
2. **UI 关键路径跨线程**：App 主线程把活派给 binder 线程，binder 线程自己负载不高，
   但它的延迟决定了用户感知。这时需要**给单个任务打标**——这是 per-task boost（§4）。
3. **系统级场景切换**：相机启动、应用冷启，整个系统的任务分布需要临时偏斜到大核。
   这时需要**整机状态**——这是全局 boost 状态机（§2）。

三者的共同点是：**都是"临时"的，都在为"短期正确"牺牲"长期能效"**。
所以它们的设计重心不是算法，而是**状态生命周期管理**——谁开、谁关、冲突了听谁的。
这正是 `boost.c` 的 300 行全部在做的事。

---

## 2. 全局 boost 状态机（`boost.c`）

### 2.1 四种类型、三种策略

类型定义在 [walt.h:342-349](../../kernel/kernel/sched/walt/walt.h#L342-L349)：

```c
#define NO_BOOST 0
#define FULL_THROTTLE_BOOST 1
#define CONSERVATIVE_BOOST 2
#define RESTRAINED_BOOST 3
#define FULL_THROTTLE_BOOST_DISABLE -1   /* 取负 = 关闭对应类型 */
...
#define MAX_NUM_BOOST_TYPE (RESTRAINED_BOOST+1)
```

三个全局变量（[boost.c:17-20](../../kernel/kernel/sched/walt/boost.c#L17-L20)），注意它们**职责不同**：

| 变量 | 含义 |
|---|---|
| `sched_boost_type` | **实际生效**的 boost 类型，由 refcount 聚合算出 |
| `sysctl_sched_boost` | **用户态视角**的 boost 值，是 sysfs 读写的那一个 |
| `boost_policy` | 由 type 派生的**放置策略**：`SCHED_BOOST_NONE / ON_BIG / ON_ALL` |

`boost_policy` 的枚举在 [walt.h:351-355](../../kernel/kernel/sched/walt/walt.h#L351-L355)。
两者的映射由 `set_boost_policy()` [boost.c:72](../../kernel/kernel/sched/walt/boost.c#L72) 完成：

- `NO_BOOST` / `RESTRAINED_BOOST` → `SCHED_BOOST_NONE`
- 其余：HMP 平台（有大小核）→ `SCHED_BOOST_ON_BIG`，否则 → `SCHED_BOOST_ON_ALL`

> **`[反直觉]`**：`sched_boost_type` 和 `boost_policy` **不是双射**。
> `RESTRAINED_BOOST` 的 policy 是 `NONE`——也就是说它**只开频率聚合，不做任何放置倾斜**。
> 这一点在 [boost.c:61-71](../../kernel/kernel/sched/walt/boost.c#L61-L71) 的注释里被专门解释过。
> 读 placement 代码时若看到 `boost_policy != SCHED_BOOST_NONE` 就以为"有 boost"，会误判 RESTRAINED。

### 2.2 refcount 状态机：优先级聚合

核心是一个按类型索引的数组 [boost.c:132-153](../../kernel/kernel/sched/walt/boost.c#L132-L153)：

```c
static struct sched_boost_data sched_boosts[] = {
	[NO_BOOST]            = { .enter = sched_no_boost_nop, ... },
	[FULL_THROTTLE_BOOST] = { .enter = sched_full_throttle_boost_enter, ... },
	...
};
```

每个条目有 `refcount` + `enter()` / `exit()` 回调。
三种操作在 `_sched_set_boost()` [boost.c:237](../../kernel/kernel/sched/walt/boost.c#L237) 分发：

```c
if (type == 0)        sched_boost_disable_all();   /* 全清 */
else if (type > 0)    sched_boost_enable(type);    /* refcount++ */
else                  sched_boost_disable(-type);  /* refcount-- */
```

**生效类型的选择**由 `sched_effective_boost()` [boost.c:158](../../kernel/kernel/sched/walt/boost.c#L158) 做——
这是个"优先级仲裁"：从 `FULL_THROTTLE_BOOST` 开始往下扫，**第一个 refcount ≥ 1 的胜出**。
注释明说 "The boosts are sorted in descending order by priority"。

因此 `sched_boost_enable()` [boost.c:199](../../kernel/kernel/sched/walt/boost.c#L199) 的逻辑是
「先算聚合结果，若与当前不同，则 `exit()` 旧的、`enter()` 新的」——
这是一个**只有一条上行边、一条下行边**的紧凑状态机，不存在中间态。
`[反直觉]` 反过来说也成立：**高优先级 boost 开着时，低优先级 boost 的 enable/disable 是静默的**——
refcount 在变，但不触发任何回调。

`exit` 时先 exit 再 enter（[boost.c:195-196](../../kernel/kernel/sched/walt/boost.c#L195-L196)），
顺序不可交换：例如 FT→RESTRAINED 时，`core_ctl_set_boost(false)`+`frequency_aggregation(false)`
必须发生在 `frequency_aggregation(true)` 之前，否则会把后者关掉。

### 2.3 三个 enter/exit 回调实际做了什么

| 类型 | enter | 实际动作 |
|---|---|---|
| `NO_BOOST` | `sched_no_boost_nop` [boost.c:92](../../kernel/kernel/sched/walt/boost.c#L92) | 空 |
| `FULL_THROTTLE_BOOST` | `sched_full_throttle_boost_enter` [boost.c:96](../../kernel/kernel/sched/walt/boost.c#L96) | `core_ctl_set_boost(true)` + `walt_enable_frequency_aggregation(true)` |
| `CONSERVATIVE_BOOST` | `sched_conservative_boost_enter` [boost.c:108](../../kernel/kernel/sched/walt/boost.c#L108) | **空函数** |
| `RESTRAINED_BOOST` | `sched_restrained_boost_enter` [boost.c:116](../../kernel/kernel/sched/walt/boost.c#L116) | `walt_enable_frequency_aggregation(true)` |

> **`[反直觉]`**：`CONSERVATIVE_BOOST` 的 enter/exit 是**空的**。
> 它的全部效果来自 `boost_policy == SCHED_BOOST_ON_BIG` 加上 `task_boost_policy()` 里的
> util 过滤阈值（下节）——即它是个**纯放置策略**，不碰 core_ctl、不碰频率聚合。

`walt_enable_frequency_aggregation()` 是 [walt.h:278](../../kernel/kernel/sched/walt/walt.h#L278) 的
static inline，只写一个全局 `bool sched_freq_aggr_en`（定义于 [walt.c:403](../../kernel/kernel/sched/walt/walt.c#L403)）。
它**不经过 `cpufreq_walt.c`**——governor 侧只是读这个 flag，见 §3。

### 2.4 对外 API 与 sysfs 入口

三个入口，全部最终落到 `_sched_set_boost()`：

| 入口 | 位置 | 说明 |
|---|---|---|
| `sched_set_boost(int type)` | [boost.c:259](../../kernel/kernel/sched/walt/boost.c#L259) | 内核内 API，`verify_boost_params()` 校验后加锁调用 |
| `sched_boost_handler()` | [boost.c:272](../../kernel/kernel/sched/walt/boost.c#L272) | sysfs 写处理器，包一层 `proc_dointvec_minmax` |
| `walt_boost_init()` | [boost.c:296](../../kernel/kernel/sched/walt/boost.c#L296) | 开机时 `sched_set_boost(FULL_THROTTLE_BOOST)` |

sysfs 节点 `sched_boost` 定义在 [sysctl.c:622-629](../../kernel/kernel/sched/walt/sysctl.c#L622-L629)：

```c
.procname = "sched_boost",
.data     = &sysctl_sched_boost,
.proc_handler = sched_boost_handler,
.extra1   = &neg_three,   /* 范围 [-3, 3] */
.extra2   = &three,
```

即 `/proc/sys/walt/sched_boost`，取值范围 `[-3, 3]`：
`0` = 全部关闭，正数 = 开启该类型并 refcount++，负数 = 关闭该类型并 refcount--。

> **`[反直觉]` `sched_boost` 开机默认就是 1（FULL_THROTTLE_BOOST）**。
> `walt_boost_init()` 在 `walt_init()` 末尾被无条件调用
> [walt.c:5131](../../kernel/kernel/sched/walt/walt.c#L5131)，而它直接 enable 了 FT boost，
> 于是 `_sched_set_boost` 把 `sysctl_sched_boost` 也写成 1。
> **想观察"无 boost 基线"必须先 `echo 0 > .../sched_boost`。**
> 另外 `sched_boost_disable_all()` [boost.c:225](../../kernel/kernel/sched/walt/boost.c#L225) 是"大锤"——
> 它把**所有**类型的 refcount 归零，会一并清掉别的子系统（如 pipeline）叠加的 boost。
> `[推测]` 这是 `sched_boost` 写 0 之后状态"不干净"的根因。

### 2.5 boost 落地：三条路径，没有一条是"直接设频率"

这是本节最需要讲清楚的问题。boost 影响频率**不通过**任何显式的 freq request，
而是通过三条间接路径：

**路径 A —— 频率聚合（由 `sched_freq_aggr_en` 开关控制）**
在 `freq_policy_load()` [walt.c:592](../../kernel/kernel/sched/walt/walt.c#L592)：

```c
if (sched_freq_aggr_en) {
	load = wrq->prev_runnable_sum + aggr_grp_load;   /* 整个簇的组负载 */
	*reason = CPUFREQ_REASON_FREQ_AGR;
} else
	load = wrq->prev_runnable_sum + wrq->grp_time.prev_runnable_sum;
```

`aggr_grp_load` 由 [walt.c:4033](../../kernel/kernel/sched/walt/walt.c#L4033) 在
`__walt_irq_work_locked()` 中按簇汇总。
直觉：**聚合把"本 CPU 的负载"换成"整个簇的负载"**，于是簇内任一 CPU 的组负载都会抬高
每个 CPU 的请求频率——相当于把 RTG 负载从"局部问题"变成"全局信号"。
`[推测]` 这是 FT boost 在 UI 场景下最有效的部分，因为它让大核频率不依赖于"恰好有 RTG 任务落在大核"。
另外 `sched_freq_aggr_en` 为真时会**短路 user hint boost**
（`should_apply_suh_freq_boost()` [walt.c:581](../../kernel/kernel/sched/walt/walt.c#L581) 首条件即返回 false）——
即 boost 开启时 SysAd 的 `sched_user_hint` 写值不生效。详见 [cpufreq.md](03-cpufreq.md)。

**路径 B —— 放置倾斜（经 `boost_policy`）**
`task_placement_boost_enabled()` [walt.h:534](../../kernel/kernel/sched/walt/walt.h#L534) 与
`task_boost_policy()` [walt.h:542](../../kernel/kernel/sched/walt/walt.h#L542) 被 placement 代码消费：

- `walt_get_indicies()` [walt_cfs.c:221](../../kernel/kernel/sched/walt/walt_cfs.c#L221)：
  FT boost 下直接 `*order_index = num_sched_clusters - 1`（从最大簇开始找）[walt_cfs.c:239](../../kernel/kernel/sched/walt/walt_cfs.c#L239)；
  `task_boost_policy() == ON_BIG` 时把起始簇限制为 1 [walt_cfs.c:249](../../kernel/kernel/sched/walt/walt_cfs.c#L249)。
- `task_fits_max()` [walt.h:810](../../kernel/kernel/sched/walt/walt.h#L810)：boost 任务判为"fits 小簇 = false"，被顶到大核。
- `walt_rt_energy_aware_wake_cpu()` [walt_rt.c:104](../../kernel/kernel/sched/walt/walt_rt.c#L104)：RT 任务同样受 `rt_boost_on_big()` [walt.h:498](../../kernel/kernel/sched/walt/walt.h#L498) 影响。
- `_set_preferred_cluster()` [walt.c:2949](../../kernel/kernel/sched/walt/walt.c#L2949)：组内有 `SCHED_BOOST_ON_BIG` 任务则 `group_boost = true`。
- `is_cluster_hosting_top_app()` [walt.c:3314](../../kernel/kernel/sched/walt/walt.c#L3314)：`boost_policy != SCHED_BOOST_ON_BIG` 时才认为 top-app 在最小簇上。

**路径 C —— `core_ctl` 保核**（仅 FT boost）
`core_ctl_set_boost()` [core_ctl.c:1045](../../kernel/kernel/sched/walt/core_ctl.c#L1045) 对**每个簇**的
`cluster->boost` 计数器增减，`boost_state_changed` 时调 `apply_need()`。
注意它内部**自带 refcount**，因此 boost.c 之外还有调用者：
pipeline 隔离场景 [walt.c:3701](../../kernel/kernel/sched/walt/walt.c#L3701)/[walt.c:3760](../../kernel/kernel/sched/walt/walt.c#L3760)，
以及 pipeline 任务找到时 [walt.c:3973](../../kernel/kernel/sched/walt/walt.c#L3973)。
`[反直觉]` 也就是说 `sched_boost_disable_all()` 归零 refcount 后，**pipeline 的保核核数不会自动回退**——
两边 refcount 是独立的。

**boost 的附加副作用**：`is_ed_enabled()` [walt.c:469](../../kernel/kernel/sched/walt/walt.c#L469)
直接就是 `boost_policy != SCHED_BOOST_NONE`。
也就是说**开启任意 boost 都会顺带启用 Early Detection**，进而影响 `wrq->ed_task`
（[walt.c:1175](../../kernel/kernel/sched/walt/walt.c#L1175)）和频率侧走 `CPUFREQ_REASON_ED`。
`[反直觉]` 这是 boost 与 ED 之间一条**无文档的耦合**：调 boost 会改变 ED 行为。

---

## 3. per-cgroup boost：`sched_boost_enable[]`

`struct walt_task_group` [walt.h:357](../../kernel/kernel/sched/walt/walt.h#L357) 有一个
`bool sched_boost_enable[MAX_NUM_BOOST_TYPE]` [walt.h:367](../../kernel/kernel/sched/walt/walt.h#L367)，
即**每个 cgroup 声明自己参与哪几种 boost**。三个初始化函数：

| 函数 | 位置 | 语义 |
|---|---|---|
| `walt_init_tg()` | [boost.c:22](../../kernel/kernel/sched/walt/boost.c#L22) | 默认组：仅 FT |
| `walt_init_topapp_tg()` | [boost.c:35](../../kernel/kernel/sched/walt/boost.c#L35) | top-app：FT + CONSERVATIVE，`colocate = true` |
| `walt_init_foreground_tg()` | [boost.c:48](../../kernel/kernel/sched/walt/boost.c#L48) | foreground：FT + CONSERVATIVE |

注意**没有任何组默认参与 `RESTRAINED_BOOST`**。
查询入口是 `task_sched_boost()` [walt.h:509](../../kernel/kernel/sched/walt/walt.h#L509)，
它有一个值得记的快速路径：

```c
/* optimization for FT boost, skip looking at tg */
if (sched_boost_type == FULL_THROTTLE_BOOST)
	return true;
```

`[反直觉]` **FT boost 下所有任务都算 boosted，cgroup 过滤完全失效**。
更狠的是 `task_sched_boost()` 内部做 `task_css()` + `rcu_read_lock()`——
在 FT boost 下这段整个被跳过，是热路径上实实在在的性能优化。

CONSERVATIVE 的额外过滤在 `task_boost_policy()` [walt.h:542](../../kernel/kernel/sched/walt/walt.h#L542)：

```c
if (sched_boost_type == CONSERVATIVE_BOOST &&
	task_util(p) <= sysctl_sched_min_task_util_for_boost &&
	!walt_pipeline_low_latency_task(p))
	policy = SCHED_BOOST_NONE;
```

即**保守 boost 只抬"够重"的任务**，阈值 `sysctl_sched_min_task_util_for_boost` 默认 51
（[sysctl.c:67](../../kernel/kernel/sched/walt/sysctl.c#L67)，注释说是 "1ms default for 20ms window scaled to 1024"）。
这让 CONSERVATIVE 成为"只救大任务"的模式——这正是它 enter/exit 可以为空的原因。

字段含义详见 [data-structures.md §8](../00-overview/03-data-structures.md)。

---

## 4. per-task boost：`wts->boost`

### 4.1 数据结构与惰性过期

`struct walt_task_struct` 里三个字段（[sched/walt.h:112-118](../../kernel/include/linux/sched/walt.h#L112-L118)）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `boost` | `int` | 取值 `TASK_BOOST_NONE(0)` … `TASK_BOOST_STRICT_MAX(3)` |
| `boost_period` | `u64` | 有效期（ns）；**兼作"是否有超时"的标志** |
| `boost_expires` | `u64` | 过期绝对时刻（`walt_sched_clock()` 基准） |

枚举 `enum task_boost_type` 定义在 [sched/walt.h:32-38](../../kernel/include/linux/sched/walt.h#L32-L38)：

```c
TASK_BOOST_NONE = 0,
TASK_BOOST_ON_MID,
TASK_BOOST_ON_MAX,
TASK_BOOST_STRICT_MAX,
TASK_BOOST_END,
```

**没有定时器**——过期是**惰性**的，在 `per_task_boost()` [walt.h:655](../../kernel/kernel/sched/walt/walt.h#L655) 里顺手检查：

```c
if (wts->boost_period) {
	if (walt_sched_clock() > wts->boost_expires) {
		wts->boost_period = 0;
		wts->boost_expires = 0;
		wts->boost = 0;      /* 三个字段一起清 */
	}
}
return wts->boost;
```

`[反直觉]` 这段是**读路径带写副作用**，且**没有任何锁**——
它靠"boost 是 per-task 的、只有该任务自己（或迁移时持 rq lock 的 CPU）会读"来保证无竞争。
`[待确认]` 但 `binder_restore_priority_hook()` [walt_cfs.c:1207](../../kernel/kernel/sched/walt/walt_cfs.c#L1207)
会写**别人的** `wts->boost`，理论上与惰性过期存在竞态窗口。是否可达需要实验（见 §7）。

### 4.2 设置入口：sysfs 两个，内核 API 一个

**sysfs（用户态可用）**——两个 pid+value 形式的节点，走 `sched_task_handler()`
[sysctl.c:211](../../kernel/kernel/sched/walt/sysctl.c#L211)：

| 节点 | 位置 | `param` |
|---|---|---|
| `sched_per_task_boost` | [sysctl.c:939-945](../../kernel/kernel/sched/walt/sysctl.c#L939-L945) | `PER_TASK_BOOST` |
| `sched_per_task_boost_period_ms` | [sysctl.c:946-952](../../kernel/kernel/sched/walt/sysctl.c#L946-L952) | `PER_TASK_BOOST_PERIOD_MS` |

写格式是 `"<pid> <value>"`（两个数）。处理逻辑在 [sysctl.c:314-331](../../kernel/kernel/sched/walt/sysctl.c#L314-L331)：

```c
case PER_TASK_BOOST:
	if (val < TASK_BOOST_NONE || val >= TASK_BOOST_END) { ret = -EINVAL; ... }
	wts->boost = val;
	if (val == 0) wts->boost_period = 0;
	break;
case PER_TASK_BOOST_PERIOD_MS:
	if (wts->boost == 0 && val) { ret = -EINVAL; ... }  /* 先设 boost 再设周期 */
	wts->boost_period = (u64)val * 1000 * 1000;
	wts->boost_expires = sched_clock() + wts->boost_period;
	break;
```

两个要点：
1. **顺序敏感**：必须先写 `sched_per_task_boost` 再写 `..._period_ms`，否则 `-EINVAL`。
2. `[反直觉]` **period 用 `sched_clock()`，而惰性过期用 `walt_sched_clock()`**
   （[walt.h:660](../../kernel/kernel/sched/walt/walt.h#L660)）。两者在 suspend 时的行为不同
   （`walt_sched_clock()` 冻结，见 [walt.c:88-98](../../kernel/kernel/sched/walt/walt.c#L88-L98)），
   因此**设备休眠会让实际的 boost 时长比用户设定值更长**。
   同一文件里 `set_task_boost()` 用的是 `walt_sched_clock()`，与 sysfs 路径**不一致**。

**内核 API**：`set_task_boost(int boost, u64 period)`
[walt.c:109](../../kernel/kernel/sched/walt/walt.c#L109)，作用于 **`current`**：

```c
/*@boost:should be 0,1,2.*/
/*@period:boost time based on ms units.*/
```

以 `EXPORT_SYMBOL(set_task_boost)` 导出（[walt.c:126](../../kernel/kernel/sched/walt/walt.c#L126)）。
`[待确认]` **本树内没有任何调用者**（`grep -rn set_task_boost kernel/` 只有定义与导出）。
注释说 `boost` 应为 0/1/2，但代码校验的是 `[TASK_BOOST_NONE, TASK_BOOST_END)`，
即 3（`STRICT_MAX`）也合法——**注释与代码不一致**。
`[推测]` 它是给某个 out-of-tree 模块（智能指针/输入子系统类）用的，本仓库不含该模块。

### 4.3 消费点：boost 值如何改变行为

`per_task_boost()` 的结果被 placement 大量消费：

| 消费点 | 位置 | 语义 |
|---|---|---|
| `walt_get_indicies()` | [walt_cfs.c:233](../../kernel/kernel/sched/walt/walt_cfs.c#L233) | `> TASK_BOOST_ON_MID` → 直接从最大簇开始找，不做能量评估 |
| `walt_get_indicies()` | [walt_cfs.c:248](../../kernel/kernel/sched/walt/walt_cfs.c#L248) | 任意非零 boost → `order_index = 1`（跳过小簇） |
| `walt_should_reject_fbt_cpu()` | [walt_cfs.c:363](../../kernel/kernel/sched/walt/walt_cfs.c#L363) | `num_mvp_tasks > 0` 且非 `STRICT_MAX` 时拒绝该 CPU |
| `task_fits_max()` | [walt.h:825](../../kernel/kernel/sched/walt/walt.h#L825) | 中簇：`task_boost > TASK_BOOST_ON_MID` 才算放不下 |
| `walt_should_kick_upmigrate()`（lb） | [walt_lb.c:248](../../kernel/kernel/sched/walt/walt_lb.c#L248) | `STRICT_MAX` + RTG → 禁止下行迁移 |
| `walt_get_mvp_task_prio()` | [walt_cfs.c:1233](../../kernel/kernel/sched/walt/walt_cfs.c#L1233) | `STRICT_MAX` → `WALT_TASK_BOOST_MVP` 抢占优先级 |

所以 `TASK_BOOST_ON_MID` 是"别放小核"，`TASK_BOOST_ON_MAX` 是"只在最大簇"。

### 4.4 binder 链上的 boost 传递（`STRICT_MAX`）

这是 per-task boost 里最精巧的一处——由两个 vendor hook 实现，
注册关系见 [02-integration-model.md](../00-overview/02-integration-model.md)：

- `binder_set_priority_hook()` [walt_cfs.c:1191](../../kernel/kernel/sched/walt/walt_cfs.c#L1191)：
  当**发起方** `current` 是 `STRICT_MAX` 且事务需要回复时，
  把服务端线程的**原 boost 值**存进 `bndrtrans->android_vendor_data1`，再把服务端线程**提升**为 `STRICT_MAX`。

```c
if (bndrtrans && bndrtrans->need_reply && current_wts->boost == TASK_BOOST_STRICT_MAX) {
	bndrtrans->android_vendor_data1 = wts->boost;   /* 存档 */
	wts->boost = TASK_BOOST_STRICT_MAX;             /* 提升 */
}
```

- `binder_restore_priority_hook()` [walt_cfs.c:1207](../../kernel/kernel/sched/walt/walt_cfs.c#L1207)：
  事务完成后**还原**存档值。

`[反直觉]` **"升级后的 binder 线程"没有独立的超时**——它靠服务端自己 `wts->boost_period` 的惰性过期，
而 `wts->boost` 此刻被覆写成了 `STRICT_MAX`，**原值只在 binder_transaction 结构里**。
如果事务与超时交错，恢复出来的可能是已被清 0 的旧值。这是 §7 登记的问题。

---

## 5. `wts->load_boost`：负载侧的 boost

与 `wts->boost`（改变**去哪**）完全不同，`load_boost` 改变的是**这个任务被算成多重**。
它只有一个消费点：`scale_exec_time()` [walt.c:1566](../../kernel/kernel/sched/walt/walt.c#L1566)：

```c
delta = (delta * wrq->task_exec_scale) >> SCHED_CAPACITY_SHIFT;

if (wts->load_boost && wts->grp && wts->grp->skip_min)
	delta = (delta * (1024 + wts->boosted_task_load) >> 10);

return delta;
```

三个条件必须**同时**成立，缺一不可——这是最容易读漏的一行：

1. `wts->load_boost != 0` —— 用户显式设置过；
2. `wts->grp != NULL` —— 任务在某个 RTG 里；
3. `wts->grp->skip_min` —— **该 RTG 开启了"不下放小核"**。

> **`[反直觉]` 第 3 条是本机制最反直觉的地方。**
> 对一个**不在 RTG 里**（或所在 RTG 未设 `skip_min`）的任务设置 `task_load_boost`，
> **完全不产生任何效果**，但 sysfs 写入会成功返回。
> 调试时看到"设了没反应"首先要查 `grp` 和 `skip_min`。

`boosted_task_load` 的换算在 `sched_task_handler()` [sysctl.c:362-367](../../kernel/kernel/sched/walt/sysctl.c#L362-L367)：

```c
case LOAD_BOOST:
	if (pid_and_val[1] < -90 || pid_and_val[1] > 90) { ret = -EINVAL; ... }
	wts->load_boost = val;
	if (val)
		wts->boosted_task_load = mult_frac((int64_t)1024, (int64_t)val, 100);
	else
		wts->boosted_task_load = 0;
```

- 用户写的是**百分比**，范围 `[-90, 90]`（唯一允许负值的参数——
  `sched_task_handler` 里 `if (param != LOAD_BOOST && val < 0) return -EINVAL;`）；
- 内部存的是 `1024 * val / 100`，于是 `delta *= (1024 + load) / 1024 = 1 + val/100`。
  写 `50` → 负载放大 1.5 倍；写 `-50` → 缩小到 0.5 倍。

sysfs 节点是 `task_load_boost` [sysctl.c:967-973](../../kernel/kernel/sched/walt/sysctl.c#L967-L973)。
字段同样在 `__sched_fork_init()` 中清零 [walt.c:2344-2345](../../kernel/kernel/sched/walt/walt.c#L2344-L2345)。

`[推测]` 这个机制的设计意图是给"轻负载但关键"的任务**人为抬高 demand**，
从而触发上行迁移和更高的频率请求——即"用假的负载换取真实的资源"。
它作用在**低频归一化之后**，所以放大的是"参考频率下的等效时间"。

注意 `scale_exec_time()` 被 10+ 处调用（[walt.c:1749](../../kernel/kernel/sched/walt/walt.c#L1749) 起），
因此 load_boost 会同时污染 `curr_runnable_sum`、`prev_runnable_sum`、`pred_demand` 的输入，
进而影响 demand、colocation、频率——**影响面远大于 `wts->boost`**。
详见 [window-model.md](01-window-model.md)。

---

## 6. input boost（`input-boost.c`）

### 6.1 数据通路：从触摸到 `freq_qos`

```
input 子系统 (EV_ABS / BTN_TOUCH / EV_KEY)
   └─ inputboost_input_event()          [input-boost.c:163]   过滤 + 限流
        └─ queue_work(input_boost_work)  → wq "inputboost_wq" (WQ_HIGHPRI)
             └─ do_input_boost()         [input-boost.c:129]
                  ├─ 每 CPU 设 sync_info->input_boost_min = sysctl_input_boost_freq[cpu]
                  ├─ update_policy_online() → boost_adjust_notify()
                  │     └─ freq_qos_update_request(&qos_req[cpu], ib_min)   ← 真正的频率下限
                  ├─ 可选：sched_set_boost(sysctl_sched_boost_on_input)
                  └─ queue_delayed_work(input_boost_rem, sysctl_input_boost_ms)
                       └─ do_input_boost_rem()  [input-boost.c:106]
                            ├─ input_boost_min = 0 全部
                            ├─ update_policy_online()
                            └─ 若本模块开过 sched boost → sched_set_boost(0)
```

### 6.2 事件过滤与限流（`inputboost_input_event()`）

```c
for_each_possible_cpu(cpu)
	if (sysctl_input_boost_freq[cpu] > 0) { enabled = 1; break; }
if (!enabled) return;                      /* 全 0 则彻底旁路，零开销 */

now = ktime_to_us(ktime_get());
if (now - last_input_time < MIN_INPUT_INTERVAL) return;   /* 150ms 限流 */
if (work_pending(&input_boost_work)) return;              /* 已有在跑 */
```

三条闸门，注意 `MIN_INPUT_INTERVAL = 150 * USEC_PER_MSEC`（[input-boost.c:57](../../kernel/kernel/sched/walt/input-boost.c#L57)）
= **150ms**，比默认 boost 时长 40ms 长得多。
`[反直觉]` 这意味着**连续滑动屏幕时，每 150ms 只有第一次触摸能续上 boost**——
中间会有最多 110ms 的"裸奔"窗口，boost 实际是**周期性脉冲**而非持续生效。
`[待确认]` 这是否是有意为之（抑制抖动）需要在真机上抓 freq 曲线确认。

`inputboost_ids[]` [input-boost.c:227](../../kernel/kernel/sched/walt/input-boost.c#L227) 匹配三类设备：
多点触控屏、触摸板、键盘（`EV_KEY`）。
`[反直觉]` 键盘也在列表里，且 `inputboost_input_event()` **不区分事件类型**——
任何来自这些设备的事件都会触发 boost。

### 6.3 频率下限的实现：per-CPU `freq_qos_request`

初始化在 `input_boost_init()` [input-boost.c:264](../../kernel/kernel/sched/walt/input-boost.c#L264)：

- 建 wq `inputboost_wq`（`WQ_HIGHPRI`，flags 0）；
- 对**每个 possible CPU** 取其 `cpufreq_policy`，加一个 `FREQ_QOS_MIN` 请求
  [input-boost.c:289-290](../../kernel/kernel/sched/walt/input-boost.c#L289-L290)：
  ```c
  freq_qos_add_request(&policy->constraints, req, FREQ_QOS_MIN, policy->min);
  ```
  初值即 policy 当前的 min，因此**初始不改变任何东西**。

实际抬升在 `boost_adjust_notify()` [input-boost.c:61](../../kernel/kernel/sched/walt/input-boost.c#L61)：
`freq_qos_update_request(req, ib_min)`——`ib_min` 为 0 时即"撤销约束"。

**这是 input boost 与全局 boost 的本质区别**：全局 boost 走的是 **load → governor → 频率**的
软信号链，input boost 走的是 **cpufreq 的 QoS 硬约束**，会直接碾压 governor 的计算结果。
`[反直觉]` 因此 input boost 生效时，`walt` governor 的输出**被无视**，
`CPUFREQ_REASON_*` 里的任何 reason 都不再反映真实频率来源。
与 schedutil 的 iowait boost 对比见 [../01-baseline/schedutil.md](../01-baseline/03-schedutil.md)。

`update_policy_online()` [input-boost.c:82](../../kernel/kernel/sched/walt/input-boost.c#L82)
遍历 online CPU 并**用 `cpumask_andnot` 剔除同 policy 的 CPU**，保证每个 policy 只通知一次。

### 6.4 与全局 boost 的联动：`sysctl_sched_boost_on_input`

`do_input_boost()` [input-boost.c:150-157](../../kernel/kernel/sched/walt/input-boost.c#L150-L157)：

```c
if (sysctl_sched_boost_on_input > 0) {
	ret = sched_set_boost(sysctl_sched_boost_on_input);
	if (ret) pr_err(...);
	else     sched_boost_active = true;
}
```

`sysctl_sched_boost_on_input` 定义于 [sysctl.c:51](../../kernel/kernel/sched/walt/sysctl.c#L51)，
`[反直觉]` **它没有在任何初始化路径里被赋值**（`walt_tunables()` 只设了
`sysctl_input_boost_ms = 40` 和 `input_boost_freq[] = 0`，
[sysctl.c:1143-1147](../../kernel/kernel/sched/walt/sysctl.c#L1143-L1147)），
因此是一个 **BSS 零初始化 = 0** 的全局量。
即：**默认情况下 input boost 只抬频率下限，不启用全局 sched boost**；
要靠用户态显式写 `/proc/sys/walt/input_boost/sched_boost_on_input`。

`do_input_boost_rem()` [input-boost.c:121-126](../../kernel/kernel/sched/walt/input-boost.c#L121-L126)
只在 `sched_boost_active` 为真时才 `sched_set_boost(0)`——
即**"谁开谁关"**，不会误伤别的子系统开的 boost。

### 6.5 生命周期与遗留问题

`input_boost_init()` 由 `walt_init()` 无条件调用 [walt.c:5129](../../kernel/kernel/sched/walt/walt.c#L5129)，
**返回值被丢弃**——即使 `cpufreq_cpu_get()` 失败返回 `-ESRCH`，`walt_init()` 也继续往下走。
`[待确认]` 此时 `input_boost_wq` 已创建但 handler 未注册，是"半初始化"状态。

**没有 `input_boost_exit()` / `_remove()`**（`grep -rn input_boost_exit` 返回空）——
这是**设计如此**：本模块不可卸载。对应的，`inputboost_input_disconnect()`
[input-boost.c:220](../../kernel/kernel/sched/walt/input-boost.c#L220) 只在设备热拔时被调。

**死代码清单**（读这份文件时会浪费时间的部分）：
`input_boost_attr_rw` / `show_one` / `store_one` 三个宏
（[input-boost.c:21-40](../../kernel/kernel/sched/walt/input-boost.c#L21-L40)）
和全局 `input_boost_kobj` [input-boost.c:263](../../kernel/kernel/sched/walt/input-boost.c#L263)
**全都没有任何引用**——它们是从 sysfs 版本的古早实现里残留下来的。
本树的配置入口**只有 sysctl**（`/proc/sys/walt/input_boost/*`），
**没有 `input_boost_*` 形式的 kobject 属性节点**。

### 6.6 sysfs / procfs 节点汇总

`input_boost_sysctls[]` [sysctl.c:555](../../kernel/kernel/sched/walt/sysctl.c#L555)，
挂在 `walt_table[]` 的 `input_boost` 子目录下 [sysctl.c:913-917](../../kernel/kernel/sched/walt/sysctl.c#L913-L917)：

| 路径 | 变量 | 默认 | 范围 | 语义 |
|---|---|---|---|---|
| `/proc/sys/walt/input_boost/input_boost_ms` | `sysctl_input_boost_ms` | **40** | `[0, 100000]` | boost 持续毫秒数 |
| `/proc/sys/walt/input_boost/input_boost_freq` | `sysctl_input_boost_freq[8]` | **全 0** | `[0, INT_MAX]` | 每 CPU 频率下限（kHz）；全 0 = 功能关闭 |
| `/proc/sys/walt/input_boost/sched_boost_on_input` | `sysctl_sched_boost_on_input` | **0** | `[0, INT_MAX]` | 触摸时顺带开启的全局 boost 类型 |

`maxlen = sizeof(unsigned int) * 8` 对应 8 个数的数组，写格式是 8 个空格分隔的整数。
`[反直觉]` 它是**按 CPU 而非按簇**索引的——SM8550 上同一簇的多个 CPU 通常要写相同值，
但内核**不做去重也不校验一致性**，写错了不会报错，只会出现同簇内频率约束互相打架。

---

## 7. 陷阱清单与遗留问题

### 7.1 读代码时的坑（汇总）

| # | 坑 | 依据 |
|---|---|---|
| 1 | `boost.c` 无 kthread / wq / timer，是纯 refcount 状态机 | `grep kthread boost.c` 为空 |
| 2 | `sched_boost` 开机默认为 1（FT boost），不是 0 | [walt.c:5131](../../kernel/kernel/sched/walt/walt.c#L5131)、[boost.c:296](../../kernel/kernel/sched/walt/boost.c#L296) |
| 3 | `RESTRAINED_BOOST` 的 `boost_policy` 是 `NONE`，不做放置倾斜 | [boost.c:74-77](../../kernel/kernel/sched/walt/boost.c#L74-L77) |
| 4 | `CONSERVATIVE_BOOST` 的 enter/exit 是空函数 | [boost.c:108-114](../../kernel/kernel/sched/walt/boost.c#L108-L114) |
| 5 | 开任意 boost 会**顺带开启 Early Detection** | [walt.c:471](../../kernel/kernel/sched/walt/walt.c#L471) |
| 6 | FT boost 下 `task_sched_boost()` 对所有任务返回 true，cgroup 过滤失效 | [walt.h:517](../../kernel/kernel/sched/walt/walt.h#L517) |
| 7 | `load_boost` 必须同时满足 `grp && grp->skip_min` 才生效 | [walt.c:1572](../../kernel/kernel/sched/walt/walt.c#L1572) |
| 8 | sysfs 设 per-task boost period 用 `sched_clock()`，内核 API 用 `walt_sched_clock()` | [sysctl.c:330](../../kernel/kernel/sched/walt/sysctl.c#L330) vs [walt.c:118](../../kernel/kernel/sched/walt/walt.c#L118) |
| 9 | per-task boost 过期是**读路径惰性清理**，无锁 | [walt.h:655](../../kernel/kernel/sched/walt/walt.h#L655) |
| 10 | input boost 是 **freq_qos 硬约束**，会碾压 governor | [input-boost.c:73](../../kernel/kernel/sched/walt/input-boost.c#L73) |
| 11 | 触摸限流 150ms > boost 时长 40ms，boost 是脉冲而非持续 | [input-boost.c:57](../../kernel/kernel/sched/walt/input-boost.c#L57)、[sysctl.c:1143](../../kernel/kernel/sched/walt/sysctl.c#L1143) |
| 12 | `input-boost.c` 无 exit 路径，且有三个宏 + 一个 kobj 是死代码 | [input-boost.c:21-40](../../kernel/kernel/sched/walt/input-boost.c#L21-L40) |
| 13 | `sched_boost_disable_all()` 会清掉别的子系统的 refcount | [boost.c:225](../../kernel/kernel/sched/walt/boost.c#L225) |

### 7.2 `[待确认]` 登记

以下条目需要实验或额外读代码，已按 CONVENTIONS §3 要求标注：

1. **`set_task_boost()` 无树内调用者**，仅 `EXPORT_SYMBOL`。
   注释说 `boost` 应为 0/1/2，但代码接受 0..3。是否由某个 out-of-tree 模块使用？
2. **binder boost 与惰性过期的竞态**（§4.4）：
   `binder_restore_priority_hook()` 写他人 `wts->boost` 的同时，
   对方可能正在 `per_task_boost()` 里清 0。可达性未知。
3. **`input_boost_init()` 返回值被丢弃**（§6.5）：cpufreq policy 缺失时的降级行为未定义。
4. **input boost 的 150ms 限流意图**（§6.2）：是抑制抖动还是历史遗留？
5. **`sched_boost_disable_all()` 与 core_ctl 独立 refcount 的交互**（§3）：
   用户写 `sched_boost=0` 后，pipeline 的保核核数是否按预期回退？

---

## 相关文档

- [03-data-structures.md](../00-overview/03-data-structures.md) —— `walt_task_struct.boost*` / `load_boost` 字段、`walt_task_group.sched_boost_enable[]` 的字段级说明
- [02-integration-model.md](../00-overview/02-integration-model.md) —— binder set/restore priority 两个 vendor hook 的注册点
- [04-data-flow.md](../00-overview/04-data-flow.md) —— boost 在整体数据流中的位置
- [cpufreq.md](03-cpufreq.md) —— `walt` governor 如何消费 `sched_freq_aggr_en` 与 `freq_policy_load()`
- [placement.md](04-placement.md) —— `walt_get_indicies()` / `task_fits_max()` 中 boost 对候选簇顺序的影响
- [groups-and-clusters.md](06-groups-and-clusters.md) —— RTG、`skip_min`、pipeline（`load_boost` 的前置条件）
- [power-side.md](07-power-side.md) —— `core_ctl_set_boost()` 与 `core_ctl` 的状态机
- [rt-mvp.md](09-rt-mvp.md) —— `TASK_BOOST_STRICT_MAX` 在 MVP 抢占队列中的角色
- [window-model.md](01-window-model.md) —— `scale_exec_time()` 在窗口记账中的全部调用点
- [../01-baseline/schedutil.md](../01-baseline/03-schedutil.md) —— 与 schedutil iowait boost 的机制对比
- [observability.md](10-observability.md) —— `sched_set_boost` / `sched_load_to_gov` tracepoint 的抓取方法
