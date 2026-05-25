#!/usr/bin/env python3
"""
spike_explainer.py

Analyzes rank movement from a diff_detector.py output CSV.
Optimized for daily automated prototype: selects the single top upward mover,
generates a query plan, respects strict Tavily & YouTube API budgets, and gathers
contextual evidence to build LLM-ready explanation templates.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from dateutil import parser as date_parser
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Constants & Defaults ---
TOP_N_SPIKES_TO_ANALYZE = 1
BASE_DIR = "/content/drive/MyDrive/steam_diploma"
OUTPUT_DIR = "/content/drive/MyDrive/steam_diploma/data/spike_explainer"
COMPARISONS_DIR = "/content/drive/MyDrive/steam_diploma/data/comparisons"

# Time Windows
EXTENDED_WINDOW_DAYS_BEFORE = 45
EXTENDED_WINDOW_DAYS_AFTER = 3

# Tavily Budget
MAX_TAVILY_TOTAL_QUERIES_PER_RUN = 15
MAX_TAVILY_GENERAL_QUERIES_PER_GAME = 10
MAX_TAVILY_SOCIAL_QUERIES_PER_GAME = 5
TAVILY_SEARCH_DEPTH = "basic"
TAVILY_MAX_RESULTS_PER_QUERY = 5

# YouTube Budget
MAX_YOUTUBE_QUERIES_PER_GAME = 3
MAX_YOUTUBE_RESULTS_PER_QUERY = 5

# --- Setup Logging ---
def setup_logging(output_dir: Path, debug: bool):
    output_dir.mkdir(parents=True, exist_ok=True)
    log_level = logging.DEBUG if debug else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    
    logging.basicConfig(level=log_level, format=log_format, datefmt="%Y-%m-%d %H:%M:%S")
    
    file_handler = logging.FileHandler(output_dir / "errors.log")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)

# --- Helper Functions ---
def slugify(value: str) -> str:
    value = str(value).lower()
    value = re.sub(r'[^a-z0-9]+', '_', value)
    return value.strip('_')

def parse_date(date_str: Any, default: datetime = None) -> Optional[datetime]:
    if pd.isna(date_str) or not date_str:
        return default
    try:
        dt = date_parser.parse(str(date_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return default

def is_true(val: Any) -> bool:
    if pd.isna(val): return False
    if isinstance(val, bool): return val
    return str(val).lower() in ['true', '1', 'yes', 'y']

# --- Query Planning ---
def build_tavily_query_plan(title: str, social_enabled: bool) -> list:
    plan = []
    
    # Priority 1: General
    general_qs = [
        f'"{title}" Steam', f'"{title}" trailer', f'"{title}" gameplay',
        f'"{title}" demo', f'"{title}" release date', f'"{title}" announcement',
        f'"{title}" preview', f'"{title}" review', f'"{title}" publisher', f'"{title}" developer'
    ]
    for q in general_qs:
        plan.append({'query': q, 'query_group': 'general', 'priority': 1})
        
    # Priority 2: Press
    press_qs = [
        f'site:pcgamer.com "{title}"', f'site:rockpapershotgun.com "{title}"',
        f'site:ign.com "{title}"', f'site:gamespot.com "{title}"', f'site:game8.co "{title}"'
    ]
    for q in press_qs:
        plan.append({'query': q, 'query_group': 'press', 'priority': 2})
        
    # Priority 3 & 4: Social
    if social_enabled:
        plan.append({'query': f'site:reddit.com "{title}"', 'query_group': 'social', 'priority': 3})
        social_qs = [
            f'site:tiktok.com "{title}"', f'site:instagram.com "{title}"',
            f'site:x.com "{title}"', f'site: "{title}"'
        ]
        for q in social_qs:
            plan.append({'query': q, 'query_group': 'social', 'priority': 4})
            
    # Sort strictly by priority
    return sorted(plan, key=lambda x: x['priority'])

def build_youtube_query_plan(title: str, changed_fields: list, max_queries: int) -> list:
    queries = [f'"{title}" trailer', f'"{title}" gameplay', f'"{title}" demo']
    
    if "price" in changed_fields or "release_date" in changed_fields:
        # Swap out the lowest priority (demo) for an announcement/release query
        if "release_date" in changed_fields:
            queries[2] = f'"{title}" release date'
        else:
            queries[2] = f'"{title}" announcement'
            
    return queries[:max_queries]

# --- API Clients ---
class APIClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SteamSpikeExplainer/1.0"})

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    def fetch_steam_news(self, appid: str) -> dict:
        url = f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={appid}&count=20&maxlength=5000"
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        return response.json()

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    def fetch_youtube_search(self, query: str, api_key: str, limit: int) -> dict:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {"part": "snippet", "q": query, "type": "video", "order": "relevance", "maxResults": limit, "key": api_key}
        response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
    def fetch_tavily_search(self, query: str, api_key: str, limit: int) -> dict:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key, "query": query, "search_depth": TAVILY_SEARCH_DEPTH,
            "include_answer": False, "include_images": False, "include_raw_content": False, "max_results": limit
        }
        response = self.session.post(url, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()

# --- Core Logic ---
def find_latest_diff_csv(base_dir: Path) -> Optional[Path]:
    candidates = list(base_dir.rglob("*.csv"))
    diff_files = [f for f in candidates if "diff" in f.name or "rank_changes" in f.name]
    if not diff_files: return None
    diff_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return diff_files[0]

def select_top_spikes(df: pd.DataFrame, top_n: int) -> pd.DataFrame:
    df_spikes = df.copy()
    
    # Map input columns
    col_map = {'title_new': 'curr_title', 'rank_old': 'prev_rank', 'rank_new': 'curr_rank'}
    df_spikes.rename(columns={k: v for k, v in col_map.items() if k in df_spikes.columns}, inplace=True)
    
    # Ensure rank_delta
    if 'rank_delta' not in df_spikes.columns and 'prev_rank' in df_spikes.columns and 'curr_rank' in df_spikes.columns:
        df_spikes['rank_delta'] = df_spikes['prev_rank'] - df_spikes['curr_rank']
        
    df_spikes['rank_delta'] = pd.to_numeric(df_spikes.get('rank_delta', 0), errors='coerce').fillna(0)
    
    # Identify upward movers
    is_up = False
    if 'movement_type' in df_spikes.columns:
        is_up = is_up | (df_spikes['movement_type'] == 'up')
    if 'spike_type' in df_spikes.columns:
        is_up = is_up | (df_spikes['spike_type'] == 'upward_spike')
        
    df_spikes = df_spikes[(is_up) | (df_spikes['rank_delta'] > 0)]
    
    # Sort
    if 'movement_score' in df_spikes.columns:
        df_spikes = df_spikes.sort_values(by=['movement_score', 'rank_delta'], ascending=[False, False])
    else:
        df_spikes = df_spikes.sort_values(by='rank_delta', ascending=False)
        
    return df_spikes.head(top_n)

def extract_game_metadata(row: pd.Series, args) -> dict:
    now = datetime.now(timezone.utc)
    
    # Time Window Inference
    fallback_used = False
    if args.old_snapshot_date and args.new_snapshot_date:
        dt_start = parse_date(args.old_snapshot_date, now - timedelta(days=7))
        dt_end = parse_date(args.new_snapshot_date, now)
    else:
        if 'old_captured_at_utc' in row and pd.notna(row['old_captured_at_utc']):
            dt_start = parse_date(row['old_captured_at_utc'])
            dt_end = parse_date(row.get('new_captured_at_utc', now))
        else:
            dt_start = now - timedelta(days=7)
            dt_end = now
            fallback_used = True

    extended_start = dt_start - timedelta(days=EXTENDED_WINDOW_DAYS_BEFORE)
    extended_end = dt_end + timedelta(days=EXTENDED_WINDOW_DAYS_AFTER)

    title = str(row.get('curr_title') or row.get('title') or "Unknown Game")
    
    # Changed fields
    changed = []
    if is_true(row.get('price_changed')): changed.append("price")
    if is_true(row.get('release_changed')): changed.append("release_date")
    if str(row.get('title_old', '')) != str(row.get('curr_title', '')) and pd.notna(row.get('title_old')):
        changed.append("title")

    return {
        "appid": str(row.get('appid', '')),
        "game_title": title,
        "safe_slug": slugify(title),
        "analysis_date_utc": now.isoformat(),
        "spike_window_start": dt_start.isoformat(),
        "spike_window_end": dt_end.isoformat(),
        "extended_window_start": extended_start.isoformat(),
        "extended_window_end": extended_end.isoformat(),
        "dt_start": dt_start,
        "dt_end": dt_end,
        "ext_start": extended_start,
        "ext_end": extended_end,
        "window_inferred_fallback": fallback_used,
        "prev_rank": row.get('prev_rank'),
        "curr_rank": row.get('curr_rank'),
        "rank_delta": row.get('rank_delta'),
        "movement_score": row.get('movement_score', 0),
        "is_new_entry": pd.isna(row.get('prev_rank')),
        "changed_fields_detected": changed,
        "input_diff_row": row.fillna("").to_dict()
    }

def classify_date(pub_date: Optional[datetime], meta: dict) -> str:
    if not pub_date: return "unknown_date"
    if meta['dt_start'] <= pub_date <= meta['dt_end']: return "inside_spike_window"
    if meta['ext_start'] <= pub_date <= meta['ext_end']: return "inside_extended_window"
    return "outside_window"

def detect_keywords(text: str) -> list:
    kws = ["trailer", "gameplay", "demo", "announcement", "release date", "showcase"]
    text_lower = text.lower()
    return [kw for kw in kws if kw in text_lower]

def score_evidence(evidence_list: list, meta: dict) -> list:
    scored = []
    title_lower = meta["game_title"].lower()
    changed = meta["changed_fields_detected"]
    
    for ev in evidence_list:
        score = 0
        reasons = []
        
        # Date
        date_class = ev.get("date_classification", "unknown_date")
        if date_class == "inside_spike_window":
            score += 30; reasons.append("Inside spike window (+30)")
        elif date_class == "inside_extended_window":
            score += 15; reasons.append("Inside extended window (+15)")
            
        # Source / Type
        ev_type = ev.get("evidence_type", "")
        if ev_type == "steam_news":
            score += 20; reasons.append("Official Steam News (+20)")
        elif ev_type == "youtube_video":
            score += 15; reasons.append("YouTube (+15)")
        elif ev_type == "press_article":
            score += 15; reasons.append("Press (+15)")
        elif ev_type == "reddit_discussion" or "reddit.com" in str(ev.get("url", "")):
            score += 10; reasons.append("Reddit (+10)")
        elif ev_type == "social_post" or any(s in str(ev.get("url", "")) for s in ["tiktok.com", "instagram.com", "x.com"]):
            score += 5; reasons.append("Social Index (+5)")
            
        # Text Matches
        text_block = (str(ev.get("title", "")) + " " + str(ev.get("snippet", "")) + " " + str(ev.get("contents_preview", ""))).lower()
        if title_lower in text_block:
            score += 20; reasons.append("Exact title match (+20)")
            
        kws = detect_keywords(text_block)
        if kws:
            score += 20; reasons.append(f"Keywords {kws} (+20)")
            ev["matched_keywords"] = ", ".join(kws)
            
        if ("price" in changed and "price" in text_block) or ("release_date" in changed and "release" in text_block):
            score += 10; reasons.append("Mentions changed Store field (+10)")
            
        score = min(score, 100)
        conf = "weak" if score <= 30 else "medium" if score <= 60 else "strong" if score <= 85 else "very_strong"
        
        ev["evidence_score"] = score
        ev["confidence_label"] = conf
        ev["reason_for_score"] = "; ".join(reasons)
        scored.append(ev)
        
    return sorted(scored, key=lambda x: x["evidence_score"], reverse=True)

def generate_llm_input(meta: dict, scored: list) -> str:
    # Group evidence
    ev_steam = [e for e in scored if e['evidence_type'] == 'steam_news']
    ev_yt = [e for e in scored if e['evidence_type'] == 'youtube_video']
    ev_web = [e for e in scored if e['evidence_type'] not in ['steam_news', 'youtube_video'] and e.get('query_group') in ['general', 'press']]
    ev_soc = [e for e in scored if e.get('query_group') == 'social']
    
    def format_ev(ev_list, limit=5):
        if not ev_list: return "None found.\n"
        out = ""
        for e in ev_list[:limit]:
            out += f"- [{e['evidence_score']}/100 | {e['date_classification']}] {e.get('title', 'No Title')}\n"
            if e.get('url'): out += f"  URL: {e.get('url')}\n"
            preview = str(e.get('contents_preview') or e.get('snippet') or e.get('description_preview') or '')
            if preview: out += f"  Preview: {preview[:150].strip()}...\n"
        return out + "\n"

    return f"""# SPIKE ANALYSIS INPUT

