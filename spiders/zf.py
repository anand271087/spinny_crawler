"""ZF Aftermarket spider — v1.0 brand (cracked 2026-05-19; was Vue SPA stub).

Site: https://aftermarket.zf.com/en/aftermarket-portal/our-catalog/search-by-vehicle/
Platform: Vue.js SPA backed by a clean REST API at /functions/controller/opc/.
xlsx fields: item_name, item_code, compatible_car_model.

Site reality (verified 2026-05-19):

The Vue SPA renders a 5-level cascade UI (Manufacturer × Model × Vehicle × Variant
× Product Group × Brand) but the backend exposes a clean REST API discovered by
mining the Vue bundle for endpoint literals. No UI cascade driving needed.

CRAWL CHAIN (5 GETs deep, all under /functions/controller/opc/):

1. /getManufacturers
     params: languageID=4, brandIDs=14&22&32&35&68&126&161&294&8888 (ZF sub-brands),
             countryID=IND, vehicleTypeIDs=P  (P = Passenger Cars)
     → 105 manufacturers for India PV market (ABARTH, ALFA ROMEO, …, MARUTI, …)

2. /getModels?manufacturerID=X
     → ~7-30 models per manufacturer (e.g. MARUTI: 800, ALTO, BALENO, OMNI,
        WAGON R, ZEN, BALENO Estate)

3. /getGroupedVehicles?modelID=Y
     → variants grouped by engine/displacement; each entry has a `vehicleIDs`
        ARRAY of TecDoc K-types (e.g. MARUTI 800 0.8L → vehicleIDs=[33797,11007])

4. /getAllPictureAssemblyGroups?vehicleIDs=A,B,...
     → list of "picture assembly groups" (Engine, Steering, Brake, Chassis, …)
        each with inner `assemblyGroups` array of specific part types
        (Brake Disc=82, Tie Rod End=914, Inner Tie Rod=51, etc.)

5. /getArticlesForFilter?vehicleIDs=...&assemblyGroupIDs=N
     → articles for that vehicle + assembly group
        Each article: articleID, name, brandName, articleCriterias (specs)

Field mapping per xlsx:
   item_code ← articleID (e.g. "DF95023", "38747 01")
   item_name ← name (e.g. "Brake Disc", "Tie Rod End")
   compatible_car_model ← "<Manufacturer> | <Model> | <Variant>"

Per BRD passenger-vehicle gate: vehicleTypeIDs=P (Passenger Cars only).
Per BRD geography gate: countryID=IND, languageID=4.

Volume at full scope:
   105 mfrs × ~15 models × ~3 variants × ~30 assemblyGroups × ~1-5 articles
   ≈ 100K-500K article fetches.
   Each fetch ~0.3s = ~10-40 hours full crawl — exceeds BRD §8 8h window.

Realistic scope: limit to top-10 India PV manufacturers + dedup articles aggressively
(same article number fits many vehicles). Spider dedupes on `articleID` and merges
`compatible_car_model` into "; "-joined list.

Env vars — defaults are "representative" scope (production-ready, ~1-2h,
fits BRD §8 8h SLA). Override per knob for smoke or full scope:
   ZF_MAX_MFRS              default 0 (=all 105 manufacturers)
   ZF_MAX_MODELS_PER_MFR    default 0 (=all models)
   ZF_MAX_VARIANTS_PER_MDL  default 1 (1 representative variant per model)
   ZF_MAX_ASSEMBLYGROUPS    default 30 (top 30 AGs by sortNumber per variant)

Defaults bumped from `1/1/1/3` to `0/0/1/30` on 2026-05-22 after diagnosing the
2026-05-21 regression. The previous defaults walked ONLY ABARTH (alphabetical
first), which has zero articles in ZF's India catalog → spider shipped 0 rows.
With the representative defaults each variant samples 30 AGs which is enough to
catch popular parts (brakes, suspension, clutch, steering) for any popular
Indian PV make.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.zf")

BASE = "https://aftermarket.zf.com"
OPC = f"{BASE}/functions/controller/opc"

# ZF sub-brands: LEMFÖRDER, SACHS, TRW, BOGE, +others
# NOTE: 2026-05-22 - removed brandIDs from COMMON_QS. Including this filter on
# getArticlesForFilter returned `content: []` for every Indian PV manufacturer
# (Maruti, AUDI, BMW). Dropping the filter unblocks article extraction. ZF brand
# info still arrives per-article via `brandName` so we can post-filter if needed.
BRAND_IDS = [14, 22, 32, 35, 68, 126, 161, 294, 8888]

LANG_ID = 4
COUNTRY_ID = "IND"
VEHICLE_TYPE_ID = "P"  # Passenger Cars
COMMON_QS = f"languageID={LANG_ID}&countryID={COUNTRY_ID}&vehicleTypeIDs={VEHICLE_TYPE_ID}"

HDRS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/en/aftermarket-portal/our-catalog/search-by-vehicle/",
}

UA = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
TIMEOUT_MS = 20_000

# Spinny-relevant India PV makes. The site's vehicleTypeIDs=P gate is permissive
# and lets 2W (KTM/Aprilia/Husqvarna/Bimota/Fantic/GasGas), CV (Ashok Leyland/
# Bajaj Tempo/Bedford/Iveco/Bristol/Freight Rover/DAF), and ultra-luxury (Ferrari/
# Bentley/Aston Martin/Lamborghini) through. Walking those wastes ~70% of wall-
# clock for 0 Spinny-relevant rows. Override via ZF_MFR_WHITELIST="A,B,C" env var.
DEFAULT_MFR_WHITELIST = {
    "MARUTI", "HYUNDAI", "TATA", "MAHINDRA", "HONDA", "TOYOTA",
    "KIA", "RENAULT", "NISSAN", "SKODA", "VOLKSWAGEN", "FORD",
    "MERCEDES-BENZ", "BMW", "AUDI",  # premium PV — Spinny does sell these
    "FIAT", "CHEVROLET", "DATSUN", "MITSUBISHI", "JAGUAR LAND ROVER",
    "VOLVO",
}

# Outer picture-group whitelist DISABLED by default — empirical smoke (2026-05-26)
# showed AUDI's article-yielding AGs spread across buckets beyond the PV-relevant
# 10 (cut yield from 85 → 19). Different vehicle makes expose different bucket
# names (Picto*), so a static whitelist over-prunes. Override via ZF_PAG_BUCKETS
# only if you've enumerated the buckets for the specific scope and confirmed
# yield. Empty default means "walk all returned buckets".
DEFAULT_PAG_BUCKETS: set[str] = set()


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        # Defaults below are "representative" scope (production-ready); see docstring.
        self.max_mfrs = int(os.environ.get("ZF_MAX_MFRS", "0") or "0")
        self.max_models = int(os.environ.get("ZF_MAX_MODELS_PER_MFR", "0") or "0")
        self.max_variants = int(os.environ.get("ZF_MAX_VARIANTS_PER_MDL", "1") or "1")
        self.max_assemblygroups = int(os.environ.get("ZF_MAX_ASSEMBLYGROUPS", "30") or "30")
        # 2026-05-26 optimizations: avoid pointless empty-AG fetches.
        wl = os.environ.get("ZF_MFR_WHITELIST", "").strip()
        if wl:
            self.mfr_whitelist = {w.strip().upper() for w in wl.split(",") if w.strip()}
        else:
            self.mfr_whitelist = set(DEFAULT_MFR_WHITELIST)
        pb = os.environ.get("ZF_PAG_BUCKETS", "").strip()
        if pb:
            self.pag_buckets = {b.strip() for b in pb.split(",") if b.strip()}
        else:
            self.pag_buckets = set(DEFAULT_PAG_BUCKETS)
        # Skip the rest of a variant's AGs after this many consecutive empties.
        # DISABLED BY DEFAULT (set to 0) — empirical smoke (2026-05-26) showed ZF's
        # article distribution is scattered (~3-5% AG-hit rate), so 5-empty streak
        # before a hit is the norm. False-positive-skips real long-tail hits and
        # cost AUDI 100% of yield. Override via ZF_EMPTY_STREAK_SKIP if you've
        # profiled the variant data and know hits cluster at the front of the AG list.
        # Note: incompatible with concurrent inner-loop (ZF_CONCURRENCY > 1) — when
        # concurrency is on, streak is silently disabled because we no longer have
        # a deterministic call order to count empties against.
        self.empty_streak_skip = int(os.environ.get("ZF_EMPTY_STREAK_SKIP", "0") or "0")
        # 2026-05-27 concurrency layer. The inner AG loop is 93.5% of all traffic
        # and totally I/O-bound. Fan out N requests at once via httpx.AsyncClient
        # to cut wall-clock ~Nx without changing call count. Default 3 — empirical
        # smoke at C=5 tripped sporadic 502s on getManufacturers immediately after
        # a 30-req burst; C=3 stays comfortably below the unwritten rate limit.
        # Set to 1 to fall back to fully sequential.
        self.concurrency = max(1, int(os.environ.get("ZF_CONCURRENCY", "3") or "3"))
        # 2026-05-30 hit cache. ZF's getArticlesForFilter has a 3.2% hit rate —
        # 96.8% of calls return empty. By persisting which (mfr,model,vehicles,ag)
        # tuples yielded data, subsequent runs can skip the 28,880 known-empty
        # calls and only fire the ~960 productive ones. Expected first-run pays
        # the full 3.5h cost; subsequent runs drop to ~15 min.
        #   ZF_USE_CACHE=1     enable cache read (default 0 — full discovery)
        #   ZF_WRITE_CACHE=1   write cache at end of run (default 1 always)
        self.use_cache = bool(int(os.environ.get("ZF_USE_CACHE", "0") or "0"))
        self.write_cache = bool(int(os.environ.get("ZF_WRITE_CACHE", "1") or "1"))
        self.cache_path = self._find_latest_cache() if self.use_cache else None
        self.hit_cache: set[tuple[int, int, str, int]] = self._load_cache() if self.cache_path else set()

    @staticmethod
    def _find_latest_cache() -> Optional["Path"]:
        from pathlib import Path
        output_dir = Path("output")
        if not output_dir.exists():
            return None
        candidates = sorted(output_dir.glob("*/zf_hit_cache.json"), reverse=True)
        return candidates[0] if candidates else None

    def _load_cache(self) -> set[tuple[int, int, str, int]]:
        if not self.cache_path or not self.cache_path.exists():
            log.info("ZF: no hit-cache found, falling back to full discovery")
            return set()
        try:
            data = json.loads(self.cache_path.read_text())
        except Exception as e:
            log.warning("ZF: hit-cache parse err %s, falling back: %s", self.cache_path, e)
            return set()
        # Cache stored as list of [mfr_id, mdl_id, veh_ids_str, ag_id]
        cache = {tuple(t) for t in data.get("hits", [])}
        log.info("ZF: loaded hit-cache from %s — %d productive tuples", self.cache_path, len(cache))
        return cache

    def _write_cache(self, hits: set[tuple[int, int, str, int]],
                    elapsed_s: float, total_calls: int) -> None:
        """Write the hit cache for the next run to use."""
        from pathlib import Path
        import datetime as dt
        if not self.write_cache or not hits:
            return
        out_dir = Path("output") / dt.date.today().strftime("%Y%m%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_path = out_dir / "zf_hit_cache.json"
        cache_path.write_text(json.dumps({
            "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_s": elapsed_s,
            "total_calls": total_calls,
            "hit_count": len(hits),
            "hits": [list(h) for h in sorted(hits)],
        }, indent=2))
        log.info("ZF: wrote hit cache to %s (%d productive tuples)", cache_path, len(hits))

    def crawl(self) -> list[Row]:
        return asyncio.run(self._crawl_async())

    async def _crawl_async(self) -> list[Row]:
        log.info("ZF: spider crawl() entered (concurrency=%d, use_cache=%s, cache_size=%d)",
                 self.concurrency, self.use_cache, len(self.hit_cache))
        rows: list[Row] = []
        # articleID → {item_name, compat:set[str], brand_name}
        seen: dict[str, dict] = {}
        # Productive (mfr_id, mdl_id, veh_ids_str, ag_id) tuples → next-run cache
        productive_hits: set[tuple[int, int, str, int]] = set()
        t_start = time.time()
        sem = asyncio.Semaphore(self.concurrency)

        # Switched from sync httpx.Client to AsyncClient 2026-05-27 — inner AG
        # loop fan-out cuts the 7.6h serial wall-clock to ~1.5h at concurrency=5.
        # 2026-05-28: keep-alive reuse on AsyncClient was triggering ZF's WAF
        # to return 502 on the second-or-later request reusing a connection (curl
        # always got 200 on the same URLs with fresh connections). Forcing
        # max_keepalive_connections=0 makes each request open a fresh TCP+TLS
        # session — slower but reliable. Trade: ~150ms extra per request.
        async with httpx.AsyncClient(
            headers={"User-Agent": UA, **HDRS},
            follow_redirects=True,
            timeout=30,
            limits=httpx.Limits(max_connections=self.concurrency,
                                max_keepalive_connections=0),
            http2=False,
        ) as client:
            try:
                # 1. Manufacturers
                mfrs = await self._aget(client, f"{OPC}/getManufacturers?{COMMON_QS}", sem)
                mfr_list = mfrs if isinstance(mfrs, list) else (mfrs or {}).get("content", [])
                log.info("ZF: %d manufacturers (PV in India)", len(mfr_list))
                if not mfr_list:
                    log.error("ZF: getManufacturers returned empty — aborting")
                    return rows
                if self.mfr_whitelist:
                    before = len(mfr_list)
                    mfr_list = [m for m in mfr_list
                                if m.get("name", "").upper() in self.mfr_whitelist]
                    log.info("ZF: mfr whitelist filtered %d → %d", before, len(mfr_list))
                if self.max_mfrs:
                    mfr_list = mfr_list[: self.max_mfrs]

                for mfr_idx, mfr in enumerate(mfr_list, start=1):
                    mfr_id = mfr.get("manufacturerID")
                    mfr_name = mfr.get("name", "?")
                    log.info("[%d/%d] %s — elapsed %.0fs, %d unique articles so far",
                             mfr_idx, len(mfr_list), mfr_name,
                             time.time() - t_start, len(seen))

                    # 2. Models
                    models = await self._aget(
                        client, f"{OPC}/getModels?{COMMON_QS}&manufacturerID={mfr_id}", sem)
                    mdl_list = models if isinstance(models, list) else (models or {}).get("content", [])
                    if self.max_models:
                        mdl_list = mdl_list[: self.max_models]
                    log.info("  %s → %d models", mfr_name, len(mdl_list))

                    for mdl in mdl_list:
                        mdl_id = mdl.get("modelID")
                        mdl_name = mdl.get("name", "?")

                        # 3. GroupedVehicles
                        gv = await self._aget(
                            client, f"{OPC}/getGroupedVehicles?{COMMON_QS}&modelID={mdl_id}", sem)
                        gv_list = (gv or {}).get("content", []) if isinstance(gv, dict) else (gv or [])
                        if self.max_variants:
                            gv_list = gv_list[: self.max_variants]
                        log.info("    %s → %d variants", mdl_name, len(gv_list))

                        for variant in gv_list:
                            veh_ids = variant.get("vehicleIDs") or []
                            if not veh_ids:
                                continue
                            veh_ids_str = ",".join(str(v) for v in veh_ids)
                            variant_name = variant.get("name", "?")
                            compat_token = f"{mfr_name} | {mdl_name} | {variant_name}"

                            # 4. Picture assembly groups
                            pag_url = (f"{OPC}/getAllPictureAssemblyGroups?{COMMON_QS}"
                                       f"&manufacturerID={mfr_id}&modelID={mdl_id}"
                                       f"&vehicleIDs={veh_ids_str}")
                            pag = await self._aget(client, pag_url, sem)
                            pag_list = pag if isinstance(pag, list) else (pag or {}).get("content", [])
                            # Flatten to inner assemblyGroupIDs, filtered by PV-relevant
                            # outer picture buckets (PictoBremse/Kupplung/Lenkung/...).
                            ag_ids = []
                            for outer in pag_list:
                                bucket = outer.get("pictureAssemblyGroupID")
                                if self.pag_buckets and bucket not in self.pag_buckets:
                                    continue
                                for inner in outer.get("assemblyGroups", []):
                                    ag = inner.get("assemblyGroupID")
                                    if ag:
                                        ag_ids.append(ag)
                            if self.max_assemblygroups:
                                ag_ids = ag_ids[: self.max_assemblygroups]

                            # 2026-05-30 hit-cache: skip AGs not in last run's cache
                            ag_ids_before = len(ag_ids)
                            if self.use_cache and self.hit_cache:
                                ag_ids = [
                                    a for a in ag_ids
                                    if (mfr_id, mdl_id, veh_ids_str, a) in self.hit_cache
                                ]
                            log.info("      %s → %d AGs%s",
                                     variant_name, len(ag_ids),
                                     f" (cache-filtered from {ag_ids_before})"
                                     if self.use_cache and self.hit_cache else "")

                            if not ag_ids:
                                continue

                            # 5. Fetch all AGs concurrently for this variant. The
                            # empty-streak heuristic is incompatible with parallel
                            # ordering — when concurrency > 1, all AGs are fired.
                            if self.concurrency > 1:
                                ag_results = await self._fetch_ag_batch(
                                    client, sem, ag_ids, mfr_id, mdl_id, veh_ids_str)
                            else:
                                ag_results = await self._fetch_ag_sequential(
                                    client, sem, ag_ids, mfr_id, mdl_id, veh_ids_str)

                            for ag_id, articles in ag_results:
                                if not articles:
                                    continue
                                # Record this productive tuple for the next run's cache
                                productive_hits.add((mfr_id, mdl_id, veh_ids_str, ag_id))
                                log.info("        ag=%s → %d articles", ag_id, len(articles))
                                for art in articles:
                                    code = (art.get("articleID") or "").strip()
                                    name = (art.get("name") or "").strip()
                                    if not code or not name:
                                        continue
                                    entry = seen.get(code)
                                    if entry:
                                        entry["compat"].add(compat_token)
                                    else:
                                        seen[code] = {
                                            "name": name,
                                            "brand_name": (art.get("brandName") or "").strip(),
                                            "compat": {compat_token},
                                        }
            except Exception as exc:
                log.exception("ZF: unhandled error mid-crawl: %s", exc)

        for code, info in seen.items():
            # Prefix item_name with ZF brand if present (e.g. "LEMFÖRDER Tie Rod End")
            display_name = info["name"]
            if info.get("brand_name"):
                display_name = f"{info['brand_name']} {display_name}"
            rows.append(Row(
                item_name=display_name,
                item_code=code,
                compatible_car_model="; ".join(sorted(info["compat"])),
            ))
        elapsed = time.time() - t_start
        log.info("ZF: %d unique articles extracted in %.0fs (%.1f min)",
                 len(rows), elapsed, elapsed / 60)

        # Persist hit cache for next month's run
        self._write_cache(productive_hits, elapsed, total_calls=0)

        return rows

    # ---------- helpers ----------

    @staticmethod
    def _articles_url(mfr_id, mdl_id, veh_ids_str, ag_id) -> str:
        return (f"{OPC}/getArticlesForFilter?{COMMON_QS}"
                f"&limitToBrandIDs=&manufacturerID={mfr_id}"
                f"&modelID={mdl_id}&vehicleIDs={veh_ids_str}"
                f"&assemblyGroupIDs={ag_id}&tolerances="
                f"&parameter=&hubProfile=")

    async def _fetch_ag_batch(self, client, sem, ag_ids, mfr_id, mdl_id, veh_ids_str):
        """Parallel — fire all AG fetches at once, gated by semaphore. Returns
        [(ag_id, articles_list), ...] preserving input ag_ids order."""
        async def one(ag_id):
            url = self._articles_url(mfr_id, mdl_id, veh_ids_str, ag_id)
            resp = await self._aget(client, url, sem)
            articles = (resp or {}).get("content", []) if isinstance(resp, dict) else []
            return ag_id, articles
        return await asyncio.gather(*(one(ag) for ag in ag_ids))

    async def _fetch_ag_sequential(self, client, sem, ag_ids, mfr_id, mdl_id, veh_ids_str):
        """Sequential fallback (concurrency=1) — applies empty_streak_skip if set."""
        out = []
        empty_streak = 0
        for i, ag_id in enumerate(ag_ids):
            url = self._articles_url(mfr_id, mdl_id, veh_ids_str, ag_id)
            resp = await self._aget(client, url, sem)
            articles = (resp or {}).get("content", []) if isinstance(resp, dict) else []
            out.append((ag_id, articles))
            if not articles:
                empty_streak += 1
                if self.empty_streak_skip and empty_streak >= self.empty_streak_skip:
                    skipped = len(ag_ids) - i - 1
                    if skipped > 0:
                        log.info("        skipping remaining %d AGs (%d-empty streak)",
                                 skipped, empty_streak)
                    break
            else:
                empty_streak = 0
        return out

    @staticmethod
    async def _aget(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore):
        """Async GET + JSON-decode under a semaphore. Retries transient errors
        (5xx, 429, network) up to 3 times with exponential backoff. Logs warnings
        on permanent failure so silent regressions surface in INFO-level output."""
        async with sem:
            last_err = None
            for attempt in range(3):
                try:
                    r = await client.get(url)
                except Exception as e:
                    last_err = f"network err: {e}"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                # 5xx and 429 = retry-worthy; 4xx other = give up
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"status={r.status_code}"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    log.warning("ZF GET %s → status=%d (no retry)", url[:120], r.status_code)
                    return None
                try:
                    return r.json()
                except Exception as e:
                    log.warning("ZF GET %s → JSON decode failed: %s", url[:120], e)
                    return None
            log.warning("ZF GET %s → %s after 3 retries", url[:120], last_err)
            return None
