/**
 * api/data.js
 * ============
 * Vercel Serverless Function that compiles the Solana ecosystem health snapshot.
 * Uses native fetch (Node 18+) and in-memory caches to respect rate limits.
 */

// Module-level caches (preserved across warm serverless starts)
let defiCache = null;
let marketCache = null;
let supplyCache = null;
let rwaCache = null;
let newsCache = null;
let economicsCache = null;
let historyCache = [];

let lastUpdated = {
  defi: 0,
  market: 0,
  supply: 0,
  rwa: 0,
  news: 0,
  economics: 0
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function rpcCall(method, params = [], timeout = 5000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch("https://api.mainnet-beta.solana.com", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      signal: controller.signal
    });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (body.error) throw new Error(body.error.message);
    return body.result;
  } catch (err) {
    clearTimeout(id);
    console.warn(`RPC Warning: ${method} failed - ${err.message}`);
    return null;
  }
}

async function getJupiterPrice() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch("https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112", {
      signal: controller.signal
    });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    const data = body.data?.["So11111111111111111111111111111111111111112"];
    return {
      priceUsd: data ? parseFloat(data.price) : null,
      change24hPct: null
    };
  } catch (err) {
    clearTimeout(id);
    console.warn(`Jupiter Price failed - ${err.message}`);
    return null;
  }
}

async function getCoinGeckoMarket() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch("https://api.coingecko.com/api/v3/coins/solana?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false", {
      headers: { "User-Agent": "solana-ecosystem-report/0.1" },
      signal: controller.signal
    });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    const md = body.market_data;
    return {
      priceUsd: md?.current_price?.usd || null,
      change24hPct: md?.price_change_percentage_24h || null,
      marketCapUsd: md?.market_cap?.usd || null,
      volume24hUsd: md?.total_volume?.usd || null,
      circulatingSupply: md?.circulating_supply || null
    };
  } catch (err) {
    clearTimeout(id);
    console.warn(`CoinGecko Market failed - ${err.message}`);
    return null;
  }
}

async function getDeFiLlama() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 8000);
  try {
    const tvlPromise = fetch("https://api.llama.fi/chains", { signal: controller.signal })
      .then(r => r.json())
      .catch(() => null);
    
    const stablePromise = fetch("https://stablecoins.llama.fi/stablecoins?includePrices=true", { signal: controller.signal })
      .then(r => r.json())
      .catch(() => null);
      
    const dexPromise = fetch("https://api.llama.fi/overview/dexs/solana", { signal: controller.signal })
      .then(r => r.json())
      .catch(() => null);

    const [tvlRes, stableRes, dexRes] = await Promise.all([tvlPromise, stablePromise, dexPromise]);
    
    let tvlUsd = null;
    if (Array.isArray(tvlRes)) {
      const sol = tvlRes.find(c => c.name?.toLowerCase() === "solana");
      if (sol) tvlUsd = sol.tvl;
    }
    
    let stablecoinSupplyUsd = null;
    if (stableRes && Array.isArray(stableRes.peggedAssets)) {
      let sum = 0;
      for (const asset of stableRes.peggedAssets) {
        if (asset.chainBalances?.Solana) {
          sum += asset.chainBalances.Solana;
        }
      }
      if (sum > 0) stablecoinSupplyUsd = sum;
    }
    
    let dexVolume24hUsd = null;
    if (dexRes && dexRes.total24h) {
      dexVolume24hUsd = dexRes.total24h;
    }
    
    clearTimeout(id);
    return { tvlUsd, stablecoinSupplyUsd, dexVolume24hUsd };
  } catch (err) {
    clearTimeout(id);
    console.warn(`DeFiLlama failed - ${err.message}`);
    return null;
  }
}

async function getRwaVolume() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch("https://api.llama.fi/protocols", { signal: controller.signal });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const protocols = await res.json();
    let sum = 0;
    if (Array.isArray(protocols)) {
      for (const p of protocols) {
        if (p.category === "RWA" && p.chainTvls) {
          for (const key of Object.keys(p.chainTvls)) {
            if (key.toLowerCase() === "solana") {
              sum += p.chainTvls[key] || 0;
            }
          }
        }
      }
    }
    return sum;
  } catch (err) {
    clearTimeout(id);
    console.warn(`RWA fetch failed - ${err.message}`);
    return null;
  }
}

async function getDeFiLlamaFees() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch("https://api.llama.fi/summary/fees/solana", { signal: controller.signal });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    return body.total24h ? parseFloat(body.total24h) : null;
  } catch (err) {
    clearTimeout(id);
    console.warn(`Fees fetch failed - ${err.message}`);
    return null;
  }
}

