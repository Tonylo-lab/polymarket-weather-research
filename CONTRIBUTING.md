# Contributing / 参与研究

Please submit small, reproducible changes with offline tests. 请提交可复现的小改动和离线测试。

Useful tasks / 欢迎参与：

1. Add **H2-quality-v2** with an explicit spread/depth gate. Compare it with preserved H2; do not rename old results as v2 results. 为 H2 显式质量审核另立版本。
2. Evaluate reservation-gated execution separately, reporting reserved versus realized cost and skipped orders. 分开验证资金预留与实际成本。
3. Design settlement evidence records tied to the exact event, bucket rules, official final payouts and source hashes. Keep unknown labels unknown. 建立可核验结算证据，未知仍未知。
4. Add latency, partial-fill and unavailable-depth sensitivity tests without making network calls. 加入延迟、部分成交与缺失深度的离线敏感性测试。
5. Preregister a truly new sample, candidate set and accounting method; publish failures and no-trade outcomes. 预注册新样本，保留失败和无交易结果。

Run `python -B -m unittest discover -v` before a pull request. State what changed, which input version you used, fees, labels, execution assumptions and what remains unknown. A behavioral change needs a new experiment ID and new results. Synthetic tests validate mechanics, not profitability.

Do not submit private keys, account activity, real wallet addresses, personal logs or data whose redistribution terms are unresolved. The public project accepts local offline inputs; it has no live-account operation mode. Contributors retain copyright and must have the right to submit their work under MIT; identify any third-party material separately.
