# limit-trader — Polymarket BTC 5dk CLOB Market-Maker

Polymarket CLOB üzerinde **BTC 5 dakikalık (up/down)** marketlerinde çalışan penny-scalp
market-maker botu. Her yeni 5dk marketini otomatik keşfeder, YES ve NO tarafına 1¢ limit
alım koyar, dolan tarafı 2¢'ten satar, kapanışa 30 sn kala tüm emirleri iptal eder (cancel-all),
market kapanınca bir sonraki markete geçer.

> ⚠️ **Gerçek USDC ile canlı emir gönderir.** Çalıştırma ve fon sorumluluğu kullanıcıdadır.
> 1¢ alımlar defterin dibindedir; çoğu dolmaz, dolanlar vade sonunda tam zarara açıktır.

## Gereksinimler

- Python 3.11+
- `py-clob-client-v2` (fork — `SignatureTypeV2.POLY_1271=3` destekler; resmi py-clob-client 3'ü reddeder)

## Kurulum (VPS)

```bash
git clone -b limit-trader-btc5m https://github.com/23emrahgunes/clone-trader.git
cd clone-trader/limit-trader

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Yapılandırma

```bash
cp .env.example .env
nano .env     # PRIVATE_KEY, PROXY_WALLET_ADDRESS, SIGNATURE_TYPE=3 doldurun
```

`.env` asla commit edilmez (`.gitignore` dışlar). Kritik alanlar:

| Alan | Açıklama |
|---|---|
| `PRIVATE_KEY` | Polygon cüzdan özel anahtarı (emirleri imzalar) |
| `PROXY_WALLET_ADDRESS` | CLOB funder adresi (USDC/pozisyonların durduğu proxy) |
| `SIGNATURE_TYPE` | 0/1/2/3 — proxy+EIP-1271 kurulumunda **3 (POLY_1271)** |
| `SHARES_PER_ORDER` | Emir başına share (min $1 kuralı: 1¢×100=$1) |

### Doğru imza tipini kesinleştir (read-only, emir göndermez)

```bash
python diag_balance.py
```

Gerçek USDC bakiyeni gösteren `sig_type` değerini `.env`'de `SIGNATURE_TYPE=<o>` yapın.

## Çalıştırma

Önce emir göndermeden tüm döngüyü (keşif/fill/iptal) test edin:

```bash
DRY_RUN=true python bot.py
```

Loglarda güncel `btc-updown-5m-*` slug'ı, YES/NO token'ları ve `seconds_to_close` doğruysa canlıya geçin:

```bash
python bot.py            # .env içinde DRY_RUN=false
```

## VPS'te sürekli çalıştırma (systemd)

`/etc/systemd/system/limit-trader.service`:

```ini
[Unit]
Description=Polymarket BTC 5m CLOB market-maker
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/clone-trader/limit-trader
ExecStart=/root/clone-trader/limit-trader/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now limit-trader
sudo journalctl -u limit-trader -f      # canlı log
```

Alternatif (hızlı): `tmux new -s bot` → `python bot.py` → `Ctrl+b d` ile ayrıl.

## Ayarlanabilir parametreler (.env)

`BUY_PRICE` (0.01) · `SELL_PRICE` (0.02) · `ORDER_COUNT` (5) · `SHARES_PER_ORDER` (100) ·
`POLL_INTERVAL` (0.5s) · `EXPIRY_CANCEL_SECONDS` (30) · `DRY_RUN` (false)
