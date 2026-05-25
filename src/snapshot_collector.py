#!/usr/bin/env python3
"""
snapshot_collector.py

Robust script to collect Steam Top Wishlisted games rankings.
Restored exact backward compatibility with previous snapshot schema.
Optimized for faster crawling using a local work directory and speed modes.
"""

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
import uuid
import glob
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Tuple, List, Set, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

class SteamThrottleException(Exception):
    pass

class SteamSuspiciousPageException(Exception):
    pass

MAIN_SCHEMA_COLUMNS = [
    'snapshot_id',
    'captured_at_utc',
    'rank',
    'appid',
    'title',
    'release_text',
    'price_text',
    'game_url',
    'page_number',
    'position_on_page'
]

SPEED_PRESETS = {
    "fast": {"delay_min": 0.5, "delay_max": 1.5, "cooldown_every": 50, "cooldown_min": 10.0, "cooldown_max": 20.0, "long_every": 0, "long_min": 0.0, "long_max": 0.0},
    "normal": {"delay_min": 1.5, "delay_max": 3.0, "cooldown_every": 25, "cooldown_min": 15.0, "cooldown_max": 30.0, "long_every": 75, "long_min": 45.0, "long_max": 90.0},
    "safe": {"delay_min": 4.0, "delay_max": 8.0, "cooldown_every": 10, "cooldown_min": 30.0, "cooldown_max": 60.0, "long_every": 40, "long_min": 120.0, "long_max": 240.0}
}

def get_final_snapshot_dir(base_dir: Optional[str] = None) -> Path:
    if base_dir is None or str(base_dir).strip() == "":
        base_dir = "/content/drive/MyDrive/steam_diploma"
    snapshot_dir = Path(base_dir) / "data" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    return snapshot_dir

def parse_args():
    parser = argparse.ArgumentParser(description="Collect Steam Top Wishlists Ranking")
    
    # Directories
    parser.add_argument("--base-dir", type=str, default="", help="Base directory for final outputs")
    parser.add_argument("--work-dir", type=str, default="/content/steam_diploma_work", help="Working directory for temporary files")
    
    # Core settings
    parser.add_argument("--max-games", type=int, default=0, help="Max games to collect (0 = no limit)")
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages to collect (0 = no limit)")
    parser.add_argument("--speed-mode", type=str, choices=["fast", "normal", "safe"], default="normal", help="Pacing preset")
    parser.add_argument("--debug-save-all-html", action="store_true", help="Save HTML for every page fetched (slow)")
    parser.add_argument("--continue-on-suspicious", action="store_true", help="Continue even if page looks suspicious")
    
    # Resume support
    parser.add_argument("--resume", action="store_true", help="Resume from a previous run")
    parser.add_argument("--run-id", type=str, help="Snapshot ID to resume (required if --resume)")
    
    # Pacing Overrides
    parser.add_argument("--delay-min", type=float)
    parser.add_argument("--delay-max", type=float)
    parser.add_argument("--cooldown-every-pages", type=int)
    parser.add_argument("--cooldown-min", type=float)
    parser.add_argument("--cooldown-max", type=float)
    parser.add_argument("--long-cooldown-every-pages", type=int)
    parser.add_argument("--long-cooldown-min", type=float)
    parser.add_argument("--long-cooldown-max", type=float)

    args = parser.parse_args()
    return args

def apply_speed_config(args: argparse.Namespace) -> dict:
    config = SPEED_PRESETS[args.speed_mode].copy()
    
    # Apply CLI overrides if provided
    if args.delay_min is not None: config["delay_min"] = args.delay_min
    if args.delay_max is not None: config["delay_max"] = args.delay_max
    if args.cooldown_every_pages is not None: config["cooldown_every"] = args.cooldown_every_pages
    if args.cooldown_min is not None: config["cooldown_min"] = args.cooldown_min
    if args.cooldown_max is not None: config["cooldown_max"] = args.cooldown_max
    if args.long_cooldown_every_pages is not None: config["long_every"] = args.long_cooldown_every_pages
    if args.long_cooldown_min is not None: config["long_min"] = args.long_cooldown_min
    if args.long_cooldown_max is not None: config["long_max"] = args.long_cooldown_max
    
    return config

def setup_logging(work_dir: Path, timestamp: str):
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"snapshot_collector_{timestamp}.log"
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    
    # Clear existing handlers
    if logger.hasHandlers():
        logger.handlers.clear()
        
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    # File handler
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    return log_file

