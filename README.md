# Polymarket Weather Research

**Research project — not proven consistently profitable.**  
**研究项目：尚未证明能够稳定盈利。**

[中文说明](README.zh-CN.md) · [Start contributing](#start-contributing) · [Actual implementation](docs/IMPLEMENTATION.md) · [Data and permissions](docs/DATA.md)

An offline research release of a personal weather-market strategy, including unsuccessful experiments. The aim is to make the mechanisms inspectable and invite better tests, not to sell a trading system. Experiments were developed by the project owner with **Kiro / Opus 5.5 assistance**; these are tool/model attributions supplied by the owner, not strategy names or independently verified model provenance.

### Start contributing

New here? Start with [#1: offline quickstart verification](https://github.com/Tonylo-lab/polymarket-weather-research/issues/1); one operating system per initial contribution is welcome. For research work, choose one of the scoped tasks below. Each issue includes starting files and acceptance criteria.

| Task | Entry point | Labels |
| --- | --- | --- |
| [#1 Verify the offline quickstart on Windows, macOS or Linux](https://github.com/Tonylo-lab/polymarket-weather-research/issues/1) | Run the synthetic demo/tests and improve both language guides | `good first issue`, `documentation` |
| [#2 Compare realized-cost and reserved-cost sizing](https://github.com/Tonylo-lab/polymarket-weather-research/issues/2) | A deterministic quote-comparison tool using synthetic books | `help wanted`, `enhancement` |
| [#3 Validate settlement evidence records](https://github.com/Tonylo-lab/polymarket-weather-research/issues/3) | A versioned schema and offline structural/hash validator | `help wanted`, `enhancement` |
| [#4 Add a separately versioned H2 quality-gated control](https://github.com/Tonylo-lab/polymarket-weather-research/issues/4) | Explicit spread/depth checks, preserving current H2 behavior | `help wanted`, `enhancement` |

Comment on the issue with your proposed approach or platform, then submit a small PR following [CONTRIBUTING.md](CONTRIBUTING.md). No wallet or private dataset is needed. Keep work offline, unknown labels unknown, and new behavioral experiments separate from historical returns.

### What is the original strategy?

`baseline` uses **Main + Protection**. Main is the highest normalized market-price signal; Protection is the more likely adjacent temperature bucket. These signals are market prices, not a calibrated weather forecast. The demo baseline runs the original decision rules under the same true-fee research adapter as the experiments; it does not reproduce production execution.

For example, if 31°C is Main and 30°C is Protection, the engine may buy both as the Main signal crosses 60%, 70%, 80% and 90%. The cumulative event deployment targets are 20%, 35%, 50% and 60% of the event's reference bankroll, subject to the existing affordability, fee, depth, minimum-order and Main-profit checks. A 200-unit reference therefore gives targets of 40 / 70 / 100 / 120 units, not guaranteed executed orders.

Protection switches and Main flips have separate checks. After the original preconditions reach a three-outcome Minimax calculation, the baseline compares executable Minimax against direct liquidation: choose the higher worst net P&L across the same Main / old Protection / new Protection scenarios; ties choose SELL. All event BUY costs and past net proceeds count, and unsellable residuals keep their scenario payouts. This is **not an all-outcomes loss guarantee**.

Ordinary exits require eligible bids at or above 0.998. An uncovered initial Main-only position has a separate persistent exit on its first Main flip. Minimax-selected liquidation can also sell at lower legal positive bids. Residuals are retained; prices near zero or one do not establish an official result. See [the mechanism map](docs/IMPLEMENTATION.md).

### What was tried?

The three main experiments use a later 80 / 85 / 90% ladder capped at 30%, with fixed 200-unit reference sizing. They are separate from the original baseline.

| Experiment / actual mechanism | Recorded net change, 200-unit start | Reproduction status |
| --- | ---: | --- |
| Baseline: original ladder | -159.8109 | Historical only |
| S2: Main-book quality on two distinct decision ticks, then both-leg entry | +2.0771 | Historical only; synthetic engine runs |
| H2: first entry at/after 14:00 HKT, both-leg feasibility | +7.3990 | Historical only; synthetic engine runs |
| A1: H2 + reject small BUY budgets outside first-entry allocation | +17.1625 | Historical only; synthetic engine runs |
| S3: early Main risk exit | -12.0782 | Historical only |
| A2: suppress Protection + early risk exit | -103.9337 | Historical only |
| H2 with one-minute execution sensitivity | -6.3638 | Historical only |

**Every number above is a historical record, not rerun in this release.** The files describe 68 Hong Kong event windows, 2026-08-19 through 2026-10-25, with per-level fees using rate 0.05 / exponent 1, sampled execution and a mixed settlement basis. Their recorded labels are 54 “official”, 4 assumed and 10 window-end proxies; this release has not independently verified those 54 labels. A net change including proxy release is not necessarily realized settled profit. Raw inputs and complete historical version bindings are not bundled. [All candidate aggregates](results/historical_summary.json) preserve failures as well as positive records.

The [candidate register](docs/EXPERIMENTS.md) covers all 34 recorded configurations. These results cannot establish consistent profitability: the baseline lost heavily in the recorded replay; tests reuse a small, already studied sample; positive candidates depend on execution and settlement assumptions; and the delayed-execution H2 record turns negative. No causal explanation or future edge has been established.

### What the code actually does

- **H2 does not apply the explicit spread ≤ 0.03 / depth ≥ 5 gate** claimed in historical descriptions. Its time branch goes directly to both-leg feasibility. This release preserves that behavior.
- **A1 is broader than ordinary add-ons**: it checks each BUY budget below 2 units outside the first-entry allocation wrapper, regardless of reason. Protection, repair and other BUY paths can be affected.
- **S2 / H2 / A1 use `v3_true_fee`**, whose quantity budget uses realized cost. `v3_gated_exec` is included as a separate reservation-gated adapter; it is not silently substituted into these experiments.
- Same-frame fills, repeated-book depth handling, unknown winners and hypothetical window-end capital release limit interpretation. Frozen files are not complete reproducible environments. [Evidence and differences](docs/IMPLEMENTATION.md).

### Run locally, offline

Use Python **3.11+**. Runtime and tests use only the Python standard library. Start in the repository directory:

```sh
python -m venv --without-pip .venv
```

Activate with `.venv\Scripts\Activate.ps1` in Windows PowerShell, or `source .venv/bin/activate` on macOS/Linux. Then:

```sh
python -B -m research.run
python -B -m unittest discover -v
```

The demo generates two deliberately synthetic 2030 event windows in a temporary directory and runs all four strategies. It uses **unknown** settlement labels and explicitly reports window-end valuation, not confirmed profit. Nothing is downloaded, and no wallet, credentials or private database are needed. Imports install an offline guard without creating files. HTTP transports, signing SDKs, credential stores, Bot launchers and service configuration are excluded.

For data you may lawfully use, follow the [bundle format](docs/DATA.md), then:

```sh
python -B -m research.run --input samples/your_bundle/manifest.json --strategy H2 A1
```

The supported entry point is `research.run`; retained `scripts/` modules supply the original computational drivers and adapters. They are not production Bot commands. Input hashes are checked. Outputs/accounts are temporary; the only other permitted write area is `research_outputs/`.

### Data, licence and contributions

No real market archive, HKO raw dataset or real account database is included. Pendulum Flow's current licence differs from an old local code comment; HKO conditions depend on the specific source, and direct Polymarket redistribution permission has not been confirmed. Obtain inputs under their own terms; the code licence does not license third-party data. [Sources and permission decisions](docs/DATA.md).

Owner-supplied project code and newly authored examples are released under [MIT](LICENSE). No third-party SDK or collector implementation is vendored. See [THIRD_PARTY.md](THIRD_PARTY.md). The preserved namespace `live_strategy/` contains only offline calculations; its name does not enable trading.

Choose a task from [Start contributing](#start-contributing), or browse [open issues](https://github.com/Tonylo-lab/polymarket-weather-research/issues). [Contribution guide](CONTRIBUTING.md) · [Release checks and limits](docs/RELEASE_CHECKS.md).
