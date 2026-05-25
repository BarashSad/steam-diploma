#!/usr/bin/env python3
"""
compare_snapshots.py

A robust utility for comparing 2 or more Steam Top Wishlisted ranking snapshots.
It matches games securely by appid to detect rank movements, tracks changes in 
pricing and release text, and generates a visual chart of game trajectories.
"""

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


def setup_logging(debug: bool):
    """Configures console logging."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )


def extract_timestamp_from_file(filepath: Path) -> datetime:
    """Attempts to find a captured_at_utc inside the CSV, falling back to filename."""
    try:
        # Use sep=None and engine='python' to gracefully handle both ',' and ';'
        df_peek = pd.read_csv(filepath, nrows=1, sep=None, engine='python')
        if 'captured_at_utc' in df_peek.columns and pd.notna(df_peek['captured_at_utc'].iloc[0]):
            return pd.to_datetime(df_peek['captured_at_utc'].iloc[0])
    except Exception as e:
        logging.debug(f"Could not read timestamp from contents of {filepath}: {e}")

    # Fallback to finding a timestamp in the filename (e.g., 20231024T123000Z)
    match = re.search(r'_(\d{8}T\d{6}Z)', filepath.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        except ValueError:
            pass
            
    # Absolute fallback: file modification time
    return datetime.fromtimestamp(filepath.stat().st_mtime)


def select_snapshots(snapshot_dir, latest_n=2):
    """
    Select latest snapshot CSV files safely.

    Fixes:
    - normalizes timestamps to UTC-aware pandas timestamps
    - avoids naive vs aware datetime comparison errors
    - skips invalid snapshot files
    """

    from pathlib import Path
    import pandas as pd

    snapshot_dir = Path(snapshot_dir)

# Only use main snapshot files.
# Ignore snapshot_extra_*.csv and any other CSV outputs.
    csv_files = sorted(snapshot_dir.glob("snapshot_main_*.csv"))

    if not csv_files:
      raise FileNotFoundError(
        f"No main snapshot files found in {snapshot_dir}."
        "Expected files named like snapshots_main_*.csv"
      )

    files_with_times = []

    for path in csv_files:
        try:
            # Read only first few rows for speed
            df_head = pd.read_csv(path, nrows=5)

            if "captured_at_utc" in df_head.columns:
                raw_ts = df_head["captured_at_utc"].dropna()

                if len(raw_ts) > 0:
                    ts = pd.to_datetime(
                        raw_ts.iloc[0],
                        utc=True,
                        errors="coerce"
                    )

                    if not pd.isna(ts):
                        files_with_times.append((path, ts))
                        continue

            # fallback: file modification time
            fallback_ts = pd.Timestamp.fromtimestamp(
                path.stat().st_mtime,
                tz="UTC"
            )

            files_with_times.append((path, fallback_ts))

        except Exception as e:
            print(f"[WARN] Could not parse snapshot file: {path}")
            print(f"       {e}")

    if len(files_with_times) < latest_n:
        raise ValueError(
            f"Found only {len(files_with_times)} valid snapshots, "
            f"but latest_n={latest_n}"
        )

    # IMPORTANT:
    # sort by numeric timestamp value
    # avoids naive/aware comparison issues
    files_with_times.sort(key=lambda x: x[1].value)

    selected = files_with_times[-latest_n:]

    print("\n[INFO] Selected snapshots:")
    for path, ts in selected:
        print(f"  {ts.isoformat()} -> {path.name}")

    return [path for path, ts in selected]


def make_auto_outdir(snapshot_dir: str | None = None, files: list[str] | None = None) -> Path:
    """
    Automatically creates a timestamped comparison output directory.

    If snapshot_dir is provided:
        data/snapshots -> data/comparisons/comparison_YYYYMMDD_HHMMSS

    If only files are provided:
        uses parent folders to infer a nearby comparisons directory.

    Fallback:
        ./data/comparisons/comparison_YYYYMMDD_HHMMSS
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if snapshot_dir:
        snapshot_path = Path(snapshot_dir)

        # Expected structure:
        # .../data/snapshots
        # .../data/comparisons
        if snapshot_path.name == "snapshots":
            comparisons_base = snapshot_path.parent / "comparisons"
        else:
            comparisons_base = snapshot_path / "comparisons"

    elif files:
        first_file = Path(files[0])

        # If file is in .../data/snapshots/file.csv
        if first_file.parent.name == "snapshots":
            comparisons_base = first_file.parent.parent / "comparisons"
        else:
            comparisons_base = first_file.parent / "comparisons"

    else:
        comparisons_base = Path("data") / "comparisons"

    outdir = comparisons_base / f"comparison_{timestamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    return outdir


