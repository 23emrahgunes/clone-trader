#!/usr/bin/env bash
# Weather-Bot tek-komut baslat (PAPER). Eski log/ledger backups/ altina yedeklenir.
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
echo "=== Weather-Bot deploy ($DIR) ==="

if [ ! -d ".venv" ]; then
  echo "venv yok, kuruluyor..."
  python3 -m venv .venv --without-pip 2>/dev/null || python3 -m venv .venv
  . .venv/bin/activate
  if ! python -m pip --version >/dev/null 2>&1; then
    curl -sL https://bootstrap.pypa.io/get-pip.py -o get-pip.py && python get-pip.py
  fi
  pip install -q -r requirements.txt
else
  . .venv/bin/activate
fi

[ -f ".env" ] || { echo "HATA: .env yok. .env.example'i .env yapip doldurun."; exit 1; }

echo "-- calisan durduruluyor --"
pkill -INT -f weather_bot.py 2>/dev/null; sleep 2; pkill -9 -f weather_bot.py 2>/dev/null; sleep 1

mkdir -p backups; ts=$(date +%Y%m%d-%H%M%S)
[ -f weather.log ] && mv weather.log "backups/weather.$ts.log"

echo "-- baslatiliyor (port 8095) --"
nohup python -u weather_bot.py > weather.log 2>&1 &
sleep 3
pgrep -af weather_bot.py || echo "UYARI: baslamadi -> weather.log bakin"
echo
echo "Izleme : tail -f $DIR/weather.log"
echo "Ozet   : bash $DIR/stats.sh"
echo "=== bitti ==="