def build_session() -> requests.Session:
    session = requests.Session()
    session.cookies.set('birthtime', '283993201', domain='store.steampowered.com')
    session.cookies.set('lastagecheckage', '1-January-1979', domain='store.steampowered.com')
    session.cookies.set('wants_mature_content', '1', domain='store.steampowered.com')
    session.cookies.set('view_adult_content', '1', domain='store.steampowered.com')
    
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://store.steampowered.com/',
        'Connection': 'keep-alive',
    })
    return session

@retry(
    wait=wait_exponential(multiplier=2, min=2, max=20),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True
)
def fetch_page_with_retry(session: requests.Session, url: str) -> requests.Response:
    try:
        response = session.get(url, timeout=20)
        # 429/403 trigger explicit adaptive throttling, do not retry blindly
        if response.status_code in [429, 403]:
            raise SteamThrottleException(f"HTTP {response.status_code}")
        response.raise_for_status()
        return response
    except requests.exceptions.ConnectionError as e:
        logging.warning(f"Connection error: {e}. Retrying via tenacity...")
        raise

def apply_pacing(config: dict, pages_attempted: int):
    if pages_attempted == 0:
        return
        
    delay = random.uniform(config["delay_min"], config["delay_max"])
    total_sleep = delay
    cooldown_msg = ""
    
    if config["long_every"] > 0 and pages_attempted % config["long_every"] == 0:
        cd = random.uniform(config["long_min"], config["long_max"])
        total_sleep += cd
        cooldown_msg = f" | Long Cooldown: {cd:.1f}s"
    elif config["cooldown_every"] > 0 and pages_attempted % config["cooldown_every"] == 0:
        cd = random.uniform(config["cooldown_min"], config["cooldown_max"])
        total_sleep += cd
        cooldown_msg = f" | Short Cooldown: {cd:.1f}s"
        
    logging.info(f"Pacing: sleep {total_sleep:.1f}s{cooldown_msg}")
    time.sleep(total_sleep)

def validate_html(html: str) -> Tuple[bool, str]:
    html_lower = html.lower()
    suspicious_terms = [
        "captcha", "too many requests", "access denied", 
        "robot check", "are you a human", "blocked", "error code:"
    ]
    
    for term in suspicious_terms:
        if term in html_lower:
            return False, f"Suspicious term found: '{term}'"
            
    if 'id="search_resultsrows"' not in html_lower:
        return False, "Search results container missing"
        
    return True, "OK"

def extract_slot_containers(html: str) -> list:
    if not html: return []
    soup = BeautifulSoup(html, 'lxml')
    container = soup.find(id='search_resultsRows')
    return container.find_all('a', class_='search_result_row') if container else []

def parse_game_slot(slot: BeautifulSoup, rank: int, page_num: int, pos: int, snapshot_id: str, captured_at: str, source_url: str) -> dict:
    appid = slot.get('data-ds-appid', '')
    title_el = slot.find('span', class_='title')
    title = title_el.text.strip() if title_el else 'unknown game'
    
    release_el = slot.find('div', class_='search_released')
    release_text = release_el.text.strip() if release_el else ''
    
    raw_url = slot.get('href', '')
    clean_url = raw_url.split('?')[0] if raw_url else ''

    price_text = "Unknown"
    discount_text = ""
    price_block = slot.find('div', class_='search_price_discount_combined')
    
    if price_block:
        pct_el = price_block.find('div', class_='search_discount')
        if pct_el and pct_el.text.strip():
            discount_text = pct_el.text.strip()
            
        final_price_element = price_block.find('div', class_='discount_final_price')
        if final_price_element:
            price_text = final_price_element.text.strip()
        else:
            data_price_final = price_block.get('data-price-final')
            if data_price_final == '0':
                if any(kw in release_text for kw in ['Coming Soon', 'TBA', 'To be announced', '2025', '2026', 'Announced']):
                    price_text = 'Unpriced (Coming Soon)'
                else:
                    price_text = 'Free'
            elif data_price_final:
                price_text = f"${int(data_price_final)/100:.2f}"

    review_summary = ""
    rev_el = slot.find('span', class_=re.compile(r'search_review_summary'))
    if rev_el:
        review_summary = rev_el.get('data-tooltip-html', '')

    image_url = ""
    img_el = slot.find('img', class_='search_capsule')
    if img_el:
        image_url = img_el.get('src', img_el.get('srcset', '')).split(',')[0].strip().split(' ')[0]

    platforms = []
    plat_els = slot.find_all('span', class_='platform_img')
    for p in plat_els:
        classes = p.get('class', [])
        if len(classes) > 1:
            platforms.append(classes[1])

    return {
        'snapshot_id': snapshot_id, 
        'captured_at_utc': captured_at, 
        'rank': rank,
        'appid': appid, 
        'title': title,
        'release_text': release_text, 
        'price_text': price_text, 
        'game_url': clean_url,
        'page_number': page_num, 
        'position_on_page': pos,
        'source_url': source_url,
        'discount_text': discount_text,
        'review_summary': review_summary,
        'image_url': image_url,
        'platforms': ','.join(platforms),
        'tags_raw': ''
    }