def load_snapshot(filepath: Path, top_limit: int) -> pd.DataFrame:
    """Loads, cleans, and standardizes a snapshot CSV."""
    df = pd.read_csv(filepath, sep=None, engine='python')
    
    # Map of expected columns
    expected_cols = [
        'snapshot_id', 'captured_at_utc', 'rank', 'appid', 'title', 
        'release_text', 'price_text', 'game_url', 'page_number', 'position_on_page'
    ]
    for col in expected_cols:
        if col not in df.columns:
            df[col] = ''
            
    # Normalize appid
    df['appid'] = df['appid'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
    df['appid'] = df['appid'].replace('nan', '')
    
    # Normalize rank
    df['rank'] = pd.to_numeric(df['rank'], errors='coerce')
    
    # Clean text fields
    for text_col in ['title', 'release_text', 'price_text']:
        df[text_col] = df[text_col].fillna('').astype(str).str.strip()
        
    if top_limit and top_limit > 0:
        df = df[df['rank'] <= top_limit].copy()
        
    # Ensure captured_at_utc is actual datetime for plotting
    df['captured_at_utc_dt'] = pd.to_datetime(df['captured_at_utc'], errors='coerce')
        
    return df


def validate_snapshot(df: pd.DataFrame, label: str):
    """Logs the integrity of the loaded snapshot."""
    total_rows = len(df)
    missing_appids = len(df[df['appid'] == ''])
    
    logging.info(f"--- Validation: {label} ---")
    logging.info(f"Total rows: {total_rows}")
    if missing_appids > 0:
        logging.warning(f"Missing appids: {missing_appids}")


def process_comparison(dfs: list[pd.DataFrame], spike_threshold: int) -> dict:
    """Core engine comparing the First and Last dataframes for deltas."""
    df_old = dfs[0]
    df_new = dfs[-1]
    
    # 1. Filter Valid Rows
    old_valid = df_old[df_old['appid'] != ''].copy()
    new_valid = df_new[df_new['appid'] != ''].copy()
    
    # Extract Metadata
    old_captured_utc = old_valid['captured_at_utc'].iloc[0] if not old_valid.empty else ''
    new_captured_utc = new_valid['captured_at_utc'].iloc[0] if not new_valid.empty else ''
    
    window_hours = 0.0
    if old_captured_utc and new_captured_utc:
        try:
            t1 = pd.to_datetime(old_captured_utc)
            t2 = pd.to_datetime(new_captured_utc)
            window_hours = round((t2 - t1).total_seconds() / 3600.0, 2)
        except Exception:
            pass

    # 2. Merge Valid Rows by Appid
    old_clean = old_valid.drop_duplicates(subset=['appid'])
    new_clean = new_valid.drop_duplicates(subset=['appid'])
    
    df_common = pd.merge(
        old_clean, new_clean, on='appid', suffixes=('_old', '_new')
    )
    
    # 3. Compute Deltas, Movements, and Text Changes
    if not df_common.empty:
        df_common['rank_delta'] = df_common['rank_old'] - df_common['rank_new']
        df_common['abs_rank_delta'] = df_common['rank_delta'].abs()
        
        df_common['movement_type'] = np.where(
            df_common['rank_delta'] > 0, 'up',
            np.where(df_common['rank_delta'] < 0, 'down', 'stable')
        )
        
        df_common['spike_type'] = np.where(
            df_common['rank_delta'] >= spike_threshold, 'upward_spike',
            np.where(df_common['rank_delta'] <= -spike_threshold, 'downward_spike', 'none')
        )
        
        df_common['price_changed'] = df_common['price_text_old'] != df_common['price_text_new']
        df_common['release_changed'] = df_common['release_text_old'] != df_common['release_text_new']
        
    else:
        # Create missing columns safely if DataFrame is entirely empty
        for col in ['rank_delta', 'abs_rank_delta']:
            df_common[col] = pd.Series(dtype='float64')
        for col in ['movement_type', 'spike_type', 'price_changed', 'release_changed']:
            df_common[col] = pd.Series(dtype='object')
            
        # Add suffix columns manually if merge resulted in zero rows
        if 'title_old' not in df_common.columns:
            for col in ['title_old', 'title_new', 'rank_old', 'rank_new', 'price_text_old', 'price_text_new', 'release_text_old', 'release_text_new']:
                df_common[col] = pd.Series(dtype='object')

    # Format Rank Changes table
    cols_rank_changes = [
        'appid', 'title_old', 'title_new', 'rank_old', 'rank_new', 'rank_delta', 'abs_rank_delta', 'movement_type', 'spike_type',
        'price_text_old', 'price_text_new', 'price_changed',
        'release_text_old', 'release_text_new', 'release_changed'
    ]
    
    # Ensure all columns exist before sorting
    for c in cols_rank_changes:
        if c not in df_common.columns:
            df_common[c] = None

    df_rank_changes = df_common[cols_rank_changes].sort_values(
        by=['abs_rank_delta', 'rank_new'], ascending=[False, True]
    )
    
    # 4. Filter Spikes and Text Changes
    df_spikes_up = df_rank_changes[df_rank_changes['spike_type'] == 'upward_spike'].sort_values(by='rank_delta', ascending=False)
    df_spikes_down = df_rank_changes[df_rank_changes['spike_type'] == 'downward_spike'].sort_values(by='rank_delta', ascending=True)
    df_text_changes = df_rank_changes[(df_rank_changes['price_changed'] == True) | (df_rank_changes['release_changed'] == True)].copy()

    # 5. Missing / New Entries
    df_new_entries = new_clean[~new_clean['appid'].isin(old_clean['appid'])].copy()
    if not df_new_entries.empty:
        df_new_entries = df_new_entries.rename(columns={'title': 'title_new', 'rank': 'rank_new', 'price_text': 'price_text_new', 'release_text': 'release_text_new'})
        df_new_entries = df_new_entries[['appid', 'title_new', 'rank_new', 'price_text_new', 'release_text_new']].sort_values(by='rank_new')

    df_dropped_entries = old_clean[~old_clean['appid'].isin(new_clean['appid'])].copy()
    if not df_dropped_entries.empty:
        df_dropped_entries = df_dropped_entries.rename(columns={'title': 'title_old', 'rank': 'rank_old'})
        df_dropped_entries = df_dropped_entries[['appid', 'title_old', 'rank_old']].sort_values(by='rank_old')

    # 6. Aggregate Summary
    biggest_up = df_spikes_up.iloc[0] if not df_spikes_up.empty else None
    biggest_down = df_spikes_down.iloc[0] if not df_spikes_down.empty else None
    
    summary = {
        'files_compared_count': len(dfs),
        'comparison_window_start_utc': old_captured_utc,
        'comparison_window_end_utc': new_captured_utc,
        'comparison_window_hours': window_hours,
        'old_unique_appids': len(old_clean),
        'new_unique_appids': len(new_clean),
        'common_app_count': len(df_common),
        'new_entry_count': len(df_new_entries) if not df_new_entries.empty else 0,
        'dropped_entry_count': len(df_dropped_entries) if not df_dropped_entries.empty else 0,
        'moved_up_count': len(df_common[df_common['movement_type'] == 'up']),
        'moved_down_count': len(df_common[df_common['movement_type'] == 'down']),
        'stable_count': len(df_common[df_common['movement_type'] == 'stable']),
        'upward_spike_count': len(df_spikes_up),
        'downward_spike_count': len(df_spikes_down),
        'price_changes_count': len(df_common[df_common['price_changed'] == True]),
        'release_changes_count': len(df_common[df_common['release_changed'] == True]),
        'biggest_upward_spike_appid': biggest_up['appid'] if biggest_up is not None else None,
        'biggest_upward_spike_title': biggest_up['title_new'] if biggest_up is not None else None,
        'biggest_upward_spike_delta': int(biggest_up['rank_delta']) if biggest_up is not None else 0,
        'biggest_downward_spike_appid': biggest_down['appid'] if biggest_down is not None else None,
        'biggest_downward_spike_title': biggest_down['title_new'] if biggest_down is not None else None,
        'biggest_downward_spike_delta': int(biggest_down['rank_delta']) if biggest_down is not None else 0
    }

    return {
        'summary': summary,
        'df_rank_changes': df_rank_changes,
        'df_spikes_up': df_spikes_up,
        'df_spikes_down': df_spikes_down,
        'df_new_entries': df_new_entries,
        'df_dropped_entries': df_dropped_entries,
        'df_text_changes': df_text_changes,
        'df_all_combined': pd.concat(dfs, ignore_index=True)
    }


def plot_movements(df_all: pd.DataFrame, target_appids: list, outdir: Path, filename="trajectory_chart.png"):
    """Generates a line chart tracking the rank of specified games across all provided snapshots."""
    if not MATPLOTLIB_AVAILABLE:
        logging.warning("matplotlib is not installed. Skipping diagram generation.")
        return
        
    if df_all.empty or not target_appids:
        logging.warning("No data or appids provided to plot.")
        return

    df_plot = df_all[df_all['appid'].isin(target_appids)].copy()
    if df_plot.empty:
        logging.warning("Target appids for plotting not found in the dataset.")
        return
        
    df_plot = df_plot.dropna(subset=['captured_at_utc_dt', 'rank'])
    fig, ax = plt.subplots(figsize=(10, 6))
    
    for appid in target_appids:
        game_data = df_plot[df_plot['appid'] == appid].sort_values('captured_at_utc_dt')
        if game_data.empty:
            continue
        title = game_data['title'].iloc[-1]
        ax.plot(game_data['captured_at_utc_dt'], game_data['rank'], marker='o', linewidth=2, label=f"{title} ({appid})")

    ax.invert_yaxis()
    ax.set_ylabel('Steam Wishlist Rank')
    ax.set_xlabel('Capture Time')
    ax.set_title('Game Rank Trajectory Across Snapshots')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))
    plt.xticks(rotation=45)
    ax.grid(True, linestyle='--', alpha=0.7)
    ax.legend(loc='upper right', bbox_to_anchor=(1.05, 1))
    
    plt.tight_layout()
    plot_path = outdir / filename
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(plot_path, dpi=150)
    logging.info(f"Generated rank movement diagram at: {plot_path}")


