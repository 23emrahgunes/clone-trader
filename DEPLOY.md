# Deploy — clone-trader (VPS + systemd)

Ubuntu/Debian VPS assumed. Replace `<REPO_URL>` and secrets with real values.

## 1. System deps
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
```

## 2. Dedicated user + code
```bash
sudo useradd -r -m -d /opt/clone-trader -s /usr/sbin/nologin clonetrader
sudo -u clonetrader git clone <REPO_URL> /opt/clone-trader
cd /opt/clone-trader
```

## 3. Virtualenv + dependencies
```bash
sudo -u clonetrader python3.11 -m venv /opt/clone-trader/.venv
sudo -u clonetrader /opt/clone-trader/.venv/bin/pip install -U pip
sudo -u clonetrader /opt/clone-trader/.venv/bin/pip install -r requirements.txt
```

## 4. Secrets — DONE BY YOU (never by the assistant)
```bash
sudo -u clonetrader cp /opt/clone-trader/.env.example /opt/clone-trader/.env
sudo -u clonetrader nano /opt/clone-trader/.env    # fill PRIVATE_KEY, RPC_URL, tokens, etc.
sudo chmod 600 /opt/clone-trader/.env
```
Keep `DRY_RUN=true` for the first live-connectivity test.

## 5. Smoke test (paper mode)
```bash
sudo -u clonetrader /opt/clone-trader/.venv/bin/python /opt/clone-trader/config.py   # config loads?
sudo -u clonetrader /opt/clone-trader/.venv/bin/python /opt/clone-trader/main.py     # Ctrl+C to stop
```

## 6. systemd service
```bash
sudo cp /opt/clone-trader/deploy/clone-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clone-trader
sudo systemctl status clone-trader
journalctl -u clone-trader -f
```

## 7. Go live (only after paper looks right)
Set `DRY_RUN=false` in `.env`, then `sudo systemctl restart clone-trader`.
The bot still boots **PAUSED** — send `/arm` in Telegram to enable real orders,
`/kill` to stop instantly.