def save_intermediate_batch(df_rows: list, progress_dir: Path, batch_index: int):
    if not df_rows: return
    page_csv = progress_dir / "pages" / f"batch_{batch_index:04d}.csv"
    page_csv.parent.mkdir(parents=True, exist_ok=True)
    
    with page_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=df_rows[0].keys())
        writer.writeheader()
        writer.writerows(df_rows)

def save_html(html: str, progress_dir: Path, page_num: int, prefix: str = "page"):
    html_file = progress_dir / "html" / f"{prefix}_{page_num:04d}.html"
    html_file.parent.mkdir(parents=True, exist_ok=True)
    html_file.write_text(html, encoding="utf-8")
    logging.info(f"Saved raw HTML for debugging: {html_file.name}")

def update_progress(progress_file: Path, state: dict):
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    with progress_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)

def rebuild_seen_appids(progress_dir: Path) -> Set[str]:
    seen = set()
    page_files = glob.glob(str(progress_dir / "pages" / "*.csv"))
    for pf in page_files:
        df = pd.read_csv(pf, usecols=['appid'])
        seen.update(df['appid'].dropna().astype(str).tolist())
    return seen

def validate_snapshot_contract(df: pd.DataFrame) -> dict:
    cols_ok = list(df.columns) == MAIN_SCHEMA_COLUMNS
    rank_ok = (df['rank'].min() == 1) and (df['rank'].max() == len(df)) and (len(df['rank'].unique()) == len(df))
    dups = int(df.duplicated(subset=['appid']).sum())
    one_snap = df['snapshot_id'].nunique() == 1
    one_time = df['captured_at_utc'].nunique() == 1
    has_page = 'page_number' in df.columns
    has_pos = 'position_on_page' in df.columns

    schema_compatible = cols_ok and one_snap and one_time and has_page and has_pos

    return {
        "schema_compatible": schema_compatible,
        "rank_sequence_ok": rank_ok,
        "duplicate_appids": dups,
        "one_snapshot_id": one_snap,
        "one_timestamp": one_time,
        "columns": list(df.columns),
        "rows": len(df)
    }