function parseRss(xmlText, sourceName, filterKeyword) {
  const items = [];
  const itemRegex = /<item>([\s\S]*?)<\/item>/g;
  let match;
  while ((match = itemRegex.exec(xmlText)) !== null) {
    const itemContent = match[1];
    const titleMatch = /<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/title>/.exec(itemContent);
    const linkMatch = /<link>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/link>/.exec(itemContent);
    const pubDateMatch = /<pubDate>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?<\/pubDate>/.exec(itemContent);
    
    if (titleMatch && linkMatch) {
      const title = titleMatch[1].trim();
      const link = linkMatch[1].trim();
      const pubDate = pubDateMatch ? pubDateMatch[1].trim() : '';
      
      if (filterKeyword && !title.toLowerCase().includes("solana")) {
        continue;
      }
      items.push({
        title,
        link,
        published: pubDate,
        source: sourceName
      });
    }
  }
  return items;
}

async function getNews() {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), 8000);
  try {
    const blogPromise = fetch("https://solana.com/news/rss.xml", { signal: controller.signal })
      .then(r => r.text())
      .catch(() => "");
    const telegraphPromise = fetch("https://cointelegraph.com/rss/tag/solana", { signal: controller.signal })
      .then(r => r.text())
      .catch(() => "");
    
    const [blogXml, telegraphXml] = await Promise.all([blogPromise, telegraphPromise]);
    
    const items = [];
    if (blogXml) items.push(...parseRss(blogXml, "Solana Blog", false));
    if (telegraphXml) items.push(...parseRss(telegraphXml, "Cointelegraph Solana", true));
    
    const seen = new Set();
    const uniqueItems = [];
    for (const item of items) {
      if (!seen.has(item.link)) {
        seen.add(item.link);
        uniqueItems.push(item);
      }
    }
    
    uniqueItems.sort((a, b) => new Date(b.published) - new Date(a.published));
    clearTimeout(id);
    return uniqueItems.slice(0, 5);
  } catch (err) {
    clearTimeout(id);
    console.warn(`News fetch failed - ${err.message}`);
    return [];
  }
}

// ─── Anomaly Detector ────────────────────────────────────────────────────────

function detectAnomalies(current, history) {
  const alerts = [];
  const metrics = [
    { key: "tps", path: ["network", "tps"], name: "TPS", lowThreshold: -1.8, highThreshold: null, isDelinquency: false },
    { key: "slotTime", path: ["network", "avgSlotTimeMs"], name: "Slot Time", lowThreshold: null, highThreshold: 2.2, isDelinquency: false },
    { key: "delinquency", path: ["validators", "delinquentCount"], name: "Delinquency", lowThreshold: null, highThreshold: 3.0, isDelinquency: true }
  ];
  
  const getVal = (obj, path) => {
    let curr = obj;
    for (const p of path) {
      if (curr == null) return null;
      curr = curr[p];
    }
    return curr;
  };

  const nowVal = getVal(current, ["meta", "collected_at"]) || new Date().toISOString();
  const nowTime = nowVal.substring(11, 16) + " UTC";

  for (const m of metrics) {
    const curVal = getVal(current, m.path);
    if (curVal == null) continue;
    
    const histVals = [];
    for (const h of history) {
      const v = getVal(h, m.path);
      if (v != null) histVals.push(v);
    }
    
    if (histVals.length < 5) continue;
    
    const mean = histVals.reduce((sum, v) => sum + v, 0) / histVals.length;
    const variance = histVals.reduce((sum, v) => sum + Math.pow(v - mean, 2), 0) / histVals.length;
    const stddev = Math.sqrt(variance);
    
    const floor = Math.max(0.01, Math.abs(mean) * 0.02);
    const effectiveStd = Math.max(stddev, floor);
    
    const z = (curVal - mean) / effectiveStd;
    
    if (m.lowThreshold != null && z < m.lowThreshold) {
      const zStr = z < -5.0 ? ">5.0σ" : `${Math.abs(z).toFixed(1)}σ`;
      alerts.push({
        id: `anomaly-${m.key}`,
        severity: "warning",
        title: `${m.name} Drop Detected`,
        message: `${m.name} dropped to ${curVal.toFixed(1)} (rolling mean ${mean.toFixed(1)}, deviation ${zStr} below baseline).`,
        time: nowTime
      });
    }
    if (m.highThreshold != null && z > m.highThreshold) {
      const zStr = z > 5.0 ? ">5.0σ" : `${z.toFixed(1)}σ`;
      const sev = m.isDelinquency ? "critical" : "warning";
      alerts.push({
        id: `anomaly-${m.key}`,
        severity: sev,
        title: `${m.name} Surge Detected`,
        message: `${m.name} rose to ${curVal.toFixed(1)} (rolling mean ${mean.toFixed(1)}, deviation ${zStr} above baseline).`,
        time: nowTime
      });
    }
  }
  return alerts;
}

