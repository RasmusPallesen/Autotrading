#!/bin/bash
# =============================================================================
# Deploy latest code from GitHub to the VM.
# Run whenever you push updates to GitHub.
# Usage: bash update.sh
# Optionally restart services: bash update.sh --restart
# =============================================================================

set -e

PROJECT_DIR="/home/trader/autotrading"
RESTART=${1:-""}

echo "Pulling latest code from GitHub..."
cd "$PROJECT_DIR"
sudo -u trader git pull

echo "Updating dependencies..."
sudo -u trader .venv/bin/pip install -r requirements-agent.txt -q

if [ "$RESTART" = "--restart" ]; then
    echo "Restarting services..."
    systemctl restart trading-research || true
    systemctl restart trading-controller || true
    # Paper/live: only restart if currently running
    systemctl is-active --quiet trading-paper && systemctl restart trading-paper || true
    echo "Services restarted"
fi

echo ""
echo "Update complete. $(git log -1 --format='%h %s')"
echo "Tip: run 'bash update.sh --restart' to restart services automatically"
