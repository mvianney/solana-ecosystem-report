# Project Brief: Solana Ecosystem Report (Superteam Canada Bounty)

## 1. What We're Building

An automatically-updating report/dashboard on the current state of the Solana
ecosystem — a "health dashboard" for the network. It pulls live data from
public sources on a schedule and outputs the result in three formats:
an interactive HTML dashboard, a Markdown report, and a JSON data file.

**Submission requirement:** public GitHub repo with code, README, sample
outputs, and ideally a hosted live version of the HTML dashboard (e.g. via
GitHub Pages).

---

## 2. Data to Display

### Network Performance
- TPS (transactions per second)
- Slot time (time to produce each block)
- Block height
- Epoch progress (% through current epoch)

### Validator Health
- Active vs. delinquent validator count
- Stake distribution / top validators by stake
- Commission rates
- Delinquency alerts

### Economic Indicators
- SOL price + recent movement
- Stablecoin supply on Solana
- DEX volume
- TVL (Total Value Locked)
- REV (Real Economic Value)
- Median transaction fee

### Ecosystem Growth
- Tokenized real-world asset (e.g. equities) volume
- Daily active addresses

### News & Upgrades
- Notable ecosystem news/announcements
- Upcoming protocol upgrades (e.g. Alpenglow, SIMD-525)

### Anomaly Detection (valued, optional but should be included per scope decision)
- TPS drops/spikes
- Slow slot times
- High validator delinquency
- Large TVL or SOL price swings
- Approach: rolling mean + standard deviation thresholds per metric
  (explainable, not black-box)

---

## 3. Data Sources (no API keys required)

| Source | Data | Method |
|---|---|---|
| Solana RPC (public endpoint) | Network + validator data | Direct RPC calls: `getSlot`, `getBlockTime`, `getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`, `getBalance`, `getSignaturesForAddress`, `getHealth`, `getSupply` |
| CoinGecko | SOL price, market data | Free public endpoint |
| DeFiLlama | TVL, stablecoin supply | Free public endpoint |
| Dune Analytics | Community dashboards | Public query result access (no auth for public queries) — fallback: link to dashboard if unreliable |
| solana.com/data + ecosystem sites | Supplementary ecosystem stats | Scraping/parsing, as needed |
| Twitter/X (key accounts) | News, announcements, sentiment | Scoped carefully — no API key approach needed |

**Constraint:** prefer Python stdlib only (urllib, json, sqlite3) — avoid
external dependencies unless they meaningfully improve quality (flag and
decide case-by-case).

---

## 4. Architecture

```
solana-ecosystem-report/
├── collectors/          # one module per data source
│   ├── rpc_collector.py
│   ├── defillama_collector.py
│   ├── coingecko_collector.py
│   ├── dune_collector.py
│   └── news_collector.py
├── analysis/
│   ├── anomaly_detector.py
│   └── metrics_calculator.py    # derived metrics (REV, median fees, etc.)
├── storage/
│   └── history.py               # local JSON/SQLite time-series store
├── report/
│   ├── html_generator.py        # dark-theme dashboard
│   ├── markdown_generator.py
│   └── json_generator.py
├── config.py                    # refresh intervals, thresholds, toggles
├── run.py                       # orchestrator: collect → analyze → render → save
├── scheduler.py                 # loop for auto-updating; cron-compatible
└── README.md
```

### Data Flow
1. **Collect** — each collector returns a plain dict; fails gracefully
   (timeouts/retries); independently runnable/testable.
2. **Persist** — every run appends a snapshot to local history, giving
   anomaly detection a baseline and enabling trend views.
3. **Analyze** — compare latest snapshot to rolling baseline; flag
   deviations past configurable thresholds.
4. **Render** — HTML/MD/JSON generators all consume the same normalized
   data model so outputs stay in sync.
5. **Orchestrate** — `run.py` = one full pass; `scheduler.py` wraps it in
   a loop, but design stays cron-compatible for hosted/scheduled runs.

---

## 5. Scope Decision

**Full scope for v1** — cover all metric categories above, include anomaly
detection, and produce all three output formats (HTML, Markdown, JSON) from
the start rather than shipping an MVP first.

---

## 6. Submission Deliverables Checklist

- [ ] Public GitHub repo, all code + README.md
- [ ] README explains: setup, how to run, how to interpret output
- [ ] Live/hosted dashboard demo (GitHub Pages recommended)
- [ ] Sample generated Markdown report
- [ ] Sample generated JSON report
- [ ] Write-up covering: data sources & integration, automation strategy,
      anomaly detection approach, setup instructions
- [ ] No plagiarism — original implementation

---

## 7. Judging Criteria (design against these)

1. **Comprehensiveness** — breadth/depth of metrics covered
2. **Automation & Maintainability** — how hands-off is data refresh/report generation
3. **Clarity & Presentation** — quality of HTML/MD/JSON formatting and readability
4. **Innovation** — novel collection/analysis/presentation (e.g. anomaly detection, multi-source correlation)
5. **Technical Implementation** — code quality, documentation, ease of setup
6. **No Plagiarism** — must be original
