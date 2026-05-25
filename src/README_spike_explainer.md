```
# README_spike_explainer.md

## Spike Explainer

The **Spike Explainer** takes a `diff_detector.py` CSV report, finds the top
moving games (spikes), and automatically gathers contextual evidence to figure
out *why* they spiked.

It queries the official Steam News API and formats structured folders containing
manual search queries, evidence templates, and ready-to-use LLM prompts.
Optionally, it can connect to YouTube and Web Search APIs (like Tavily) to
enrich the evidence automatically.

### Output Structure

For every game analyzed, it creates a folder like:
`/data/spike_explainer/2026-03-29__appid_123__game_slug__rank_50_to_10__up_40/`

Inside, you will find:
- `llm_summary_input.md`: The most important file. Copy-paste this into
ChatGPT/Claude to get an instant analysis.
- `evidence_scored.csv`: All data from Steam, Web, and YouTube graded by
relevance.
- `search_queries.md`: Useful links and copy-paste keywords if you want to
manually search TikTok, Reddit, etc.
- `manual_evidence_template.csv`: A spreadsheet to paste links you find
manually.
- `human_summary.md`: A quick text overview of the event.

### Usage

**Basic Run (No API Keys required):**
```bash
python spike_explainer.py --diff-csv data/comparisons/diff_report.csv

```

