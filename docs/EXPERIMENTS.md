# Candidate register / 候选实验表

All numbers are **historical records, not rerun in this release / 历史记录，本发布版未重新复现**. This register is an aggregate transcription, not a leaderboard or a claim that every stored run used the exact current source version. Mechanisms describe the retained configurations; complete historical dependency/version bindings are unavailable.

Common recorded basis: 68 Hong Kong windows, 2026-08-19–2026-10-25; fee rate 0.05 / exponent 1 at consumed levels; minute-sampled, same-frame execution with identical-book depth deduplication; 54 recorded official, 4 assumed and 10 window-end-proxy settlement labels. Official labels are not independently verified here. Each capital column is a continuous simulated account, not a real account. Only S2/H2/A1 and baseline are exposed by the supported synthetic runner; other configurations remain available for code inspection, without a claim of runtime reproduction.

| Candidate | Actual retained mechanism / 实际机制 | Net, 200 start | Net, 500 start | Reproduction |
| --- | --- | ---: | ---: | --- |
| C0_no_trade | No-trade control / 不交易对照 | +0.0000 | +0.0000 | Historical only |
| C1_original | Original ladder / 原阶梯 | -159.8109 | -424.0808 | Historical only |
| C2_cap30 | Later ladder, cap 30% / 后移阶梯 | -48.5714 | -155.8064 | Historical only |
| C3_cap30_both_legs | Cap30 + require both entry legs | -14.2042 | -23.0980 | Historical only |
| C4_cap30_minprice20 | Cap30 + Main ask >=0.20 | -48.5714 | -155.8064 | Historical only |
| C5_cap30_minprice35 | Cap30 + Main ask >=0.35 | -48.5714 | -155.8064 | Historical only |
| C6_cap30_mainlead10 | Cap30 + Main probability lead >=0.10 | -48.5714 | -155.8064 | Historical only |
| C7_cap30_spread_depth | Cap30 + explicit Main spread/depth | -19.9772 | -54.5169 | Historical only |
| C8_cap30_both_minp35 | Cap30 + both legs + Main ask >=0.35 | -14.2042 | -23.0980 | Historical only |
| C9_cap15 | Later ladder, cap 15% | -44.7043 | -98.9611 | Historical only |
| R1_cap30_all_gates | Both legs + spread/depth + Main lead | -0.9566 | +19.0708 | Historical only |
| R2_cap30_mainonly | Cap30 + suppress PROTECTION buys | -80.8101 | -126.7705 | Historical only |
| R3_cap15_mainonly | Cap15 + suppress PROTECTION buys | -41.3241 | -125.8621 | Historical only |
| R4_cap30_both_spread | Both legs + explicit Main spread/depth | -0.9566 | +19.0708 | Historical only |
| S1_capinv_both_spread | R4 + fixed 200-unit reference sizing | +0.3744 | +0.3744 | Historical only |
| S2_capinv_delayconfirm | S1 + two distinct quality decision ticks | +2.0771 | +2.0771 | Historical only |
| S3_capinv_earlytrim | S1 + early Main risk-exit switch (historical name: earlytrim) | -12.0782 | -12.0782 | Historical only |
| H1a_confirm3 | S2-style confirmation, 3 ticks | -1.3100 | -1.3100 | Historical only |
| H1b_confirm5 | S2-style confirmation, 5 ticks | -1.5977 | -1.5977 | Historical only |
| H2_late14 | First entry >=14:00; both-leg check, no explicit quality gate | +7.3990 | +7.3990 | Historical only |
| H3_trend | Main probability trend/variance history + both-leg check | -0.0845 | -0.0845 | Historical only |
| HS_adverse1tick | H2 + asks up/bids down one tick | +3.5743 | +3.5743 | Historical only |
| HS_delay1min | H2 + next-frame execution-book sensitivity | -6.3638 | -6.3638 | Historical only |
| HS_t1300 | H2 first-entry clock sensitivity 1300 | +7.3990 | +7.3990 | Historical only |
| HS_t1330 | H2 first-entry clock sensitivity 1330 | +7.3990 | +7.3990 | Historical only |
| HS_t1430 | H2 first-entry clock sensitivity 1430 | +7.3990 | +7.3990 | Historical only |
| HS_t1500 | H2 first-entry clock sensitivity 1500 | +7.3990 | +7.3990 | Historical only |
| A1_no_lowvalue_addons | H2 + BUY budget filter outside first-entry wrapper | +17.1625 | +17.1625 | Historical only |
| A2_protection_off_tail | H2 + suppress PROTECTION + early Main risk exit | -103.9337 | -103.9337 | Historical only |
| A3_protection_off_naked | H2 + suppress PROTECTION, no added risk-exit switch | +29.9022 | +29.9022 | Historical only |
| B1_earlier_if_stable | 13:00 entry + 10-frame Main-probability stability | +1.1891 | +1.1891 | Historical only |
| C1_account_scaled_budget | H2 + cash-on-hand sizing instead of fixed 200 | +6.4453 | +9.8410 | Historical only |
| C2_fixed_risk_per_event | H2 + first-entry target cap 4 units | +0.0000 | +0.0000 | Historical only |
| H2_baseline | Round-7 H2 reference | +7.3990 | +7.3990 | Historical only |

Failures and limits are retained: original-baseline losses, early-risk-exit losses, no-trade fixed-risk records and the negative H2 delay sensitivity. Positive results in the same reused sample do not establish out-of-sample skill. The recorded larger-capital R1/R4 benefit was described in source comments as a participation/gate artifact; it has not been independently re-established here.

Current-code H2 has no explicit spread/depth predicate; A1 can suppress Protection/repair BUYs; v3_true_fee differs from reservation-gated sizing. See [implementation notes](IMPLEMENTATION.md). Source-result SHA-256 values in [historical_summary.json](../results/historical_summary.json) identify the reviewed files but do not retroactively bind them to complete engine versions.