def write_outputs(outdir: Path, data: dict):
    """Writes the generated dataframes and summary to files."""
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(data['summary'], f, indent=4)
        
    pd.DataFrame([data['summary']]).to_csv(outdir / "summary.csv", index=False)
    data['df_rank_changes'].to_csv(outdir / "rank_changes.csv", index=False)
    data['df_spikes_up'].to_csv(outdir / "spikes_up.csv", index=False)
    data['df_spikes_down'].to_csv(outdir / "spikes_down.csv", index=False)
    data['df_new_entries'].to_csv(outdir / "new_entries.csv", index=False)
    data['df_dropped_entries'].to_csv(outdir / "dropped_entries.csv", index=False)
    
    if not data['df_text_changes'].empty:
        data['df_text_changes'].to_csv(outdir / "text_changes.csv", index=False)


def print_console_report(data: dict):
    """Outputs a human-readable console report."""
    s = data['summary']
    print("\n" + "="*60)
    print(" STEAM RANKING LONGITUDINAL COMPARISON REPORT")
    print("="*60)
    print(f"Snapshots Compared : {s['files_compared_count']}")
    print(f"Start Window       : {s['comparison_window_start_utc']}")
    print(f"End Window         : {s['comparison_window_end_utc']}")
    print("-" * 60)
    print(f"Games Present in Both : {s['common_app_count']}")
    print(f"New Entries Found     : {s['new_entry_count']}")
    print(f"Dropped Entries       : {s['dropped_entry_count']}")
    print("-" * 60)
    print(f"Moved Up   : {s['moved_up_count']}")
    print(f"Moved Down : {s['moved_down_count']}")
    print(f"Stable     : {s['stable_count']}")
    print("-" * 60)
    print(f"Upward Spikes   : {s['upward_spike_count']}")
    print(f"Downward Spikes : {s['downward_spike_count']}")
    print(f"Price Changes   : {s['price_changes_count']}")
    print(f"Release Changes : {s['release_changes_count']}")
    print("-" * 60)
    
    if s['biggest_upward_spike_appid']:
        print(f"Biggest Up : +{s['biggest_upward_spike_delta']} ranks | {s['biggest_upward_spike_title']} ({s['biggest_upward_spike_appid']})")
        
    df_text = data['df_text_changes']
    if not df_text.empty:
        print("\nNOTABLE PRICE/RELEASE CHANGES (First 5):")
        for _, row in df_text.head(5).iterrows():
            print(f"[{row['appid']}] {row['title_new'][:30]}")
            if row['price_changed']:
                print(f"   Price: '{row['price_text_old']}' -> '{row['price_text_new']}'")
            if row['release_changed']:
                print(f"   Release: '{row['release_text_old']}' -> '{row['release_text_new']}'")
                
    print("\nOutputs and charts saved successfully.")


