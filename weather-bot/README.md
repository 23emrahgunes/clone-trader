# Weather-Bot — Polymarket sıcaklık marketleri ("zaten oldu" edge)

Polymarket'in **günlük yüksek sıcaklık** marketlerinde ("Bugün {şehir} high ≥ T°F mi?")
**gözleme dayalı near-resolution edge** oynar. Kriptonun aksine hava **tahmin edilebilir**
ve gün içinde **gözlemlenebilir** — bu yüzden gerçek bir edge var.

## Fikir

Günün max sıcaklığı öğleden sonra fiilen kilitlenir; market bunu geç fiyatlar.
- Gözlenen high **≥ eşik** → "EVET" **kesin** → ucuz YES'i al.
- Zirve geçti, gözlenen **< eşik** → "HAYIR" kesin → ucuz NO'yu al.
- Arada belirsizliği Open-Meteo ensemble (kalan gün) verir.

```
P(YES) = final_max ≥ T olan ensemble üye oranı   (final = max(gözlenen, kalan_gün_max))
EV = P − fiyat − fee ;  |EV| ≥ EDGE_MARGIN → sinyal
```

> ⚠️ **Kanıt önce (paper), sonra canlı.** Varsayılan PAPER; dashboard'daki **ARM** ile canlı.
> Bu bir kazanç garantisi değil — edge gerçek ama küçük; paper sicili doğrular.

## Veri kaynakları (ücretsiz, key gerekmez)
- **Gözlem:** [aviationweather METAR](https://aviationweather.gov/) — resolution istasyonuyla birebir (KNYC, KMDW, KLAX, KMIA, KSFO), gece yarısından beri max.
- **Ensemble:** [Open-Meteo Ensemble](https://open-meteo.com/en/docs/ensemble-api) — ECMWF/GFS/ICON üyeleri.
- **Resolution:** NWS Daily Climate Report (CLI), günlük HIGH, °F. Paper çözümlemesi Open-Meteo archive ile.

## Şehirler (5 ABD)
NYC (KNYC), Chicago (KMDW), LA (KLAX), Miami (KMIA), SF (KSFO) — net CLI resolution.

## Kurulum
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # PRIVATE_KEY, PROXY_WALLET, SIGNATURE_TYPE=3
```

## Test / çalıştırma
```bash
python weather_bot.py --check     # config + veri baglaci (her sehir icin gozlenen high + P)
python weather_bot.py --scan      # aktif weather marketlerini listele
python weather_bot.py             # bot (PAPER) + dashboard
```
Arka planda: `nohup python -u weather_bot.py > weather.log 2>&1 &`

## Dashboard (port 8095)
Market tablosu (şehir, eşik, gözlenen high, zirve, P_yes, YES/NO ask, EV, sinyal),
paper işlem/isabet/PnL + equity, olay akışı, **ARM/DURDUR**.
- Public: `.env`'de `DASHBOARD_TOKEN` + firewall'da 8095 → `http://IP:8095/?key=...`
- Ya da SSH/IAP tüneli → `http://localhost:8095`

## Ayarlar (.env)
`CITIES` · `EDGE_MARGIN` (0.05) · `ORDER_SHARES` (10) · `POLL_INTERVAL` (30s) · `FEE_BPS` (0) · `DASHBOARD_PORT` (8095)

## Dürüst notlar
- **Resolution hassasiyeti = her şey:** doğru istasyon/eşik/tarih/birim (°F, yerel gece yarısı). Yanlışı sistematik kayıp.
- CLI (final, ertesi gün 8AM ET) vs METAR (intraday) farkı olabilir.
- Edge gerçek ama **küçük** → hacim + disiplin; köşe dönmek değil pozitif-EV grind.
- `--scan` çıktısına göre market parser'ı ayarlanabilir (gerçek soru formatı).

Durdurma: `pkill -INT -f weather_bot.py`