def finalize_snapshot(progress_dir: Path, base_dir_str: str, work_dir: Path, state: dict, timestamp: str, args: argparse.Namespace, p_config: dict, t0: float, log_file: Path):
    logging.info(f"Merging intermediate pages from work_dir: {progress_dir}")
    page_files = sorted(glob.glob(str(progress_dir / "pages" / "*.csv")))
    
    if not page_files:
        logging.warning("No page files found to merge.")
        return

    dfs = [pd.read_csv(f) for f in page_files]
    final_df = pd.concat(dfs, ignore_index=True)
    
    # Detect Duplicates
    dup_mask = final_df.duplicated(subset=['appid'], keep='first')
    duplicates_df = final_df[dup_mask]
    duplicate_appids_count = int(dup_mask.sum())
    
    # Drop duplicates early to ensure continuous rank
    if duplicate_appids_count > 0:
        logging.warning(f"Found {duplicate_appids_count} duplicate appids across the snapshot.")
        final_df = final_df.drop_duplicates(subset=['appid'], keep='first')

    # Sort strictly by page and position
    final_df = final_df.sort_values(['page_number', 'position_on_page']).reset_index(drop=True)
    
    # Enforce continuous rank based on order of appearance
    final_df['rank'] = range(1, len(final_df) + 1)
    
    # Split back into Main (strict schema) and Extra
    main_df = final_df[MAIN_SCHEMA_COLUMNS]
    extra_cols = ['appid'] + [c for c in final_df.columns if c not in MAIN_SCHEMA_COLUMNS]
    extra_df = final_df[extra_cols]
    
    out_dir = get_final_snapshot_dir(base_dir_str)
    t_drive_start = time.time()
    
    out_csv = out_dir / f"snapshot_main_{timestamp}.csv"
    main_df.to_csv(out_csv, index=False)
    
    if not out_csv.exists():
        raise RuntimeError("Final snapshot CSV was not saved to the required location")
    
    out_extra = out_dir / f"snapshot_extra_{timestamp}.csv"
    extra_df.to_csv(out_extra, index=False)
    
    if duplicate_appids_count > 0:
        out_dups = out_dir / f"duplicates_report_{timestamp}.csv"
        duplicates_df.to_csv(out_dups, index=False)

    val_results = validate_snapshot_contract(main_df)
    
    total_dur = time.time() - t0
    drive_write_dur = time.time() - t_drive_start
    avg_s_page = total_dur / state['pages_successful'] if state['pages_successful'] > 0 else 0
    avg_r_page = len(main_df) / state['pages_successful'] if state['pages_successful'] > 0 else 0
    
    print("\n--------------------------------------------------")
    print(f"FINAL_SNAPSHOT_CSV={out_csv}")
    print(f"ROWS: {val_results['rows']}")
    print(f"COLUMNS: {val_results['columns']}")
    print(f"SCHEMA_COMPATIBLE: {val_results['schema_compatible']}")
    print(f"RANK_SEQUENCE_OK: {val_results['rank_sequence_ok']}")
    print(f"DUPLICATE_APPIDS: {val_results['duplicate_appids']}")
    print("--------------------------------------------------\n")
    
    metadata = {
        "snapshot_id": state['snapshot_id'],
        "started_at": state['started_at'],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "captured_at_utc": state['captured_at_utc'],
        "source": "Steam Top Wishlists Search",
        "total_rows": len(main_df),
        "total_unique_appids": main_df['appid'].nunique(),
        "total_pages_attempted": state['pages_attempted'],
        "total_pages_successful": state['pages_successful'],
        "failed_pages": state['failed_pages'],
        "duplicate_appids_count": val_results['duplicate_appids'],
        "schema_compatible": val_results['schema_compatible'],
        "rank_sequence_ok": val_results['rank_sequence_ok'],
        "stopped_reason": state['stopped_reason'],
        "speed_mode": args.speed_mode,
        "work_dir": str(work_dir),
        "total_duration_seconds": round(total_dur, 2),
        "average_seconds_per_page": round(avg_s_page, 2),
        "average_rows_per_page": round(avg_r_page, 2),
        "drive_write_time_seconds": round(drive_write_dur, 2),
        "pacing_settings": p_config,
        "notes": "Validation complete." if val_results['schema_compatible'] else "Validation flagged schema/contract issues."
    }
    
    meta_json = out_dir / f"snapshot_main_{timestamp}.metadata.json"
    with meta_json.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)
        
    val_report = out_dir / f"validation_report_{timestamp}.json"
    with val_report.open("w", encoding="utf-8") as f:
        json.dump(val_results, f, indent=4)

    # Finally, copy the log file to base_dir
    try:
        final_log_dir = out_dir.parent.parent / "logs"
        final_log_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(log_file, final_log_dir / log_file.name)
    except Exception as e:
        logging.error(f"Failed to copy log file to base_dir: {e}")

