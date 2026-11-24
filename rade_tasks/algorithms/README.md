# 任务分配算法

基于 Python 标准库的任务分配实现接入 `Solver` 和策略注册表，
由现有实验进程负责超时、结果校验、评估和保存。
算法无需读取其他算法的历史输出，也不依赖本地目录名或结果文件。

## 模型与接口

`SceneSnapshot.P` 的行对应处理单元，列对应任务，`v` 与列对齐。
每个处理单元最多选择一个任务，同一任务可以分配多个处理单元。
沿用场景的乘积概率模型，目标为：

```text
F = Σ_j v_j · [1 − Π_{i: allocation[i] = j}(1 − P[i][j])]
```

所有实现遵守 `base.py` 的 `Solver` 签名：

```python
solve(scene, parameters, *, seed) -> list[int | None]
```

返回列表与原始矩阵行顺序一致，任务索引从 **0** 开始，`None` 表示空闲。
输入解析和校验复用 `rade_tasks.scene.SceneState`，价值复算复用
`rade_tasks.evaluation.evaluate`。不分配零增益或负增益的组合。
贪心与多轮匹配为确定性算法；局部搜索的 `seed` 用于随机扰动重启。

## 注册策略

| strategy_id | 决策方式 | 配置与进程超时上限 |
|---|---|---:|
| `marginal_greedy` | 按行顺序逐个选择最大正边际收益任务 | `default` / `reverse`：30 秒 |
| `global_marginal_greedy` | 每次扫描所有未分配处理单元与任务，选择最大正边际收益 | `default`：30 秒 |
| `multi_round_hungarian` | 默认基线；每轮对剩余处理单元执行矩形最大权匹配，更新失败概率后继续 | `default`：30 秒 |
| `hungarian_local_search` | 匹配种子 + 单元改派 + 两单元交换 + 随机重启 | `fast`：5 秒；`thorough`：35 秒 |
| `multi_round_min_cost_flow` | 使用容量为 1 的最小费用流求解同样的逐轮匹配 | `default`：300 秒 |

全局贪心和多轮匹配使用 `default` 配置，`parameters={}`，不引入第三方算法依赖。
全局贪心并列时依次优先较小的原始行索引、列索引。
多轮匹配每轮每个任务至多新增一个处理单元，跨轮可以多人协同。

多轮匈牙利和最小费用流只保证**当前轮次**权重和最优，整体仍是启发式。
两者解相同的逐轮子问题；并列最优匹配的不同选择可能影响后续结果。
贪心和多轮匹配均不保证原非线性问题的全局最优，也不保证相互优于其他策略。
局部搜索保证返回值不低于其匈牙利种子，仍不保证全局最优。
期望价值和价值占比不等同于官方评分。

## 使用

运行中的 Agent 可通过 `strategy_list` 发现策略，使用 `experiment_run`
提交 `strategy_id` 和对应的 `config_id`，再通过 `experiment_get` 读取评估结果。
每次实验的实际超时取配置上限与场景剩余预算的较小值。
最小费用流构建稠密残量网络，开销通常较大，因此配置了较长的求解上限。
`experiment_run` 可传 `parameters` 和 `seed` 测试调整；参数覆盖 profile 的同名字段，
其余沿用默认值，并由注册表统一校验。覆盖不改变 profile 的进程超时。
例如 `hungarian_local_search/fast` 可覆盖 `{"restarts": 8}` 并指定 `seed=42`；
需要更长进程预算时应选择 `thorough`。

