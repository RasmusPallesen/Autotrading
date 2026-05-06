# Cloud VM Deployment Guide

## Recommended: Hetzner Cloud (cheapest for EU)
- Go to hetzner.com/cloud
- Create account, add payment method
- Create server: Ubuntu 24.04, CX11 (2 vCPU, 2GB RAM) = €3.29/month
- Location: Nuremberg or Helsinki (closest to Copenhagen, good US latency)
- Add your SSH public key during creation
- Note your server IP address

## First-time setup

### 1. Connect to your VM
```bash
ssh root@YOUR_SERVER_IP
```

### 2. Run setup script
```bash
curl -o setup.sh https://raw.githubusercontent.com/RasmusPallesen/Autotrading/main/deploy/setup.sh
bash setup.sh
```

### 3. Fill in your secrets
```bash
nano /home/trader/.env_trading
```
Fill in all the values — Alpaca keys, Anthropic key, DATABASE_URL, NTFY topics etc.

### 4. Install systemd services
```bash
bash /home/trader/autotrading/deploy/install_services.sh
```

### 5. Use Linux controller (replaces agent_controller.py on VM)
```bash
cp /home/trader/autotrading/deploy/agent_controller_linux.py \
   /home/trader/autotrading/agent_controller.py
```

### 6. Start agents
```bash
sudo systemctl start trading-research
sudo systemctl start trading-controller
# Paper trading (when ready):
sudo systemctl start trading-paper
```

### 7. Check they're running
```bash
sudo systemctl status trading-research
sudo journalctl -u trading-research -f    # live log stream
```

---

## Deploying code updates

Every time you push to GitHub:
```bash
ssh root@YOUR_SERVER_IP
bash /home/trader/autotrading/deploy/update.sh --restart
```

Or trigger it remotely via ntfy: `restart research`

---

## Useful VM commands

| Task | Command |
|---|---|
| Watch research logs live | `journalctl -u trading-research -f` |
| Watch paper agent logs | `journalctl -u trading-paper -f` |
| Check all service status | `systemctl status trading-*` |
| Restart research agent | `systemctl restart trading-research` |
| See last 50 log lines | `journalctl -u trading-research -n 50` |
| Check disk usage | `df -h` |
| Check memory | `free -h` |
| Check CPU | `htop` |

---

## Cost breakdown

| Service | Monthly cost |
|---|---|
| Hetzner CX11 VM | €3.29 |
| Anthropic API (estimated) | ~$30-40 |
| Supabase (free tier) | $0 |
| ntfy.sh | $0 |
| Streamlit Cloud | $0 |
| **Total** | **~€35/month** |

---

## Security notes

- The VM runs a firewall allowing SSH only
- The `.env_trading` file is chmod 600 (readable only by trader user)
- The `trader` user has no sudo access by default
- Live trading service does NOT auto-restart (requires manual start for safety)
- Never commit secrets to GitHub — they live only in `.env_trading` on the VM