def main():
    t_start = time.time()
    args = parse_args()
    
    work_dir = Path(args.work_dir).resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    log_file = setup_logging(work_dir, timestamp)
    p_config = apply_speed_config(args)
    
    # Establish base_dir for logging info
    final_out_dir = get_final_snapshot_dir(args.base_dir)
    base_dir = final_out_dir.parent.parent
    
    logging.info(f"Steam Snapshot Collector Started.")
    logging.info(f"Speed Mode: {args.speed_mode}")
    logging.info(f"Work Dir: {work_dir}")
    logging.info(f"Base Output Dir: {base_dir}")
    
    in_progress_root = work_dir / "data" / "snapshots" / "_in_progress"
    
    state = {}
    seen_appids = set()
    
    if args.resume:
        if not args.run_id:
            logging.error("--run-id required for --resume.")
            sys.exit(1)
        progress_dir = in_progress_root / args.run_id
        progress_file = progress_dir / "progress.json"
        
        if progress_file.exists():
            with progress_file.open("r") as f:
                state = json.load(f)
            logging.info(f"Resuming run {args.run_id} from page {state['last_successful_page'] + 1}")
            seen_appids = rebuild_seen_appids(progress_dir)
            logging.info(f"Rebuilt {len(seen_appids)} seen appids.")
        else:
            logging.error(f"Cannot resume. Progress not found for {args.run_id}")
            sys.exit(1)
    else:
        snapshot_id = args.run_id if args.run_id else str(uuid.uuid4())
        progress_dir = in_progress_root / snapshot_id
        progress_file = progress_dir / "progress.json"
        
        captured_at = datetime.now(timezone.utc).isoformat()
        state = {
            "snapshot_id": snapshot_id,
            "started_at": captured_at,
            "captured_at_utc": captured_at,
            "last_successful_page": 0,
            "pages_attempted": 0,
            "pages_successful": 0,
            "rows_collected": 0,
            "failed_pages": [],
            "stopped_reason": ""
        }
        update_progress(progress_file, state)

    session = build_session()
    current_page = state['last_successful_page'] + 1
    
    batch_rows = []
    adaptive_triggered = False
    
    try:
        while True:
            if args.max_pages > 0 and current_page > args.max_pages:
                state['stopped_reason'] = "max_pages_reached"
                break
            if args.max_games > 0 and state['rows_collected'] >= args.max_games:
                state['stopped_reason'] = "max_games_reached"
                break
                
            apply_pacing(p_config, state['pages_attempted'])
            
            url = f"https://store.steampowered.com/search/?filter=popularwishlist&ignore_preferences=1&page={current_page}"
            logging.info(f"Page {current_page} | Collected: {state['rows_collected']}")
            
            state['pages_attempted'] += 1
            
            html = ""
            try:
                response = fetch_page_with_retry(session, url)
                html = response.text
                is_valid, val_msg = validate_html(html)
                if not is_valid:
                    raise SteamSuspiciousPageException(val_msg)
            except (SteamThrottleException, SteamSuspiciousPageException) as e:
                logging.warning(f"Block detected on page {current_page}: {e}")
                save_html(html, progress_dir, current_page, prefix="blocked")
                
                if not adaptive_triggered:
                    logging.warning("Initiating ADAPTIVE THROTTLING. Sleeping 180s, switching to SAFE mode.")
                    time.sleep(180)
                    p_config = SPEED_PRESETS["safe"].copy()
                    adaptive_triggered = True
                    # Retry this iteration
                    state['pages_attempted'] -= 1 
                    continue
                else:
                    logging.error("Blocked again even after safe mode. Stopping gracefully.")
                    state['stopped_reason'] = f"adaptive_throttle_failed_page_{current_page}"
                    break
            except Exception as e:
                logging.error(f"Failed to fetch page {current_page}: {e}")
                state['failed_pages'].append(current_page)
                state['stopped_reason'] = f"fetch_error_page_{current_page}"
                break
                
            if args.debug_save_all_html:
                save_html(html, progress_dir, current_page, prefix="page")
                
            slots = extract_slot_containers(html)
            
            if not slots:
                logging.info(f"Page {current_page} empty. Assuming end of list.")
                save_html(html, progress_dir, current_page, prefix="empty")
                state['stopped_reason'] = "no_more_results"
                break
                
            page_rows = []
            page_dups = 0
            
            for i, slot in enumerate(slots, 1):
                if args.max_games > 0 and (state['rows_collected'] + len(batch_rows) + len(page_rows)) >= args.max_games:
                    break
                    
                appid = slot.get('data-ds-appid', '')
                if appid and appid in seen_appids:
                    page_dups += 1
                else:
                    if appid: seen_appids.add(appid)
                
                temp_rank = state['rows_collected'] + len(batch_rows) + len(page_rows) + 1
                row = parse_game_slot(slot, temp_rank, current_page, i, state['snapshot_id'], state['captured_at_utc'], url)
                page_rows.append(row)
                
            if page_dups > 0:
                logging.warning(f"Page {current_page} had {page_dups} duplicate appids seen previously.")
                if page_dups == len(slots):
                    logging.warning("Entire page is duplicates. Steam pagination is looping. Stopping.")
                    save_html(html, progress_dir, current_page, prefix="loop")
                    state['stopped_reason'] = "pagination_loop_detected"
                    batch_rows.extend(page_rows)
                    break

            batch_rows.extend(page_rows)
            state['pages_successful'] += 1
            state['last_successful_page'] = current_page
            
            # Write progress every 10 pages to save I/O overhead
            if state['pages_successful'] % 10 == 0:
                save_intermediate_batch(batch_rows, progress_dir, state['pages_successful'] // 10)
                state['rows_collected'] += len(batch_rows)
                batch_rows = []
                update_progress(progress_file, state)
            
            current_page += 1
            
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt. Saving state...")
        state['stopped_reason'] = "user_interrupted"
    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
        state['stopped_reason'] = f"unexpected_error: {e}"
    
    # Flush remaining batch
    if batch_rows:
        save_intermediate_batch(batch_rows, progress_dir, (state['pages_successful'] // 10) + 1)
        state['rows_collected'] += len(batch_rows)
        
    logging.info(f"Crawl stopped. Reason: {state['stopped_reason']}. Finalizing...")
    update_progress(progress_file, state)
    
    finalize_snapshot(progress_dir, args.base_dir, work_dir, state, timestamp, args, p_config, t_start, log_file)
    logging.info(f"Total time elapsed: {time.time() - t_start:.2f}s")

if __name__ == "__main__":
    main()