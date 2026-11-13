# Data sources, formats and permission decisions / 数据来源与权限

Review date: **2026-11-13**. No real raw dataset is bundled. Own code's MIT licence does not cover third-party data.

## Separate market records from weather measurements

| Source | Observed local research scope / time | Units, clock and fields | Settlement evidence | Permission decision |
| --- | --- | --- | --- | --- |
| Pendulum Flow V3 Polymarket orderbook archive | Research sample: HK event dates 2026-08-19–2026-10-25; V3 windows use 360 minute samples; not full-day coverage | UTC `minute_us`; converted to HKT UTC+08; bids/asks `{price,size}`, proxy/envelope signal, known/crossed status, token and bucket identity. Price in collateral units/share, size in shares; temperatures label contract buckets in °C | Orderbooks alone do not prove the winner | Raw files withheld |
| Direct public Polymarket Gamma + CLOB frames | One additional HK event in the reviewed inputs: 2026-10-25 | `received_at` with offset; raw/normalized prices; complete YES bids/asks, asset version, tick/minimum size, fee schedule and quality/errors | Must join exact event identity, final official resolution and payouts after replay | Raw frames withheld; redistribution permission unconfirmed |
| HKO official weather data | Reviewed audit references a 2026 daily-maximum extract through 2026-09-30, plus current regional observation formats | HKT local dates; °C; station identity; observation time; daily maximum versus provisional since-midnight maximum and latest 1-minute mean are different quantities | HKO is an observational source. The contract's named source, station, rounding and official final payouts still need checking | Raw CSV/website extracts withheld |
| This project's generated examples | Two artificial dates, 2030-01-01/02, 12:00–17:59 HKT | Same research frame keys; fabricated probabilities/depth, `SYNTHETIC_` asset labels; no real prices or temperatures | All labels unknown | Authored synthetic examples under MIT |

### Current sources and terms

- **Pendulum Flow**: [archive](https://archive.pendulumflow.com/), [credits](https://archive.pendulumflow.com/credits), [per-prefix licence](https://archive.pendulumflow.com/LICENSE.txt). Current own V3/V3.1 capture terms state **CC BY-NC 4.0**, with commercial licensing separate. A local downloader comment said CC BY 4.0; that is not accepted as current permission evidence. Historical terms for the exact downloaded objects were not recovered. Obtain permitted files directly, keep provider attribution, version/checksum and applicable terms. Respect the site's current download restrictions. No third-party collector code is included here.
- **Polymarket**: [market-data documentation](https://docs.polymarket.com/market-data/overview) and [terms](https://polymarket.com/tos). Public API availability does not itself establish redistribution rights. The terms page could not be retrieved by the research browser during this review, so this release does not claim a licence to republish the local direct captures.
- **HKO official open-data catalogue**: [daily maximum/mean/minimum](https://data.gov.hk/en-data/dataset/hk-hko-rss-daily-temperature-info-hko), [provisional regional since-midnight maximum/minimum](https://data.gov.hk/en-data/dataset/hk-hko-rss-max-and-min-air-temp-since-midnight), [open-data index](https://www.hko.gov.hk/en/abouthko/opendata_intro.htm), [DATA.GOV.HK terms](https://data.gov.hk/en/terms-and-conditions). The portal terms allow reuse subject to attribution and other conditions, and distinguish linked sites' terms. Separately, [HKO website terms](https://www.hko.gov.hk/en/readme/readme.htm), [non-commercial conditions](https://www.hko.gov.hk/en/appweb/applink.htm) and [commercial conditions](https://www.hko.gov.hk/en/appweb/commercial.htm) apply to website materials. Do not assume identical rights for every HKO webpage, API and CSV. Before redistribution, record the exact acquisition route, Government/HKO/DATA.GOV.HK ownership/attribution, applicable conditions and any required disclaimer. This release links to the sources and redistributes none of the raw HKO materials.

No dataset rights remain unresolved **for files being distributed**, because these third-party raw files are excluded. Adding real data later requires a new permission review; MIT is not a substitute. No attempt to bypass access restrictions is provided.

## Legal local-input workflow

Acquire data from a source you are permitted to use, using its own documented access method. Record URL/provider, retrieval date, file version/checksum, event identity, station/bucket definitions, timestamps, fees and applicable terms outside Git. Convert it locally to one of the formats below, retaining missing-data flags. No network downloader is shipped. Place private bundles in ignored `samples/your_bundle/` or keep them outside the checkout. Hashes verify bytes, not data quality or legal rights.

The supported runner accepts a manifest shaped like [manifest.example.json](../samples/manifest.example.json). Paths must stay inside its bundle directory and SHA-256 must match. `kind` is `V3` or `PUBLIC_GAMMA_BOOK`. Days must be unique and chronological replay is enforced by sorting them.

**PUBLIC_GAMMA_BOOK**: a JSON list of frames with `received_at` (ISO timestamp including timezone), `raw_prices_json`, `books_json`, `markets_json`, `errors_json` (the last four are JSON strings). Each book has `asset_id`, `bids`, `asks`, `tick_size`, `min_order_size`. Each level is `{ "price": "0.5", "size": "10" }`. Each market has `version`, `active`, `closed`, `acceptingOrders`, `feesEnabled`, `feeSchedule: {rate,exponent}`. The generator in [demo.py](../research/demo.py) is a complete, runnable synthetic format example. `synthetic` flags describe provenance; the frame format name does not mean the generator downloaded anything.

**V3**: SQLite opened read-only; a `frames` table with `minute_us`, `quality='COMPLETE_REPLAY'` and `books_json`; exactly 360 ordered frames. Manifest source `report.identity.markets` supplies `{token,label}` entries. `books_json` maps each token to `known`, `crossed`, `bids`, `asks`, optional `tick_size`, and `price_proxy` or `price_interval: {lower,upper}`. Missing envelopes wider than 0.01 are skipped; narrow ones use their midpoint. A missing tick is inferred from displayed price grids. These are explicit assumptions, not repaired historical facts.

## Labels / 结算标签

Default: `{ "status": "unknown", "winner": null }`. Only use `official` when the exact market's final result has evidence; include `winner` and a nonempty `evidence` reference. The runner requires the reference but cannot authenticate your evidence. Assumed labels are rejected by this supported runner and require a distinct sensitivity experiment. Never derive an official winner solely from a price, HKO measurement or a report's old label. For unknown events the inherited continuous accounting still uses window-end valuation as a hypothetical release, clearly labelled in output.

香港数据必须分别记录盘口与客观气温：来源、日期范围、单位、时区、站点和字段、市场温度桶规则与最终官方结算证据不可混用。真实数据不随本版上传；合成样例不代表真实历史。