## 1. SPIKE OVERVIEW
**Game**: {meta['game_title']} (AppID: {meta['appid']})
**Movement**: Rank {meta['prev_rank']} -> {meta['curr_rank']} (Delta: +{meta['rank_delta']})
**New Entry**: {meta['is_new_entry']}

## 2. TIME WINDOWS
**Spike Window**: {meta['spike_window_start']} to {meta['spike_window_end']}
**Extended Window**: {meta['extended_window_start']} to {meta['extended_window_end']}
*Note: Inferred Fallback: {meta['window_inferred_fallback']}*

## 3. STORE CHANGES
**Changed Fields**: {', '.join(meta['changed_fields_detected']) if meta['changed_fields_detected'] else 'None'}

## 4. STEAM NEWS EVIDENCE
{format_ev(ev_steam)}
## 5. TAVILY WEB SEARCH EVIDENCE
{format_ev(ev_web)}
## 6. YOUTUBE EVIDENCE
{format_ev(ev_yt)}
## 7. SOCIAL / PLATFORM-SPECIFIC INDEXED RESULTS
{format_ev(ev_soc)}
## 8. TOP SCORED EVIDENCE (OVERALL)
{format_ev(scored, 3)}
## 9. MISSING OR WEAK EVIDENCE
Look at the above. If no high-score evidence is found inside the spike window, attribution is weak.

