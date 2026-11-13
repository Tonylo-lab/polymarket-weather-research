# Polymarket 天气策略研究

**Research project — not proven consistently profitable.**  
**研究项目：尚未证明能够稳定盈利。**

[English README](README.md) · [参与入口](#参与入口) · [实际实现](docs/IMPLEMENTATION.md) · [数据与权限](docs/DATA.md)

这是个人天气市场策略的离线研究发布版，保留失败实验，方便其他开发者检查机制、改进验证。实验由项目所有者通过 **Kiro／Opus 5.5 辅助开发**；这是所有者提供的工具／模型署名，不是策略名称，也不是独立核实的模型来源证明。

### 参与入口

第一次参与可以从 [#1：离线入门流程验证](https://github.com/Tonylo-lab/polymarket-weather-research/issues/1) 开始，首个贡献只验证一个操作系统也欢迎。希望参与研究，可以选择下表中的具体任务；每个 Issue 都列有代码入口和验收条件。

| 任务 | 从哪里开始 | 标签 |
| --- | --- | --- |
| [#1 验证 Windows、macOS 或 Linux 的离线入门流程](https://github.com/Tonylo-lab/polymarket-weather-research/issues/1) | 运行合成演示／测试，完善中英文操作说明 | `good first issue`、`documentation` |
| [#2 对比实际成本与资金预留的数量选择](https://github.com/Tonylo-lab/polymarket-weather-research/issues/2) | 使用合成盘口制作确定性的报价对照工具 | `help wanted`、`enhancement` |
| [#3 校验结算证据记录](https://github.com/Tonylo-lab/polymarket-weather-research/issues/3) | 版本化证据格式及离线结构／摘要校验器 | `help wanted`、`enhancement` |
| [#4 为 H2 新建显式盘口质量门槛对照](https://github.com/Tonylo-lab/polymarket-weather-research/issues/4) | 增加点差／深度审核，保留当前 H2 行为 | `help wanted`、`enhancement` |

先在对应 Issue 留言说明准备采用的方法或验证的平台，再按 [CONTRIBUTING.md](CONTRIBUTING.md) 提交小范围 PR。不需要钱包或私有数据；工作保持离线，未知结算继续未知，新行为实验不沿用历史收益。

### 原策略做什么？

`baseline` 使用 **Main＋Protection**。Main 是归一化市场价格最高的温度档，Protection 是相邻档中信号较高的一档。这些是市场价格信号，不是经过校准的天气预测。演示基线将原决策规则放在与实验相同的 true-fee 研究适配中运行，不代表复现实盘执行。

例如 Main 为 31°C、Protection 为 30°C：Main 信号跨过 60%、70%、80%、90% 时，策略尝试按事件参考本金的 20%、35%、50%、60% 累计投入。200 单位参考本金对应 40／70／100／120 单位目标，仍须通过资金、费用、深度、最小订单与 Main 收益审核，不能把目标当作实际成交。

保护切换和 Main 翻转分别审核。原前置条件实际进入三档 Minimax 计算后，比较可执行 Minimax 与直接卖出，在同一 Main／旧保护／新保护情景中选择最差净损益较高者，相同选 SELL。计算全场累计 BUY 含费成本与此前净回款；不可卖余仓保留各情景价值。**这不是全温度档亏损保证。**

普通卖出要求符合条件的买盘至少为 0.998。首次只买 Main、保护从未成交的事件，首次 Main 翻转另有持久退出；Minimax 选择的清仓也可使用更低的合法正价买盘。余仓继续保留，接近 0／1 的价格不能证明官方结算。详见[机制说明](docs/IMPLEMENTATION.md)。

### 辅助研究尝试了什么？

三个重点实验使用后续的 80／85／90% 阶梯、30% 累计上限和固定 200 单位参考预算，与原策略基线分开。

| 实验／实际机制 | 历史净变化，200 单位起始 | 复现状态 |
| --- | ---: | --- |
| Baseline：原阶梯 | -159.8109 | 仅历史记录 |
| S2：两个不同决策时间戳的 Main 盘口质量确认，再检查双腿入场 | +2.0771 | 历史记录；合成引擎运行通过 |
| H2：香港时间 14:00 后首次入场、双腿可执行检查 | +7.3990 | 历史记录；合成引擎运行通过 |
| A1：H2＋首次分配以外的小预算 BUY 过滤 | +17.1625 | 历史记录；合成引擎运行通过 |
| S3：Main 提前风险退出 | -12.0782 | 仅历史记录 |
| A2：去保护＋提前风险退出 | -103.9337 | 仅历史记录 |
| H2：一分钟执行延迟敏感性 | -6.3638 | 仅历史记录 |

**以上数字均为历史记录，本发布版未重新复现。** 结果文件记录香港 2026-08-19 至 2026-10-25 的 68 个事件窗口，使用逐档费用 rate 0.05／exponent 1、采样成交和混合结算口径。文件内标签计数为 54 个“official”、4 个假定赢家、10 个窗口末估值；本发布版没有独立证明其中 54 个标签。含估值回流的净变化不一定是实际结算利润。原始输入与完整历史版本绑定未随仓库提供。[全部候选汇总](results/historical_summary.json)同时保留失败和正数记录。

[候选实验表](docs/EXPERIMENTS.md)列出全部 34 个已记录配置。这些结果不足以证明持续盈利：原基线在历史回放中亏损较大；样本小且反复研究；正数候选受成交与结算假设影响；H2 的一分钟延迟记录转为负数。尚未证实具体因果解释或未来优势。

### 实际代码与历史描述的差异

- **H2 未执行报告所称的 spread ≤ 0.03／depth ≥ 5 显式门槛**；时间分支直接进入双腿可执行检查，本版保留实际行为。
- **A1 不只影响普通加仓**：首次分配包装器以外，每个低于 2 单位预算的 BUY 都检查，不按 reason 排除保护、修复或其他买入。
- **S2／H2／A1 使用 `v3_true_fee`**，以实际成本决定数量预算。仓库另保留资金预留门控的 `v3_gated_exec`，没有静默替换到这些实验中。
- 同帧成交、重复盘口深度处理、未知赢家和假设的窗口末资金回流都影响解释；冻结脚本也不是完整复现环境。详见[差异与证据](docs/IMPLEMENTATION.md)。

### 安装与离线运行

使用 **Python 3.11+**，运行和测试仅需 Python 标准库。在仓库目录执行：

```sh
python -m venv --without-pip .venv
```

Windows PowerShell 用 `.venv\Scripts\Activate.ps1` 激活；macOS／Linux 用 `source .venv/bin/activate`。然后运行：

```sh
python -B -m research.run
python -B -m unittest discover -v
```

演示在临时目录生成两个刻意合成的 2030 年事件窗口，调用四套策略。结算标签为 **unknown**，输出明确标注窗口末估值，不冒充已确认利润。不下载数据，不需要钱包、凭据或私有数据库。导入只安装离线防护，不创建文件；HTTP 传输、签名 SDK、凭据存储、Bot 启动器和服务配置均未提供。

使用有权使用的本地数据时，按[输入格式](docs/DATA.md)准备后运行：

```sh
python -B -m research.run --input samples/your_bundle/manifest.json --strategy H2 A1
```

统一入口是 `research.run`；`scripts/` 保留原计算驱动与适配层，不是实盘 Bot 命令。运行前核对输入摘要；账户与输出使用临时目录，其他允许写入区域仅为 `research_outputs/`。

### 数据、许可与社区参与

仓库不提供真实盘口原始档案、HKO 原始数据或真实账户数据库。Pendulum Flow 当前条款与旧注释不同；HKO 条款取决于具体来源；直接采集 Polymarket 数据的再分发权限尚未确认。数据需按各自条款取得，代码许可证不覆盖第三方数据。详见[来源与许可决定](docs/DATA.md)。

所有者提供的项目代码与新编合成示例采用 [MIT](LICENSE)，没有打包第三方 SDK 或采集器实现。见 [THIRD_PARTY.md](THIRD_PARTY.md)。`live_strategy/` 保留历史命名，但只包含离线计算。

从[参与入口](#参与入口)选择具体任务，或查看[开放 Issues](https://github.com/Tonylo-lab/polymarket-weather-research/issues)。见[贡献说明](CONTRIBUTING.md)与[发布检查和限制](docs/RELEASE_CHECKS.md)。
