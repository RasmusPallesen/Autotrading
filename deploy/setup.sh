#!/bin/bash
# =============================================================================
# Trading Agent — Cloud VM Setup Script
# Run once on a fresh Ubuntu 22.04/24.04 VPS (Hetzner, DigitalOcean, etc.)
# Usage: bash setup.sh
# =============================================================================

set -e  # Exit on any error

echo "============================================"
echo "  Trading Agent — VM Setup"
echo "============================================"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -q
apt-get install -y -q \
    python3.12 \
    python3.12-venv \
    python3-pip \
    git \
    curl \
    wget \
    htop \
    nano \
    ufw \
    fail2ban \
    libpq-dev \
    python3-dev \
    gcc

echo "      Python version: $(python3.12 --version)"

# ── 2. Create dedicated user ──────────────────────────────────────────────────
echo "[2/7] Creating 'trader' user..."
if ! id "trader" &>/dev/null; then
    useradd -m -s /bin/bash trader
    echo "      User 'trader' created"
else
    echo "      User 'trader' already exists"
fi

# ── 3. Clone/update repo ──────────────────────────────────────────────────────
echo "[3/7] Setting up project..."
PROJECT_DIR="/home/trader/autotrading"

if [ -d "$PROJECT_DIR" ]; then
    echo "      Updating existing repo..."
    cd "$PROJECT_DIR"
    sudo -u trader git pull
else
    echo "      Cloning repo..."
    sudo -u trader git clone https://github.com/RasmusPallesen/Autotrading "$PROJECT_DIR"
fi

# ── 4. Python virtual environment ─────────────────────────────────────────────
echo "[4/7] Setting up Python environment..."
cd "$PROJECT_DIR"
sudo -u trader python3.12 -m venv .venv
sudo -u trader .venv/bin/pip install --upgrade pip -q
sudo -u trader .venv/bin/pip install -r requirements-agent.txt -q
echo "      Dependencies installed"

# ── 5. Create logs directory ──────────────────────────────────────────────────
echo "[5/7] Creating directories..."
sudo -u trader mkdir -p "$PROJECT_DIR/logs"
chown -R trader:trader "$PROJECT_DIR"

# ── 6. Environment file ───────────────────────────────────────────────────────
echo "[6/7] Setting up environment..."
ENV_FILE="/home/trader/.env_trading"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# Trading Agent Environment Variables
# Fill in your actual values then run: source ~/.env_trading

export ALPACA_API_KEY=""
export ALPACA_SECRET_KEY=""
export ALPACA_PAPER="true"

export ANTHROPIC_API_KEY=""

export DATABASE_URL=""

export NTFY_TOPIC=""
export NTFY_CMD_TOPIC=""

export MASSIVE_API_KEY=""
export REDDIT_CLIENT_ID=""
export REDDIT_CLIENT_SECRET=""
export RESEND_API_KEY=""

# Agent tuning
export RESEARCH_INTERVAL_SECONDS="900"
export CONVICTION_THRESHOLD="0.70"
export BREAKOUT_MIN_SCORE="1"
export PRE_CATALYST_DAYS="7"
export ANALYSIS_CACHE_TTL_HOURS="4"
export STRONG_BEAT_THRESHOLD_PCT="10"
ENVEOF
    chown trader:trader "$ENV_FILE"
    chmod 600 "$ENV_FILE"  # Private — readable only by trader user
    echo "      Created $ENV_FILE — fill in your secrets before starting agents"
else
    echo "      $ENV_FILE already exists — skipping"
fi

# Add env file sourcing to trader's .bashrc
if ! grep -q "env_trading" /home/trader/.bashrc; then
    echo "source ~/.env_trading" >> /home/trader/.bashrc
fi

# ── 7. Firewall ───────────────────────────────────────────────────────────────
echo "[7/7] Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable
echo "      Firewall enabled (SSH only)"

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit /home/trader/.env_trading with your secrets"
echo "  2. Run: bash install_services.sh"
echo "  3. Start agents: sudo systemctl start trading-research"
echo "============================================"