def main():
    parser = argparse.ArgumentParser(description="Compare Steam Top Wishlisted snapshots and map trajectories.")
    parser.add_argument('--files', nargs='+', help='List of specific CSV files to compare.')
    parser.add_argument('--snapshot-dir', type=str, help='Directory containing snapshots.')
    parser.add_argument('--latest-n', type=int, default=2, help='Compare the latest N snapshots (default 2).')
    parser.add_argument(
    '--outdir',
    type=str, 
    default=None,
    help='Output directory. If not provided, a timestamped folder will be created automatically.'
    )
    parser.add_argument('--spike-threshold', type=int, default=20, help='Rank change threshold for spikes.')
    parser.add_argument('--top-limit', type=int, default=None, help='Limit comparison to top N ranks.')
    parser.add_argument('--plot-top-spikes', type=int, default=5, help='Number of top spiking games to chart.')
    parser.add_argument('--plot-appid', type=str, help='Generate a chart for this specific appid.')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging.')
    
    args = parser.parse_args()
    setup_logging(args.debug)
    
    if args.outdir: 
      # Normalize outdir safely
      if isinstance(args.outdir, (list, tuple)):
        outdir = Path(args.outdir[0])
      else:
        outdir = Path(args.outdir)
      outdir.mkdir(parents=True, exist_ok=True)
    else:
      outdir = make_auto_outdir( 
        snapshot_dir=args.snapshot_dir, 
        files=args.files 
        )

    logging.info(f"Comparison output directory: {outdir}")
    
    if args.files:
        selected_paths = [Path(f) for f in args.files]
    elif args.snapshot_dir:
        selected_paths = select_snapshots(Path(args.snapshot_dir), args.latest_n)
    else:
        logging.error("You must provide either --files OR --snapshot-dir.")
        sys.exit(1)
        
    logging.info(f"Loading {len(selected_paths)} DataFrames...")
    dfs = []
    for i, path in enumerate(selected_paths):
        df = load_snapshot(path, args.top_limit)
        validate_snapshot(df, f"File {i+1} ({path.name})")
        dfs.append(df)
        
    logging.info("Analyzing snapshots across time...")
    result_data = process_comparison(dfs, spike_threshold=args.spike_threshold)
    
    logging.info("Generating charts...")
    appids_to_plot = []
    if args.plot_appid:
        appids_to_plot.append(args.plot_appid)
    else:
        top_spikes = result_data['df_spikes_up'].head(args.plot_top_spikes)
        appids_to_plot.extend(top_spikes['appid'].tolist())
        
    plot_movements(result_data['df_all_combined'], appids_to_plot, outdir)
    
    logging.info(f"Writing outputs to {outdir}...")
    write_outputs(outdir, result_data)
    print_console_report(result_data)

if __name__ == "__main__":
    main()