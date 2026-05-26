#!/usr/bin/env python3
"""
run_pipeline.py

Orchestrator for the Steam Diploma data pipeline.
Runs the existing modules sequentially:
1. snapshot_collector.py
2. compare_snapshots.py
3. spike_explainer.py
4. llm_report_generator.py

Configured via a JSON file.
"""

import argparse
import csv
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# --- Helpers ---

def normalize_colab_path(path_str: str) -> str:
    """Normalizes 'drive/MyDrive/...' to '/content/drive/MyDrive/...' for Colab."""
    if not path_str:
        return path_str
    if path_str.startswith("drive/MyDrive/"):
        return "/content/" + path_str
    return path_str

def extract_date_from_filename(filepath: Path) -> datetime:
    """Attempts to find a YYYYMMDD timestamp in the filename."""
    match = re.search(r'_(\d{8})T(\d{6})Z', filepath.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            pass
    # Fallback to modified time
    return datetime.fromtimestamp(filepath.stat().st_mtime)

def get_latest_snapshots(snapshot_dir: Path) -> list[Path]:
    """Finds at least 2 snapshot files, matching ONLY snapshot_main_*.csv."""
    valid_files = list(snapshot_dir.glob("snapshot_main_*.csv"))
    
    # Sort by timestamp in filename or modified time
    files_with_times = [(f, extract_date_from_filename(f)) for f in valid_files]
    files_with_times.sort(key=lambda x: x[1])
    
    return [f[0] for f in files_with_times]

def is_valid_top_movers_csv(filepath: Path) -> bool:
    """Checks if a CSV contains at least 'appid' and 'rank_delta'."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            return 'appid' in headers and 'rank_delta' in headers
    except Exception:
        return False

# --- Main Logic ---

def main():
    parser = argparse.ArgumentParser(description="Steam Pipeline Orchestrator")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config file.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing.")
    parser.add_argument("--skip-collector", action="store_true", help="Skip snapshot_collector.py")
    parser.add_argument("--skip-comparator", action="store_true", help="Skip compare_snapshots.py")
    parser.add_argument("--skip-explainer", action="store_true", help="Skip spike_explainer.py")
    parser.add_argument("--skip-llm", action="store_true", help="Skip llm_report_generator.py")
    args = parser.parse_args()

    # 1. Load Config
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        print(f"Config file not found at {config_path}. Using safe defaults.")
        config = {
            "base_dir": "/content/drive/MyDrive/steam_diploma",
            "src_dir": "/content/drive/MyDrive/steam_diploma/src",
            "snapshot_dir": "/content/drive/MyDrive/steam_diploma/data/snapshots",
            "comparison_dir": "/content/drive/MyDrive/steam_diploma/data/comparisons",
            "spike_explainer_dir": "/content/drive/MyDrive/steam_diploma/data/spike_explainer",
            "log_dir": "/content/drive/MyDrive/steam_diploma/logs",
            "collector": {"enabled": True},
            "comparator": {"enabled": True, "latest_n": 2},
            "spike_explainer": {"enabled": True, "top_n": 1, "evidence_lookback_days": 3},
            "llm_report_generator": {"enabled": True, "provider": "none", "limit": 10, "language": "ru"}
        }

    print(json.dumps(config, indent=2))

    # Normalize paths
    for key in ["base_dir", "src_dir", "snapshot_dir", "comparison_dir", "spike_explainer_dir", "log_dir"]:
        if key in config and config[key]:
            config[key] = normalize_colab_path(config[key])
            
    base_dir = Path(config["base_dir"])
    src_dir = Path(config["src_dir"])
    snapshot_dir = Path(config["snapshot_dir"])
    comparison_dir = Path(config["comparison_dir"])
    spike_dir = Path(config["spike_explainer_dir"])
    log_dir = Path(config["log_dir"])

    # 2. Directory Creation
    for d in [snapshot_dir, comparison_dir, spike_dir, log_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # 3. Logging Setup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"pipeline_run_{timestamp}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logging.info(f"--- STARTING PIPELINE RUN: {timestamp} ---")
    if args.dry_run:
        logging.info("!!! DRY RUN MODE ENABLED - NO COMMANDS WILL BE EXECUTED !!!")

    def run_command(cmd_list: list[str], step_name: str):
        cmd_str = shlex.join(cmd_list)
        logging.info(f"[{step_name}] Executing: {cmd_str}")
        if args.dry_run:
            logging.info(f"[{step_name}] (Dry Run) Skipped execution.")
            return True
            
        try:
            result = subprocess.run(cmd_list, capture_output=True, text=True, check=False)
            if result.stdout:
                logging.info(f"[{step_name}] STDOUT:\n{result.stdout.strip()}")
            if result.stderr:
                logging.warning(f"[{step_name}] STDERR:\n{result.stderr.strip()}")
                
            if result.returncode != 0:
                logging.error(f"[{step_name}] Failed with exit code {result.returncode}.")
                return False
            return True
        except Exception as e:
            logging.error(f"[{step_name}] Exception during execution: {e}")
            return False

    # --- STEP 1: COLLECTOR ---
    collector_cfg = config.get("collector", {})
    if collector_cfg.get("enabled", True) and not args.skip_collector:
        script_path = src_dir / "snapshot_collector.py"
        
        cmd = [
            sys.executable,
            str(script_path),
            "--base-dir", str(base_dir),
            "--work-dir", str(snapshot_dir),
            "--max-games", str(collector_cfg.get("max_games", 3000)),
            "--speed-mode", str(collector_cfg.get("speed_mode", "fast")),
        ]
          
            
        if not run_command(cmd, "COLLECTOR"):
            logging.error("Pipeline stopped due to Collector failure.")
            sys.exit(1)
    else:
        logging.info("[COLLECTOR] Skipped.")

    # --- SNAPSHOT VALIDATION ---
    snapshots = get_latest_snapshots(snapshot_dir)
    logging.info("Snapshot candidates found:")
    if snapshots:
        for s in snapshots:
            logging.info(f" - {s.name}")
    else:
        logging.info(" - None")

    if len(snapshots) < 2:
        logging.error(f"Need at least 2 snapshot_main_*.csv files in {snapshot_dir} to compare. Found {len(snapshots)}.")
        if not args.dry_run:
            sys.exit(1)
    
    if len(snapshots) >= 2:
        old_snap, new_snap = snapshots[-2], snapshots[-1]
        logging.info(f"Selected Old Snapshot: {old_snap.name}")
        logging.info(f"Selected New Snapshot: {new_snap.name}")
        
        new_date_dt = extract_date_from_filename(new_snap)
        old_date_dt = extract_date_from_filename(old_snap)
    else:
        # Fallback for dry-run
        old_date_dt = datetime.now() - timedelta(days=1)
        new_date_dt = datetime.now()

    # --- STEP 2: COMPARATOR ---
    comparator_cfg = config.get("comparator", {})
    
    comparison_run_dir = None
    
    if comparator_cfg.get("enabled", True) and not args.skip_comparator:
        script_path = src_dir / "compare_snapshots.py"
        if not script_path.exists():
            script_path = src_dir / "compare_snapshots_2.py"
    
        comparison_run_dir = comparison_dir / f"comparison_{timestamp}"
        comparison_run_dir.mkdir(parents=True, exist_ok=True)
    
        cmd = [
            sys.executable, str(script_path),
            "--snapshot-dir", str(snapshot_dir),
            "--latest-n", str(comparator_cfg.get("latest_n", 2)),
            "--outdir", str(comparison_run_dir)
        ]
    
        if comparator_cfg.get("top_limit"):
            cmd.extend(["--top-limit", str(comparator_cfg.get("top_limit"))])
    
        if not run_command(cmd, "COMPARATOR"):
            logging.error("Pipeline stopped due to Comparator failure.")
            sys.exit(1)
    else:
        logging.info("[COMPARATOR] Skipped.")
    
    
    # --- COMPARATOR OUTPUT VALIDATION ---
    top_movers_csv = None
    
    if comparison_run_dir and comparison_run_dir.exists():
        latest_comp_dir = comparison_run_dir
    else:
        comp_subfolders = [f for f in comparison_dir.iterdir() if f.is_dir()]
        if comp_subfolders:
            comp_subfolders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            latest_comp_dir = comp_subfolders[0]
        else:
            latest_comp_dir = None
    
    if latest_comp_dir:
        logging.info(f"Selected Comparison Folder: {latest_comp_dir.name}")
    
        for candidate in ["spikes_up.csv", "top_movers.csv", "upward_movers.csv", "rank_changes.csv"]:
            fpath = latest_comp_dir / candidate
            if fpath.exists() and is_valid_top_movers_csv(fpath):
                top_movers_csv = fpath
                break
    
        if not top_movers_csv:
            for fpath in latest_comp_dir.glob("*.csv"):
                if is_valid_top_movers_csv(fpath):
                    top_movers_csv = fpath
                    break
    
    if not top_movers_csv:
        logging.error("Could not find a valid top movers CSV in the comparison output.")
        if not args.dry_run:
            sys.exit(1)
    else:
        logging.info(f"Selected Top Movers CSV: {top_movers_csv.name}")
        
    # --- STEP 3: SPIKE EXPLAINER ---
    explainer_cfg = config.get("spike_explainer", {})
    
    # Record folders before running
    before_folders = set(f.name for f in spike_dir.iterdir() if f.is_dir()) if spike_dir.exists() else set()
    spike_step_started_at = datetime.now().timestamp()
    
    if explainer_cfg.get("enabled", True) and not args.skip_explainer:
        script_path = src_dir / "spike_explainer.py"
        
        lookback = explainer_cfg.get("evidence_lookback_days", 3)
        window_start = old_date_dt - timedelta(days=lookback)
        
        cmd = [
            sys.executable, str(script_path),
            "--diff-csv", "latest",
            "--comparisons-dir", str(comparison_dir),
            "--output-dir", str(spike_dir),
            "--top-n", str(explainer_cfg.get("top_n", 1)),
            "--old-snapshot-date", window_start.strftime("%Y-%m-%d"),
            "--new-snapshot-date", new_date_dt.strftime("%Y-%m-%d")
        ]
        
        provider = explainer_cfg.get("search_provider", "none")
        cmd.extend(["--search-provider", provider])
        
        # Pull API keys from environment if not explicit in config to avoid hardcoding
        tav_key = os.environ.get("TAVILY_API_KEY")
        if tav_key and provider == "tavily":
            cmd.extend(["--tavily-api-key", tav_key])
            
        yt_key = os.environ.get("YOUTUBE_API_KEY")
        if yt_key:
            cmd.extend(["--youtube-api-key", yt_key])
            
        if not run_command(cmd, "SPIKE_EXPLAINER"):
            logging.error("Pipeline stopped due to Spike Explainer failure.")
            sys.exit(1)
    else:
        logging.info("[SPIKE_EXPLAINER] Skipped.")

    # --- STEP 4: LLM REPORT GENERATOR ---
    llm_cfg = config.get("llm_report_generator", {})
    if llm_cfg.get("enabled", True) and not args.skip_llm:
        script_path = src_dir / "llm_report_generator.py"
        
        explicit_folder = llm_cfg.get("input_folder")
        explicit_file = llm_cfg.get("input_file")
        
        targets = []
        
        if explicit_file:
            targets.append(["--input-file", normalize_colab_path(explicit_file)])
        elif explicit_folder:
            targets.append(["--input-folder", normalize_colab_path(explicit_folder)])
        else:
            # Detect newly created folders
            after_folders = set(f.name for f in spike_dir.iterdir() if f.is_dir()) if spike_dir.exists() else set()
            new_folder_names = after_folders - before_folders
            
            new_targets = []
            for fname in new_folder_names:
                fpath = spike_dir / fname
                if (fpath / "llm_summary_input.md").exists():
                    new_targets.append(fpath)
                    
            if new_targets:
                # Sort newest first
                new_targets.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                for tgt in new_targets:
                    targets.append(["--input-folder", str(tgt)])
            else:
                logging.warning(
                    "No newly created spike folders detected; selecting latest modified spike folder instead."
                )
            
                candidate_folders = []
                for fpath in spike_dir.iterdir():
                    if fpath.is_dir() and (fpath / "llm_summary_input.md").exists():
                        candidate_folders.append(fpath)
            
                if candidate_folders:
                    candidate_folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                    latest_spike_folder = candidate_folders[0]
                    logging.info(f"Selected latest spike folder: {latest_spike_folder}")
                    targets.append(["--input-folder", str(latest_spike_folder)])
                else:
                    logging.error("No spike folders with llm_summary_input.md found.")
                    if not args.dry_run:
                        sys.exit(1)
        
        for target_args in targets:
            cmd = [
                sys.executable, str(script_path),
                "--provider", llm_cfg.get("provider", "none"),
                "--language", llm_cfg.get("language", "ru")
            ]
            cmd.extend(target_args)
            
            if "limit" in llm_cfg and "--input-dir" in target_args:
                cmd.extend(["--limit", str(llm_cfg["limit"])])
            if "model" in llm_cfg and llm_cfg["model"]:
                cmd.extend(["--model", str(llm_cfg["model"])])
            if llm_cfg.get("overwrite", False):
                cmd.append("--overwrite")
                
            run_command(cmd, "LLM_GENERATOR")
    else:
        logging.info("[LLM_GENERATOR] Skipped.")

    # --- FINAL SUMMARY ---
    logging.info("==================================================")
    logging.info("PIPELINE RUN COMPLETE")
    logging.info("==================================================")
    logging.info(f"Log File Saved: {log_file}")
    if len(snapshots) >= 2:
        logging.info(f"Latest Snapshot: {snapshots[-1]}")
    if top_movers_csv:
        logging.info(f"Top Movers CSV: {top_movers_csv}")
    logging.info(f"Spike Explainer Dir: {spike_dir}")
    logging.info("==================================================")

if __name__ == "__main__":
    main()