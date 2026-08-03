# Signal-Trader — Polymarket BTC 5dk yönlü (bilgi/latency) bot

5 dakikalık BTC up/down marketlerinde **yönlü** işlem yapar. Pasif dip scalp veya
YES+NO arb değil — **fiyat modeline dayalı** bir edge dener ve **önce paper'da kendini
kanıtlar**, sonra tek tıkla canlıya geçer.

## Model (ilkeli, uydurma değil)

Pencere kapanınca **Up** kazanır eğer BTC kapanış > açılış (**price-to-beat**). Herhangi bir `t` anında:

```
d     = anlık_BTC − price_to_beat        (sınıra uzaklık)
τ     = kapanışa kalan saniye
σ     = anlık oynaklık (son VOL_WINDOW_SEC tick'ten)
P(Up) = Φ( d / (σ·√τ) )                   (Bachelier / normal dijital opsiyon)
```

Bu olasılığı market'in ima ettiği fiyatla (YES/NO ask) kıyaslar. **Fee düşülmüş beklenen
değer** `EV = P − ask − fee` eşiği (`EDGE_MARGIN`) aşarsa ucuz tarafı alır.

## Kanıt önce (paper), sonra canlı

- Varsayılan **PAPER**: gerçek emir yok. Her sinyal + **sonucu** (`signal_trades.jsonl`) kaydedilir;
  dashboard'da **isabet oranı + kümülatif PnL + equity eğrisi** birikir. **Kanıt budur.**
- Sicil pozitifse dashboard'daki **▶ CANLIYA GEÇ (ARM)** butonu ile canlı emir açılır.
  **■ DURDUR** ile paper'a döner. Canlıda da paper kaydı sürer.
- Veri kaynağı = Polymarket'in **settle ettiği** Chainlink RTDS BTC/USD feed'i (birebir).

> ⚠️ Bu bir kazanç garantisi değildir. Model bir hipotez; onu **paper sicili** doğrular ya da
> çürütür. Canlıya ancak ölçülmüş pozitif beklenti gördükten sonra geç.

## Kurulum (VPS)

```bash
git clone -b signal-trader https://github.com/23emrahgunes/clone-trader.git
cd clone-trader/signal-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # PRIVATE_KEY, PROXY_WALLET, SIGNATURE_TYPE=3
```

Doğrula: `python signal_bot.py --check`

## Çalıştırma

```bash
nohup python -u signal_bot.py > signal.log 2>&1 &
tail -f signal.log
```

Bot **PAPER** başlar. Dashboard'dan izle; sicil ikna edince **ARM** ile canlıya geç.

## Web arayüzü (port 8091)

Market **ismi büyük** yazılı; canlı BTC fiyatı, price-to-beat, mesafe, σ, model P(Up),
YES/NO ask, EV, güncel sinyal, açık pozisyon, paper işlem/isabet/PnL + equity eğrisi,
olay/sonuç akışı ve **ARM/DURDUR** butonu.

- **SSH tüneli:** `ssh -L 8091:localhost:8091 KULLANICI@VPS_IP` → `http://localhost:8091`
- **Public:** `.env`'de `DASHBOARD_TOKEN=...` + firewall'da 8091 → `http://VPS_IP:8091/?key=...`

## Ayarlar (.env)

`EDGE_MARGIN` (0.05) · `VOL_WINDOW_SEC` (90) · `MIN_SEC_TO_CLOSE` (20) · `MIN_ELAPSED_SEC` (15) ·
`ORDER_SHARES` (5) · `DASHBOARD_PORT` (8091)

Durdurma: `pkill -INT -f signal_bot.py`
