# 基础任务分配算法

三种基于 Python 标准库的任务分配实现已接入 `Solver` 和策略注册表，
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
这些算法均为确定性算法，`seed` 为统一接口保留，不影响结果。

## 注册策略

| strategy_id | 决策方式 | default 超时上限 |
|---|---|---:|
| `marginal_greedy` | 仓库原有基线：按行顺序逐个选择最大正边际收益任务 | 30 秒 |
| `global_marginal_greedy` | 每次扫描所有未分配处理单元与任务，选择最大正边际收益 | 30 秒 |
| `multi_round_hungarian` | 每轮对剩余处理单元执行矩形最大权匹配，更新失败概率后继续 | 30 秒 |
| `multi_round_min_cost_flow` | 使用容量为 1 的最小费用流求解同样的逐轮匹配 | 300 秒 |

新增策略只提供 `default` 配置，`parameters={}`，不引入第三方算法依赖。
全局贪心并列时依次优先较小的原始行索引、列索引。
多轮匹配每轮每个任务至多新增一个处理单元，跨轮可以多人协同。

多轮匈牙利和最小费用流只保证**当前轮次**权重和最优，整体仍是启发式。
两者解相同的逐轮子问题；并列最优匹配的不同选择可能影响后续结果。
三种新增算法均不保证原非线性问题的全局最优，也不保证优于原有基线。
期望价值和价值占比不等同于官方评分。

## 使用

运行中的 Agent 可通过 `strategy_list` 发现策略，使用 `experiment_run`
提交 `strategy_id` 和 `config_id="default"`，再通过 `experiment_get` 读取评估结果。
每次实验的实际超时取配置上限与场景剩余预算的较小值。
最小费用流构建稠密残量网络，开销通常较大，因此配置了较长的求解上限。

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

## 实现与复杂度

设处理单元数为 `n`、任务数为 `m`。全局贪心扫描实现时间为 `O(n²m)`，
额外空间为 `O(n+m)`。两个多轮算法复用 `round_matching.py` 的轮次更新逻辑。
每轮剩余处理单元为 `r`，令 `a=min(r,m)`、`b=max(r,m)`：

- 匈牙利每轮时间 `O(a²b)`，权重矩阵空间 `O(rm)`。
- 最小费用流每轮固定流量为 `a`，时间 `O(a E log V)`，空间 `O(E+V)`，
  其中 `E=O(rm)`、`V=O(r+m)`；总时间对各轮求和。

费用流使用 `C − weight` 保持初始费用非负，以势函数和 Dijkstra 逐次增广，
反向边允许撤销和重排当前轮匹配。权重使用浮点数，不做整数缩放；
约化费用仅截断绝对值不超过 `1e-8` 的浮点负误差，更大的负数会报错。

## 验证

```bash
python -m pytest tests/test_allocation_algorithms.py tests/test_experiments.py
```

匹配内核对照小矩形矩阵的穷举最优解，覆盖转置、并列、负权、零权和反向边改派。
分配测试覆盖多轮协同、饱和任务、空闲单元、非正价值、确定性和乘积公式复算，
并通过现有执行进程验证注册入口和输出协议。

## 来源

从提供的 `greedy_solver.py`、`hungarian_solver.py` 和 `min_cost_flow_solver.py`
提取求解内核，改为仓库的 `SceneSnapshot` 输入和零基分配列表输出。
原脚本的批量 CSV、Markdown 报告和历史结果校验由仓库已有实验与评估流程替代。