// ─── Route Handler ───────────────────────────────────────────────────────────

export default async function handler(req, res) {
  try {
    const collectedAt = new Date().toISOString();
    const now = Date.now();

    // 1. Fetch live RPC fast indicators & Jupiter Price concurrently
    const [healthRes, slotRes, epochRes, perfRes, voteRes, jupRes] = await Promise.all([
      rpcCall("getHealth").catch(() => null),
      rpcCall("getSlot").catch(() => null),
      rpcCall("getEpochInfo").catch(() => null),
      rpcCall("getRecentPerformanceSamples", [20]).catch(() => null),
      rpcCall("getVoteAccounts").catch(() => null),
      getJupiterPrice().catch(() => null)
    ]);

    // 2. Parse performance values
    let tps = null;
    let avgSlotTimeMs = null;
    if (Array.isArray(perfRes) && perfRes.length > 0) {
      const samples = perfRes.filter(s => s.samplePeriodSecs > 0 && s.numSlots > 0);
      if (samples.length > 0) {
        tps = parseFloat((samples[0].numTransactions / samples[0].samplePeriodSecs).toFixed(1));
        const times = samples.map(s => (s.samplePeriodSecs * 1000) / s.numSlots);
        avgSlotTimeMs = parseFloat((times.reduce((a, b) => a + b, 0) / times.length).toFixed(1));
      }
    }

    // 3. Parse Epoch metrics
    let epochNumber = null;
    let epochProgressPct = null;
    let epochSlotIndex = null;
    let epochSlotsInEpoch = null;
    let epochSlotStart = null;
    let epochSlotEnd = null;
    let epochEtaHours = null;
    if (epochRes && slotRes != null) {
      epochNumber = epochRes.epoch;
      epochSlotIndex = epochRes.slotIndex;
      epochSlotsInEpoch = epochRes.slotsInEpoch;
      epochProgressPct = parseFloat(((epochSlotIndex / epochSlotsInEpoch) * 100).toFixed(2));
      epochSlotStart = slotRes - epochSlotIndex;
      epochSlotEnd = epochSlotStart + epochSlotsInEpoch;
      const remainingSlots = epochSlotsInEpoch - epochSlotIndex;
      const avgMs = avgSlotTimeMs || 400.0;
      epochEtaHours = parseFloat((remainingSlots * avgMs / 3600000).toFixed(2));
    }

    // 4. Parse Validator Metrics
    let activeCount = null;
    let delinquentCount = null;
    let avgCommissionPct = null;
    let topValidators = [];
    if (voteRes) {
      const current = voteRes.current || [];
      const delinquent = voteRes.delinquent || [];
      activeCount = current.length;
      delinquentCount = delinquent.length;
      if (current.length > 0) {
        const sorted = [...current].sort((a, b) => (b.activatedStake || 0) - (a.activatedStake || 0));
        topValidators = sorted.slice(0, 5).map(v => ({
          votePubkey: v.votePubkey,
          name: v.votePubkey.substring(0, 8) + "…",
          activated_stake_sol: parseFloat((v.activatedStake / 1e9).toFixed(2)),
          commission: v.commission || 0,
          stake_pct: null
        }));
        const comms = current.map(v => v.commission || 0);
        avgCommissionPct = parseFloat((comms.reduce((a, b) => a + b, 0) / comms.length).toFixed(2));
      }
    }

    // 5. Caching slow variables
    // Supply
    if (!supplyCache || (now - lastUpdated.supply > 60000)) {
      const supplyRes = await rpcCall("getSupply", [{ "excludeNonCirculatingAccountsList": true }]);
      if (supplyRes && supplyRes.value) {
        const val = supplyRes.value;
        supplyCache = {
          collected_at: collectedAt,
          totalSOL: parseFloat((val.total / 1e9).toFixed(2)),
          circulatingSOL: parseFloat((val.circulating / 1e9).toFixed(2)),
          nonCirculatingSOL: parseFloat((val.nonCirculating / 1e9).toFixed(2))
        };
        lastUpdated.supply = now;
      }
    }

    // Market
    if (!marketCache || (now - lastUpdated.market > 30000)) {
      const cgMarket = await getCoinGeckoMarket();
      marketCache = {
        priceUsd: jupRes?.priceUsd || cgMarket?.priceUsd || null,
        change24hPct: cgMarket?.change24hPct || null,
        marketCapUsd: cgMarket?.marketCapUsd || null,
        volume24hUsd: cgMarket?.volume24hUsd || null,
        circulatingSupply: cgMarket?.circulatingSupply || null
      };
      lastUpdated.market = now;
    }

    // DeFi
    if (!defiCache || (now - lastUpdated.defi > 60000)) {
      const defiRes = await getDeFiLlama();
      if (defiRes) {
        defiCache = defiRes;
        lastUpdated.defi = now;
      }
    }

    // RWA
    if (!rwaCache || (now - lastUpdated.rwa > 300000)) {
      const rwaVol = await getRwaVolume();
      if (rwaVol !== null) {
        rwaCache = rwaVol;
        lastUpdated.rwa = now;
      }
    }

    // News
    if (!newsCache || (now - lastUpdated.news > 60000)) {
      const newsRes = await getNews();
      if (newsRes && newsRes.length > 0) {
        newsCache = newsRes;
        lastUpdated.news = now;
      }
    }

    // Economics (fees & DeFiLlama fees)
    if (!economicsCache || (now - lastUpdated.economics > 60000)) {
      const feeRes = await rpcCall("getRecentPrioritizationFees");
      let medianFeeSol = 0.000005;
      if (Array.isArray(feeRes) && feeRes.length > 0) {
        const fees = feeRes.map(f => f.prioritizationFee).sort((a, b) => a - b);
        const n = fees.length;
        let medianMicro = 0;
        if (n > 0) {
          if (n % 2 === 1) {
            medianMicro = fees[Math.floor(n / 2)];
          } else {
            medianMicro = (fees[Math.floor(n / 2) - 1] + fees[Math.floor(n / 2)]) / 2;
          }
        }
        const priorityLamports = medianMicro * 0.2;
        medianFeeSol = (5000 + priorityLamports) / 1e9;
      }
      const revUsd = await getDeFiLlamaFees();
      economicsCache = {
        medianFeeSol,
        revUsd24h: revUsd
      };
      lastUpdated.economics = now;
    }

    // 6. Compile JSON snapshot matching expected data.json schema
    const snapshot = {
      meta: {
        collected_at: collectedAt,
        source: "vercel-serverless",
        endpoint: "https://api.mainnet-beta.solana.com"
      },
      network: {
        tps,
        avgSlotTimeMs,
        blockHeight: slotRes,
        epochNumber,
        epochProgressPct,
        epochSlotIndex,
        epochSlotsInEpoch,
        epochSlotStart,
        epochSlotEnd,
        epochEtaHours,
        healthStatus: healthRes === "ok" ? "healthy" : "degraded"
      },
      validators: {
        activeCount,
        delinquentCount,
        avgCommissionPct,
        topValidators
      },
      supply: supplyCache,
      market: marketCache,
      defi: defiCache,
      economics_extra: economicsCache ? {
        medianFeeSol: economicsCache.medianFeeSol,
        revUsd24h: economicsCache.revUsd24h,
        rwaVolumeUsd: rwaCache
      } : null,
      news: newsCache || [],
      alerts: [],
      anomalies: []
    };

    // Backfill validator stake shares
    const totalSOL = supplyCache?.totalSOL;
    if (totalSOL && snapshot.validators.topValidators.length > 0) {
      for (const v of snapshot.validators.topValidators) {
        if (v.activated_stake_sol) {
          v.stake_pct = parseFloat(((v.activated_stake_sol / totalSOL) * 100).toFixed(2));
        }
      }
    }

    // Anomaly alerts
    const alerts = detectAnomalies(snapshot, historyCache);
    snapshot.alerts = alerts;
    snapshot.anomalies = alerts;

    // Append to warm-history cache
    historyCache.push(JSON.parse(JSON.stringify(snapshot)));
    if (historyCache.length > 20) {
      historyCache.shift();
    }

    // Add Vercel cache header (CDN edge cache for 5s, stale-while-revalidate for 5s)
    res.setHeader("Cache-Control", "s-maxage=5, stale-while-revalidate");
    res.setHeader("Content-Type", "application/json");
    res.status(200).json(snapshot);

  } catch (error) {
    console.error("Vercel Serverless Function Error:", error);
    res.status(500).json({ error: error.message || "Internal Server Error" });
  }
}