## 10. ADVANCED CAUSES NOT CHECKED AUTOMATICALLY
If automated evidence is lacking, consider these unverified possibilities:
- Giveaway/discount of a related base game/prequel
- Publisher-wide sale campaign
- In-game cross-promotion
- Non-indexed influencer campaign (e.g., live Twitch segment)
- Platform-side visibility boost (Steam algorithm)
- Franchise-level marketing beat

---
**TASK FOR LLM:**
Use only the evidence below. Do not invent facts. Separate confirmed facts from hypotheses. If evidence is weak, say so. Explain the most likely causes of the Steam Top Wishlists rank movement. Consider that absence of evidence is not proof of absence. Mention advanced causes only as unverified possibilities if not supported by evidence.

Produce:
1. Short executive summary
2. Confirmed facts
3. Likely causes of the spike
4. Weak or uncertain hypotheses
5. Recommended next manual checks
6. Marketing lessons for a developer
"""

def generate_human_summary(meta: dict, scored: list, budget: dict) -> str:
    strong_ev = [e for e in scored if e['evidence_score'] >= 60]
    
    return f"""# Spike Analysis: {meta['game_title']}

## Rank Movement
- Previous: {meta['prev_rank']} -> Current: {meta['curr_rank']} (Delta: +{meta['rank_delta']})
- Time Window: {meta['spike_window_start']} to {meta['spike_window_end']}

