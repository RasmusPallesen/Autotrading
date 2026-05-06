#!/bin/bash
# =============================================================================
# Install systemd services for the trading agent.
# Run after setup.sh and after filling in .env_trading.
# Usage: sudo bash install_services.sh
# =============================================================================

set -e

PROJECT_DIR="/home/trader/autotrading"
ENV_FILE="/home/trader/.env_trading"
VENV="$PROJECT_DIR/.venv/bin/python"

echo "Installing systemd services..."

# ── Research agent service ────────────────────────────────────────────────────
cat > /etc/systemd/system/trading-research.service << SVCEOF
[Unit]
Description=Trading Agent — Research Agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=trader
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV $PROJECT_DIR/research/research_agent.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-research

# Kill gracefully on stop
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

# ── Trading agent service (paper) ─────────────────────────────────────────────
cat > /etc/systemd/system/trading-paper.service << SVCEOF
[Unit]
Description=Trading Agent — Paper Trading
After=network-online.target trading-research.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=trader
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV $PROJECT_DIR/main.py
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-paper

KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

# ── Trading agent service (live) ──────────────────────────────────────────────
cat > /etc/systemd/system/trading-live.service << SVCEOF
[Unit]
Description=Trading Agent — LIVE Trading
After=network-online.target trading-research.service
Wants=network-online.target
# Live agent does NOT auto-restart — requires manual start for safety
StartLimitBurst=1

[Service]
Type=simple
User=trader
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
Environment=ALPACA_PAPER=false
ExecStart=$VENV $PROJECT_DIR/main.py
Restart=no
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-live

KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

# ── Controller service ────────────────────────────────────────────────────────
cat > /etc/systemd/system/trading-controller.service << SVCEOF
[Unit]
Description=Trading Agent — iPhone Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV $PROJECT_DIR/agent_controller.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-controller

[Install]
WantedBy=multi-user.target
SVCEOF

# ── Reload and enable ─────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable trading-research
systemctl enable trading-controller
# Paper and live NOT enabled by default — start manually

echo ""
echo "Services installed:"
echo "  trading-research    — auto-starts on boot, auto-restarts on crash"
echo "  trading-paper       — manual start only"
echo "  trading-live        — manual start only, no auto-restart"
echo "  trading-controller  — auto-starts on boot (iPhone commands)"
echo ""
echo "Useful commands:"
echo "  sudo systemctl start trading-research"
echo "  sudo systemctl start trading-paper"
echo "  sudo systemctl stop trading-paper"
echo "  sudo journalctl -u trading-research -f    # live logs"
echo "  sudo journalctl -u trading-paper -f       # live logs"
echo "  sudo systemctl status trading-research"
