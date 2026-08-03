#!/usr/bin/env bash
# Signal-Trader tek-komut deploy: normal + TERS instance'larini SIFIRDAN baslatir.
# Eski ledger/loglar silinmez -> backups/ altina yedeklenir.
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
echo "=== Signal-Trader deploy ($DIR) ==="

# --- venv ---
if [ ! -d ".venv" ]; then
  echo "venv yok, kuruluyor..."
  python3 -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
else
  . .venv/bin/activate
fi

# --- .env kontrol ---
if [ ! -f ".env" ]; then
  echo "HATA: .env yok. .env.example'i .env yapip doldurun."; exit 1
fi

# --- calisan instance'lari durdur ---
echo "-- calisan instance'lar durduruluyor --"
pkill -INT -f signal_bot.py 2>/dev/null
sleep 2
pkill -9 -f signal_bot.py 2>/dev/null
sleep 1

# --- eski ledger/loglari yedekle ---
echo "-- eski ledger/loglar backups/ altina yedekleniyor --"
mkdir -p backups
ts=$(date +%Y%m%d-%H%M%S)
for f in signal_trades.jsonl reverse_trades.jsonl signal.log reverse.log; do
  [ -f "$f" ] && mv "$f" "backups/$f.$ts"
done

# --- ikisini de sifirdan baslat ---
echo "-- NORMAL baslatiliyor (port 8091) --"
nohup python -u signal_bot.py > signal.log 2>&1 &
echo "-- TERS baslatiliyor (port 8092) --"
REVERSE=true DASHBOARD_PORT=8092 LEDGER_FILE=reverse_trades.jsonl nohup python -u signal_bot.py > reverse.log 2>&1 &
sleep 3

# --- durum ---
echo "-- calisan instance'lar --"
pgrep -af signal_bot.py || echo "UYARI: instance bulunamadi -> signal.log / reverse.log bakin"
echo
echo "Izleme : tail -f $DIR/signal.log    (normal)"
echo "         tail -f $DIR/reverse.log   (ters)"
echo "Ozet   : bash $DIR/stats.sh"
echo "=== bitti ==="