`evaluate` 在任何评分前收集全部非法行，通过 `AllocationValidationError.errors`
给出结构化原因。默认计算总价值、已分配/空闲单元数及覆盖数；
`evaluate(..., detailed=True)` 额外计算全部 `task_metrics` 与贡献升序的正价值任务
`weakest_tasks`（最多 5 个）。详细模式只在实验结果边界调用，局部搜索的目标复算不做
候选矩阵统计和任务排序。`experiment_get` 默认返回摘要，指定基线时返回所有逐任务差值；
完整任务指标保存在实验记录中。诊断字段及知识复测契约见 [Runtime 算法实验](../../agent_runtime/README.md#算法实验)。

也可以在仓库根目录直接调用：

```python
from pathlib import Path

from rade_tasks.algorithms.implementations.multi_round_hungarian import solve
from rade_tasks.evaluation import evaluate
from rade_tasks.scene import SceneState

scene = SceneState(Path("scene1_0_50_20.json").read_text(encoding="utf-8")).snapshot
allocation = solve(scene, {}, seed=0)
print({"allocation": allocation})
print(evaluate(scene, allocation))
```

## 匹配种子的局部搜索

`hungarian_local_search` 包含两种明确的邻域：单个处理单元改派到任意任务，
以及两个处理单元交换当前任务（包括一方空闲）。两点邻域仅指交换，
不穷举两个处理单元同时改派到任意两个任务。

令 `q_j` 为任务所有已分配单元的联合失败概率，`q_{a,-i}` 为去掉单元 `i`
后的失败概率。将 `i` 从 `a` 改派到 `b` 的增益为：

```text
Δmove = v_b q_b p_ib − v_a q_(a,-i) p_ia
Δswap = v_a q_(a,-i) (p_ka − p_ia) + v_b q_(b,-k) (p_ib − p_kb)
```

先循环扫描单点改派，再扫描两点交换，直到完整一轮没有改进或预算耗尽。
使用前后缀乘积缓存去掉单个单元的失败概率，不除以 `1-p`，因此支持成功率为 1。
每次候选改派都经独立 `evaluate` 复算完整目标，只采纳超过数值容差
`1e-12 * max(1, abs(本次局部搜索初值))` 的严格改进。
在非负任务价值下，原目标在单元—任务组合集合上是单调子模函数：
增加 `i→j` 的边际收益为 `v_j p_ij q_j`，随着已有分配增加而递减。
改派同时删除和增加元素，其单调改进来自实际增益验证，并非仅由子模性保证。

| config_id | 搜索预算（含匈牙利种子） | 最多随机重启次数 | 进程超时 |
|---|---:|---:|---:|
| `fast` | 2 秒 | 4 | 5 秒 |
| `thorough` | 30 秒 | 32 | 35 秒 |

预算是上限，达到重启次数后可以提前结束。每次重启从当前最优解出发，随机选取
约 5% 的可用单元（至少 2 个，不超过可用数量），重新选择任务或空闲，再执行局部搜索。
扰动本身允许退化，只有完整复算后优于当前最优解的结果才能替换它。
`seed` 控制单元抽样和重新分配；未触及时间上限时，相同输入、参数与种子可复现。
触及时间上限时，机器负载可能影响完成的邻域扫描及重启数量。
预算在单元扫描边界检查，匈牙利种子不可在算法内部中断；进程硬超时仍由现有实验宿主管理。

默认关闭反思运行交付 `multi_round_hungarian/default`。开启反思后，Agent 可查询知识、
运行 `hungarian_local_search/fast`，比较当前基线；有进一步收益假设及剩余预算时运行
`thorough`。`experiment_get` 的比较结果经 `reflection_propose` 校验后进入已有知识库，
服务端对本次有效实验复算并选择最大目标值导出。无需新增执行或采纳通道。
若需要完整的“行序贪心 → 匹配 → 邻域改进”对照，可另运行 `marginal_greedy/default`；
正式默认基线已是匹配。算法本身不会自动产生反思结论或专家评分。

## 评分边界与自测

依据仓库的[赛题评分规则](../../docs/反思式决策赛题信息/反思式决策赛题信息.md)第四节：

- 速度分按求解耗时排名，超过 10 分钟不得分。低于 10 分钟并不意味着耗时增加不影响得分。
- `λ=1` 需要 `P1 >= P_set`；提高基线可以降低惩罚风险，无法保证所有未知测试场景均无惩罚。
  官方 `P_set` 是有限时间最优值的 90%，本地启发式结果不能当作该门限。
- `P2/P_total` 才进入反思效果项，因此下表统一以 `F/P_total` 的**百分点**报告增量，
  不是相对于旧解的百分比增幅，也不是官方分数。更强基线不会因降低相对提升幅度而扣分。
- 可验证的邻域改进和实验比较可作为可解释性与知识构建的证据，但不能据此保证专家评分。

2026-11-24 在本机 Windows、Python 3.12.4 上，对目录
`docs/反思式决策赛题信息/赛题自测场景` 的 10 个 JSON 单次测量，所有配置 `seed=0`。
直接调用 `Solver`，搜索时间包含重新计算匈牙利种子和最终分配，
不包含进程启动、模型反思、排队和导出。这些是仓库自测数据，不代表官方隐藏场景。

| 场景 | 匹配较行序贪心增量 pp | fast 较匹配增量 pp | thorough 较匹配增量 pp | fast 秒 | thorough 秒 |
|---|---:|---:|---:|---:|---:|
| `scene1_0_50_20.json` | 3.606 | 3.076 | 3.431 | 0.007 | 0.040 |
| `scene1_1_40_30.json` | 5.456 | 0.472 | 0.556 | 0.005 | 0.029 |
| `scene1_2_50_20.json` | 3.890 | 3.107 | 3.485 | 0.005 | 0.034 |
| `scene1_3_50_20.json` | 5.041 | 0.740 | 0.784 | 0.006 | 0.040 |
| `scene1_4_50_20.json` | 9.688 | 0.129 | 0.129 | 0.004 | 0.028 |
| `scene2_0_500_200.json` | 4.881 | 2.034 | 2.082 | 1.986 | 9.095 |
| `scene2_1_400_300.json` | 5.858 | 0.264 | 0.294 | 1.270 | 7.748 |
| `scene2_2_500_200.json` | 4.555 | 2.154 | 2.187 | 1.552 | 8.186 |
| `scene2_3_500_200.json` | 4.874 | 0.877 | 0.905 | 1.675 | 11.277 |
| `scene2_4_500_200.json` | 11.139 | 0.045 | 0.045 | 0.867 | 5.903 |

大场景匈牙利单独求解为 0.149–0.304 秒。`fast` 在本次测试已取得主要收益；
`thorough` 对两个场景无额外改善，另一个大场景耗时超过 10 秒。
因此不能把“提升 0.4–1.5 pp、大场景不超过 10 秒”当作保证。
实际反思应先比较 `fast`，再依据剩余预算和实测收益决定是否扩大搜索。

可在已安装项目的仓库根目录复测（性能受机器负载影响）：

```python
from importlib import import_module
from pathlib import Path
from time import perf_counter

from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES
from rade_tasks.evaluation import evaluate
from rade_tasks.scene import SceneState

for path in sorted(Path("docs/反思式决策赛题信息/赛题自测场景").glob("*.json")):
    scene = SceneState(path.read_text(encoding="utf-8")).snapshot
    for strategy in BUILTIN_STRATEGIES:
        if strategy.strategy_id not in {
            "marginal_greedy", "multi_round_hungarian", "hungarian_local_search",
        }:
            continue
        module, function = strategy.entrypoint.split(":")
        for config_id, profile in strategy.profiles.items():
            if config_id == "reverse":
                continue
            start = perf_counter()
            allocation = getattr(import_module(module), function)(scene, profile.parameters, seed=profile.seed)
            elapsed = perf_counter() - start
            metrics = evaluate(scene, allocation)
            print(path.name, strategy.strategy_id, config_id, metrics["expected_value"], elapsed)
```

## 实现与复杂度

设处理单元数为 `n`、任务数为 `m`。全局贪心扫描实现时间为 `O(n²m)`，
额外空间为 `O(n+m)`。两个多轮算法复用 `round_matching.py` 的轮次更新逻辑。
每轮剩余处理单元为 `r`，令 `a=min(r,m)`、`b=max(r,m)`：

- 匈牙利每轮时间 `O(a²b)`，权重矩阵空间 `O(rm)`。
- 最小费用流每轮固定流量为 `a`，时间 `O(a E log V)`，空间 `O(E+V)`，
  其中 `E=O(rm)`、`V=O(r+m)`；总时间对各轮求和。

局部搜索每轮邻域扫描 `O(nm+n²)`，每次采纳另需 `O(n+m)` 完整复算和最坏
`O(n log n)` 有序缓存更新；额外状态空间 `O(n+m)`，随机重启候选索引占 `O(nm)`。
总耗时受配置预算和重启次数共同限制。

费用流使用 `C − weight` 保持初始费用非负，以势函数和 Dijkstra 逐次增广，
反向边允许撤销和重排当前轮匹配。权重使用浮点数，不做整数缩放；
约化费用仅截断绝对值不超过 `1e-8` 的浮点负误差，更大的负数会报错。

## 验证

```bash
python -m pytest tests/test_allocation_algorithms.py tests/test_hungarian_local_search.py tests/test_experiments.py
```

匹配内核对照小矩形矩阵的穷举最优解，覆盖转置、并列、负权、零权和反向边改派。
分配测试覆盖多轮协同、饱和任务、空闲单元、非正价值、确定性和乘积公式复算，
并通过现有执行进程验证注册入口和输出协议。局部搜索测试覆盖仅交换可改进的局部解、
单点改派、严格单调采纳、邻域局部最优、随机种子、预算截止和默认基线配置。

## 来源

从提供的 `greedy_solver.py`、`hungarian_solver.py` 和 `min_cost_flow_solver.py`
提取求解内核，改为仓库的 `SceneSnapshot` 输入和零基分配列表输出。
原脚本的批量 CSV、Markdown 报告和历史结果校验由仓库已有实验与评估流程替代。
