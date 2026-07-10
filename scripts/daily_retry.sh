#!/bin/bash
# Daily retry of failed extraction rows (run via cron)
LOGFILE=/tmp/retry_extraction.log
cd /home/loong/projects/CostFinder-Crawler
source .venv/bin/activate
set -a
source .env
set +a

# Check if Gemini quota is available
QUOTA=$(python3 -c "import requests,os; r=requests.get(os.environ['LLM_API_URL'].replace('/chat/completions','')+'/models', headers={'Authorization':'Bearer '+os.environ['LLM_API_KEY']}, timeout=10); print(r.status_code)" 2>/dev/null)
if [ "$QUOTA" = "429" ]; then
    echo "[$(date)] Gemini quota exhausted — skipping" >> "$LOGFILE"
    echo "Gemini quota still exhausted, waiting for tomorrow."
    exit 0
fi

echo "[$(date)] Starting retry extraction..." >> "$LOGFILE"
export PYTHONUNBUFFERED=1
python scripts/retry_extraction.py >> "$LOGFILE" 2>&1
echo "[$(date)] Done." >> "$LOGFILE"