## API Budget Used
- Tavily Queries Executed: {budget.get('tavily_used', 0)} / {budget.get('tavily_limit', 0)}
- YouTube Queries Executed: {budget.get('yt_used', 0)} / {budget.get('yt_limit', 0)}

## Strongest Evidence
{'- ' + strong_ev[0].get('title', 'Unknown') + ' (' + str(strong_ev[0]['evidence_score']) + '/100)' if strong_ev else '- None found automatically.'}

## Likely Explanation & Weak Points
{"Evidence suggests a clear catalyst. See LLM/Top evidence." if strong_ev else "Automated evidence is weak. This could be an un-indexed influencer stream, cross-promotion, or algorithm boost."}

## Recommended Manual Checks
- Search Twitter/X and TikTok manually
- Check SteamDB for simultaneous player spikes on related franchise games
- Check Steam News history manually
"""

def write_csv(filepath: Path, data: list, fieldnames: list, empty_note: str = ""):
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        if data: writer.writerows(data)
        elif empty_note: f.write(f"# {empty_note}\n")

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="Daily Automated Steam Spike Explainer.")
    parser.add_argument("--diff-csv", type=str, help="Path to diff_detector output CSV, or 'latest' to auto-find in --comparisons-dir.")
    parser.add_argument("--top-n", type=int, default=TOP_N_SPIKES_TO_ANALYZE, help="Number of spikes to analyze (default 1).")
    parser.add_argument("--old-snapshot-date", type=str, help="Start of spike window (YYYY-MM-DD).")
    parser.add_argument("--new-snapshot-date", type=str, help="End of spike window (YYYY-MM-DD).")
    parser.add_argument("--base-dir", type=str, default=BASE_DIR)
    parser.add_argument("--output-dir", type=str, default=OUTPUT_DIR)
    parser.add_argument("--comparisons-dir", type=str, default=COMPARISONS_DIR, help="Directory to scan if --diff-csv is 'latest'.")
    
    # Tavily
    parser.add_argument("--search-provider", type=str, choices=["none", "tavily"], default="none")
    parser.add_argument("--tavily-api-key", type=str, default=os.getenv("TAVILY_API_KEY", ""))
    parser.add_argument("--max-tavily-total-queries", type=int, default=MAX_TAVILY_TOTAL_QUERIES_PER_RUN)
    parser.add_argument("--max-tavily-general-queries", type=int, default=MAX_TAVILY_GENERAL_QUERIES_PER_GAME)
    parser.add_argument("--max-tavily-social-queries", type=int, default=MAX_TAVILY_SOCIAL_QUERIES_PER_GAME)
    parser.add_argument("--tavily-max-results", type=int, default=TAVILY_MAX_RESULTS_PER_QUERY)
    parser.add_argument("--disable-social-search", action="store_true", help="Disable platform-specific social queries.")
    
    # YouTube
    parser.add_argument("--youtube-api-key", type=str, default=os.getenv("YOUTUBE_API_KEY", ""))
    parser.add_argument("--max-youtube-queries", type=int, default=MAX_YOUTUBE_QUERIES_PER_GAME)
    parser.add_argument("--max-youtube-results", type=int, default=MAX_YOUTUBE_RESULTS_PER_QUERY)
    
    parser.add_argument("--debug", action="store_true")
    
    args = parser.parse_args()
    outdir = Path(args.output_dir)
    setup_logging(outdir, args.debug)
    
    logging.info("Starting Daily Spike Explainer Prototype...")
    
    diff_path = None
    if args.diff_csv:
        if args.diff_csv.lower() == "latest":
            comp_dir = Path(args.comparisons_dir)
            if not comp_dir.exists() or not comp_dir.is_dir():
                logging.error(f"Comparisons directory not found: {comp_dir}")
                sys.exit(1)
            
            subfolders = [f for f in comp_dir.iterdir() if f.is_dir()]
            subfolders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            priority_files = ["rank_changes.csv", "spikes_up.csv", "top_movers.csv", "upward_movers.csv"]
            
            for folder in subfolders:
                for fname in priority_files:
                    candidate = folder / fname
                    if candidate.exists() and candidate.is_file():
                        logging.info(f"Using latest comparison folder: {folder}")
                        logging.info(f"Using diff CSV: {candidate}")
                        diff_path = candidate
                        break
                if diff_path:
                    break
                    
            if not diff_path:
                logging.error(f"Could not find any prioritized diff CSV in {args.comparisons_dir}")
                sys.exit(1)
        else:
            diff_path = Path(args.diff_csv)
    else:
        diff_path = find_latest_diff_csv(Path(args.base_dir))
        
    if not diff_path or not diff_path.exists():
        logging.error("Could not find a valid diff CSV to analyze.")
        sys.exit(1)
        
    df_diff = pd.read_csv(diff_path)
    top_spikes_df = select_top_spikes(df_diff, args.top_n)
    if top_spikes_df.empty:
        logging.warning("No upward spikes found.")
        sys.exit(0)
        
    api = APIClient()
    index_rows = []
    skipped_queries = []
    
    tavily_used_total = 0
    yt_used_total = 0
    errors_count = 0
    
    for idx, row in top_spikes_df.iterrows():
        meta = extract_game_metadata(row, args)
        logging.info(f"Analyzing Top Spike: [{meta['appid']}] {meta['game_title']} (+{meta['rank_delta']} ranks)")
        
        prank = meta['prev_rank'] if pd.notna(meta['prev_rank']) else "new"
        folder_name = f"{meta['dt_end'].strftime('%Y-%m-%d')}__appid_{meta['appid']}__rank_{prank}_to_{meta['curr_rank']}__up_{meta['rank_delta']}"
        game_dir = outdir / folder_name
        game_dir.mkdir(parents=True, exist_ok=True)
        
        with open(game_dir / "metadata.json", "w") as f: json.dump(meta, f, indent=4, default=str)
            
        # 1. Steam News
        steam_news_processed = []
        try:
            raw = api.fetch_steam_news(meta["appid"])
            with open(game_dir / "steam_news_raw.json", "w") as f: json.dump(raw, f, indent=4)
            for item in raw.get("appnews", {}).get("newsitems", []):
                pub_dt = datetime.fromtimestamp(item.get("date", 0), tz=timezone.utc)
                contents = item.get("contents", "")
                steam_news_processed.append({
                    "appid": meta["appid"], "title": item.get("title", ""), "url": item.get("url", ""),
                    "published_at": pub_dt.isoformat(), "contents_preview": contents[:300],
                    "date_classification": classify_date(pub_dt, meta), "evidence_type": "steam_news"
                })
        except Exception as e:
            logging.error(f"Steam News API error: {e}"); errors_count += 1
        write_csv(game_dir / "steam_news.csv", steam_news_processed, ["appid", "title", "url", "published_at", "contents_preview", "date_classification", "evidence_type"])

        # 2. Query Plan & Tavily Search
        tavily_plan = build_tavily_query_plan(meta["game_title"], not args.disable_social_search)
        query_plan_csv = []
        web_results_processed = []
        
        tavily_gen_used = 0
        tavily_soc_used = 0
        
        for q in tavily_plan:
            q_str = q['query']; q_grp = q['query_group']
            q_row = {"appid": meta["appid"], "game_title": meta["game_title"], "query": q_str, "query_group": q_grp, "priority": q['priority'], "planned_provider": "tavily", "executed": False, "skipped_reason": "", "results_count": 0}
            
            # Check limits
            skip_reason = ""
            if args.search_provider != "tavily" or not args.tavily_api_key: skip_reason = "Tavily disabled/No key"
            elif tavily_used_total >= args.max_tavily_total_queries: skip_reason = "Global budget exhausted"
            elif q_grp in ['general', 'press'] and tavily_gen_used >= args.max_tavily_general_queries: skip_reason = "General budget exhausted"
            elif q_grp == 'social' and tavily_soc_used >= args.max_tavily_social_queries: skip_reason = "Social budget exhausted"
            
            if skip_reason:
                q_row["skipped_reason"] = skip_reason
                skipped_queries.append(q_row)
            else:
                try:
                    raw = api.fetch_tavily_search(q_str, args.tavily_api_key, args.tavily_max_results)
                    tavily_used_total += 1
                    if q_grp in ['general', 'press']: tavily_gen_used += 1
                    elif q_grp == 'social': tavily_soc_used += 1
                    
                    res_list = raw.get("results", [])
                    q_row["executed"] = True
                    q_row["results_count"] = len(res_list)
                    
                    for idx_r, result in enumerate(res_list):
                        web_results_processed.append({
                            "appid": meta["appid"], "query": q_str, "query_group": q_grp, "query_priority": q['priority'],
                            "provider": "tavily", "tavily_search_depth": TAVILY_SEARCH_DEPTH, "budget_query_number": tavily_used_total,
                            "url": result.get("url", ""), "title": result.get("title", ""), "snippet": result.get("content", ""),
                            "date_classification": "unknown_date", # basic search often lacks dates
                            "evidence_type": "press_article" if q_grp == 'press' else "reddit_discussion" if "reddit.com" in result.get("url","") else "social_post" if q_grp == 'social' else "web_result"
                        })
                except Exception as e:
                    logging.error(f"Tavily query failed ({q_str}): {e}"); errors_count += 1
                    q_row["skipped_reason"] = f"API Error: {e}"
            
            query_plan_csv.append(q_row)
            
        write_csv(game_dir / "query_plan.csv", query_plan_csv, list(query_plan_csv[0].keys()) if query_plan_csv else [])
        write_csv(game_dir / "web_search_raw.csv", web_results_processed, ["appid", "query", "query_group", "query_priority", "provider", "tavily_search_depth", "budget_query_number", "url", "title", "snippet", "date_classification", "evidence_type"])

        # 3. YouTube Search
        yt_plan = build_youtube_query_plan(meta["game_title"], meta["changed_fields_detected"], args.max_youtube_queries)
        yt_processed = []
        if args.youtube_api_key:
            for yq in yt_plan:
                try:
                    raw = api.fetch_youtube_search(yq, args.youtube_api_key, args.max_youtube_results)
                    yt_used_total += 1
                    for item in raw.get("items", []):
                        snip = item.get("snippet", {})
                        pub_dt = parse_date(snip.get("publishedAt"))
                        yt_processed.append({
                            "appid": meta["appid"], "query": yq, "video_id": item.get("id", {}).get("videoId", ""),
                            "url": f"https://www.youtube.com/watch?v={item.get('id', {}).get('videoId', '')}",
                            "title": snip.get("title", ""), "contents_preview": snip.get("description", ""),
                            "published_at": pub_dt.isoformat() if pub_dt else "", "date_classification": classify_date(pub_dt, meta),
                            "evidence_type": "youtube_video"
                        })
                except Exception as e:
                    logging.error(f"YouTube error ({yq}): {e}"); errors_count += 1
        write_csv(game_dir / "youtube_results.csv", yt_processed, ["appid", "query", "video_id", "url", "title", "contents_preview", "published_at", "date_classification", "evidence_type"])

        # 4. Generate Files & Scores
        write_csv(game_dir / "manual_evidence_template.csv", [], ["appid", "url", "title", "evidence_type", "notes"])
        
        all_evidence = steam_news_processed + web_results_processed + yt_processed
        scored = score_evidence(all_evidence, meta)
        ev_headers = ["appid", "evidence_type", "date_classification", "url", "title", "snippet", "contents_preview", "matched_keywords", "evidence_score", "confidence_label", "reason_for_score", "query_group"]
        write_csv(game_dir / "evidence_scored.csv", scored, ev_headers)

        with open(game_dir / "llm_summary_input.md", "w") as f: f.write(generate_llm_input(meta, scored))
        
        budget_info = {"tavily_used": tavily_used_total, "tavily_limit": args.max_tavily_total_queries, "yt_used": yt_used_total, "yt_limit": args.max_youtube_queries}
        with open(game_dir / "human_summary.md", "w") as f: f.write(generate_human_summary(meta, scored, budget_info))
        
        with open(game_dir / "README.md", "w") as f: f.write("# Prototype Spike Explainer\nRead `llm_summary_input.md` or pass it to an LLM.")

        index_rows.append({
            "analysis_date": meta["analysis_date_utc"], "appid": meta["appid"], "game_title": meta["game_title"],
            "folder_path": str(game_dir), "rank_delta": meta["rank_delta"],
            "top_evidence_score": scored[0]["evidence_score"] if scored else 0,
            "llm_summary_input_path": str(game_dir / "llm_summary_input.md")
        })

    # Output Master Files
    if index_rows:
        write_csv(outdir / "index.csv", index_rows, list(index_rows[0].keys()))
        if skipped_queries: write_csv(outdir / "skipped_queries.csv", skipped_queries, list(skipped_queries[0].keys()))
        
        run_meta = {
            "started_at": datetime.now(timezone.utc).isoformat(), "input_diff_csv": str(diff_path),
            "selected_appid": index_rows[0]['appid'], "selected_game_title": index_rows[0]['game_title'],
            "top_n": args.top_n, "old_snapshot_date": args.old_snapshot_date, "new_snapshot_date": args.new_snapshot_date,
            "tavily_enabled": args.search_provider == 'tavily' and bool(args.tavily_api_key),
            "tavily_queries_budget": args.max_tavily_total_queries, "tavily_queries_used": tavily_used_total,
            "youtube_enabled": bool(args.youtube_api_key), "youtube_queries_budget": args.max_youtube_queries, "youtube_queries_used": yt_used_total,
            "social_search_enabled": not args.disable_social_search, "output_dir": str(outdir),
            "finished_at": datetime.now(timezone.utc).isoformat(), "errors_count": errors_count, "skipped_queries_count": len(skipped_queries)
        }
        with open(outdir / "run_metadata.json", "w") as f: json.dump(run_meta, f, indent=4)
        
        with open(outdir / "daily_run_summary.md", "w") as f:
            f.write(f"# Daily Spike Run\nAnalyzed: {run_meta['selected_game_title']}\nTavily Budget Used: {tavily_used_total}\nErrors: {errors_count}\n")

    logging.info(f"Daily Prototype Run Complete. Evaluated {len(index_rows)} spikes. Errors: {errors_count}")

if __name__ == "__main__":
    main()