# Solana Ecosystem Health Report

An automated, keyless network health dashboard and report generator for the Solana blockchain. This tool runs a background orchestrator that gathers live metrics from multiple public sources on a two-tier schedule, runs a statistical anomaly detector using local database baselines, and generates an interactive dark-themed HTML dashboard, a pretty-printed JSON file, and a formatted Markdown report.

---

## 1. Overview

The Solana Ecosystem Health Report is a self-updating reporting pipeline designed to provide a comprehensive, real-time snapshot of the Solana network. The orchestrator collects performance, validator health, DeFi, news, and tokenized asset metrics directly into `data.json`, which serves as the data layer for the frontend. Simultaneously, it compiles the aggregated state into a structured Markdown health report (`reports/latest.md`) and a unified JSON dump (`reports/latest.json`).

*   **Live Demo (Hosted version)**: *[Deploy placeholder — link to be updated upon hosting]*
*   **Sample Generated Report**: [reports/latest.md](file:///home/mickey/ser/reports/latest.md)
*   **Ecosystem Dashboard**: Open [dashboard.html](file:///home/mickey/ser/dashboard.html) in your browser (via a local web server) to interact with live charts, news articles, and active validator statistics.

---

## 2. Data Sources & Integration

To maintain maximum technical autonomy and zero setup friction, all collectors use the Python standard library only and require no API keys:

| Category | Metric | Source | Specific Methods / Endpoints |
|---|---|---|---|
| **Network** | TPS, Slot Time, Finalized Block Height, Epoch Progress | Solana JSON-RPC | Methods: `getRecentPerformanceSamples`, `getSlot`, `getEpochInfo`, `getHealth`, `getBlockTime` |
| **Validator Health** | Active/Delinquent Count, Average Commission, Top 5 Stake | Solana JSON-RPC | Method: `getVoteAccounts` |
| **Supply** | Total, Circulating, and Non-circulating Supply | Solana JSON-RPC | Method: `getSupply` |
| **Market Data** | Spot Price, 24h Change % | Jupiter Price API v3 | Endpoint: `https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112` |
| **Market Cap & Volume** | Circulating Supply, CEX Volume | CoinGecko API | Endpoint: `/coins/solana` (using `/simple/price` fallback patterns) |
| **DeFi Indicators** | Chain TVL, DEX 24h Volume, Stablecoin Supply | DeFiLlama API | Endpoints: `/chains` (Solana TVL), `/stablecoins`, `/overview/dexs/solana` |
| **Ecosystem Economics** | Real Economic Value (REV), Median Transaction Fee | DeFiLlama fees API + Solana RPC | Endpoints: `https://api.llama.fi/summary/fees/solana` + RPC `getRecentPrioritizationFees` |
| **Ecosystem Growth** | Tokenized RWA TVL | DeFiLlama protocols | Endpoint: `https://api.llama.fi/protocols` (filtered for Category = `"RWA"`) |
| **News & Announcements** | Ecosystem News, Developer Logs | RSS Blog Feeds | Parsed RSS endpoints: Solana Official Blog Feed + Cointelegraph Solana News Tag |

> [!NOTE]
> **Daily Active Addresses (DAA)** is marked as **Not Available**. Resolving network-wide signing addresses over a 24-hour window requires heavy transaction indexing. Public RPC nodes do not offer this aggregate method, and public indexers (e.g., Dune Analytics) require paid subscription API keys. Rather than hardcoding static placeholder data, this metric is cleanly reported as unavailable in all outputs.

---

## 3. Automation & Orchestration Strategy

The pipeline is managed by `serve_data.py`, which implements a multithreaded daemon loop using two execution tiers to balance data freshness against rate limits and socket latency:

```
[Background Loops] ──► [Locks & Cache] ──► [Fast Loop (5s)] ──► [Data Layer]
  - RPC Validator (60s)    - _market_lock                         - data.json
  - Supply (60s)           - _defi_lock                           - reports/latest.md
  - DeFiLlama (60s)        - _economics_lock                      - reports/latest.json
  - RSS News (60s)         - _rwa_lock
  - Economics (60s)        - _news_lock
  - RWA TVL (300s)
```

1.  **Fast Loop (5s Cadence)**:
    *   Directly queries fast, lightweight RPC methods (TPS, slot height, slot production duration).
    *   Merges cached values from background loops under thread-safe locks (`threading.Lock`).
    *   Saves the unified snapshot atomically (`tempfile` write + `replace` pattern) to prevent file truncation.
    *   Triggers JSON and Markdown report generation on each cycle.
2.  **Slow/Background Threads (Asynchronous)**:
    *   **Market Data (30s)**: Concurrently polls Jupiter Spot Price and CoinGecko (converts volume and cap).
    *   **General Slow Tier (60s)**: Refreshes supply, DeFi TVL, RSS news headlines, and fast economic metrics (transaction fee, DeFiLlama fee revenue).
    *   **RWA TVL Allocation (300s / 5m)**: Iterates and sums Solana-allocated TVL for RWA category protocols. Kept at 5m to avoid rate-limiting on the heavy 5.8 MB DeFiLlama protocols endpoint.
3.  **Wall-Clock Scheduling**:
    *   Instead of standard `time.sleep(interval)`, cycles measure elapsed execution time and adjust the remaining sleep duration dynamically: `sleep_sec = max(0.0, fast_interval - elapsed)`. This prevents loop drift from transient RPC latency spikes.

---

## 4. Statistical Anomaly Detection

Every fast cycle, the pipeline evaluates the current snapshot against a rolling baseline computed from the local SQLite time-series database (`storage/history.db`):

*   **Baseline Window**: Extracts the last 20 snapshots (minimum of 5 snapshots is required for variance baseline computation).
*   **Evaluation Metrics**: TPS spikes/drops, slot time production slowdowns, validator delinquency increases, large TVL outflows, or price swings.
*   **Z-Score Calculations**: Evaluates deviation threshold:
    $$\sigma = \frac{\text{Current Value} - \mu}{\text{Std Dev}}$$
*   **Variance Noise Floor**: If standard deviation approaches zero (low-variance test or local startup environments), the detector floors $\text{Std Dev}$ at $2\%$ of the baseline mean to prevent standard deviation division explosions.
*   **Alert Routing**: Any triggered alert is merged into the top-level `"alerts"` list in `data.json`, rendering dynamic warnings inside the UI, and appending warning/critical indicators directly inside `reports/latest.md`.

---

## 5. Setup & Local Run Instructions

### Prerequisites
*   Python 3.8+ (Standard Library only - zero `pip` installations required)
*   Node.js (for Vite dev server check)

### Step 1: Start the Background Daemon Loop
Run the orchestrator from the project directory:
```bash
python3 serve_data.py
```
This initializes the SQLite database, boots the background cache-refresh threads, executes initial fetches, and begins updating `data.json` every 5 seconds.

To perform a single-shot execution and compile reports immediately without keeping the loop active:
```bash
python3 serve_data.py --once
```

### Step 2: Start the Web Dashboard
Launch the local web server to open the interactive interface:
```bash
npm run dev
```
Open the provided local URL (default: `http://localhost:5173`) in your web browser. The dashboard will automatically read from `data.json` and update in real-time.

### Step 3: Access Generated Reports
*   **Formatted Markdown Report**: [`reports/latest.md`](file:///home/mickey/ser/reports/latest.md)
*   **Unified Pretty JSON Snapshot**: [`reports/latest.json`](file:///home/mickey/ser/reports/latest.json)

---

## 6. Project Structure

```
solana-ecosystem-report/
├── collectors/          # keyless web collectors (Python standard urllib/xml)
│   ├── coingecko_collector.py  # retrieves market cap & CEX trade volume
│   ├── defillama_collector.py   # retrieves chain TVL & stablecoin supplies
│   ├── economics_collector.py   # retrieves median RPC fees, REV, and RWA TVL
│   ├── jupiter_collector.py     # retrieves high-speed spot price & 24h change
│   ├── news_collector.py        # parses RSS feeds for official updates
│   └── rpc_collector.py         # retrieves core slot, tps, and vote account tables
├── analysis/            # analytical processing engines
│   └── anomaly_detector.py      # statistical Z-score baseline evaluation
├── storage/             # persistent snapshot storage
│   ├── history.py               # manages SQLite transactions and pruning
│   └── history.db               # local SQLite DB (auto-created; retains last 500 rows)
├── report/              # file compilers for bounty submission
│   ├── json_generator.py        # compiles pretty-printed data reports
│   └── markdown_generator.py    # renders markdown tables and health badges
├── reports/             # output directory (auto-created)
│   ├── latest.json              # final pretty JSON snapshot
│   └── latest.md                # final human-readable markdown report
├── serve_data.py        # multithreaded daemon loop orchestrator
├── dashboard.html       # dark-themed interactive HTML dashboard
├── package.json         # Vite dev server configuration scripts
└── README.md            # Project submission brief (this file)
```

---

## 7. Known Scope Limitations & Technical Honesty

1.  **Daily Active Addresses (DAA)**: Marked as **Not Available**. No public keyless endpoint exists. Standard Solana RPCs cannot calculate this value natively.
2.  **Jito MEV tips (Real Economic Value)**: DeFiLlama's `/fees/solana` endpoint tracks total transaction fees ($516\text{K/day}$), but off-chain validator tips (which add $1,000$ to $1,500\text{ SOL/day}$) are excluded from aggregate public fee dashboards. Real Economic Value listed in this dashboard is based on base and prioritization transaction fees.
3.  **Tokenized RWA Metric**: Since RWA secondary trading volume is spread across multiple DEXs and orderbooks, there is no keyless API tracking daily transaction volume specifically. The "Tokenized RWA TVL" indicator represents the aggregate locked value of RWA category assets on Solana.

---

## 8. Live Demo

*   **Hosted URL**: *[Deploy placeholder — link to be updated upon hosting]*
