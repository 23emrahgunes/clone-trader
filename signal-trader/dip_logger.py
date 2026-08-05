"""Dip-logger v2: dip'ler GERCEKCI cikisla yakalanabilir mi? (READ-ONLY, emir yok)

Fark: v1 cikisi max_bid (tepe) idi -> LOOK-AHEAD (gelecegi bilme) hatasi, sisirilmis.
v2 GERCEKCI kural:
  - Dip: bir tarafin en iyi ASK'i esige (25c/15c/5c) ilk kez dusunce -> ASK'tan AL.
  - Take-profit: bid >= entry_ask + TP olunca -> o limitten SAT (gercekci dolum).
  - TP hic dolmazsa -> pencere sonunda son bid'e sat (piyasa cikisi).
  Look-ahead yok. Ayrica karsilastirma icin eski (max_bid) rakami da kaydedilir.

    python dip_logger.py

Ayarlar (.env / ortam):
  DIP_THRESHOLDS (vars. "0.25,0.15,0.05") · DIP_TAKE_PROFIT (vars. 0.03)
  DIP_SCAN_INTERVAL (vars. 1) · DIP_ASSET (vars. btc)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv()
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
KEY = os.getenv("PRIVATE_KEY", "").strip()
FUNDER = (os.getenv("PROXY_WALLET_ADDRESS") or os.getenv("PM_EDGE_FUNDER_ADDRESS") or "").strip()
SIG = int(os.getenv("SIGNATURE_TYPE", "3"))
API_KEY, API_SECRET, API_PASS = (os.getenv("CLOB_API_KEY"), os.getenv("CLOB_API_SECRET"),
                                 os.getenv("CLOB_API_PASSPHRASE"))

THRESHOLDS = sorted([float(x) for x in os.getenv("DIP_THRESHOLDS", "0.25,0.15,0.05").split(",") if x.strip()],
                    reverse=True)
TAKE_PROFIT = float(os.getenv("DIP_TAKE_PROFIT", "0.03"))
INTERVAL = float(os.getenv("DIP_SCAN_INTERVAL", "1"))
ASSET = os.getenv("DIP_ASSET", "btc").lower()
FEE_RATE = 0.10
FEE_EXP = 1.0
HITS = "dip_events.jsonl"
WINDOW = 300
ALIASES = {"btc": ["btc", "bitcoin"], "eth": ["eth", "ethereum"], "sol": ["sol", "solana"],
           "xrp": ["xrp", "ripple"], "doge": ["doge", "dogecoin"]}


def _client() -> ClobClient:
    creds = None
    if API_KEY and API_SECRET and API_PASS:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)
    c = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY, creds=creds,
                   signature_type=SIG, funder=FUNDER or None)
    if creds is None and KEY:
        c.set_api_creds(c.derive_api_key())
    return c


def _coerce(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            p = json.loads(v)
            return p if isinstance(p, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _find_market(names):
    now = int(time.time())
    win = now - (now % WINDOW)
    for off in (0, -1, 1):
        ts = win + off * WINDOW
        for a in names:
            for pat in ("{a}-updown-5m-{ts}", "{a}-up-or-down-5m-{ts}"):
                slug = pat.format(a=a, ts=ts)
                try:
                    r = requests.get(f"{GAMMA}/markets/slug/{slug}", timeout=5)
                    if r.status_code != 200:
                        continue
                    p = r.json()
                except Exception:
                    continue
                if p.get("active") is not True:
                    continue
                toks = _coerce(p.get("clobTokenIds"))
                if len(toks) < 2:
                    continue
                outs = [str(o).strip().lower() for o in _coerce(p.get("outcomes"))]
                ui = next((i for i, o in enumerate(outs) if o in ("up", "yes", "long")), 0)
                di = next((i for i, o in enumerate(outs) if o in ("down", "short", "no")), 1)
                return {"slug": p.get("slug", slug), "yes": str(toks[ui]), "no": str(toks[di]),
                        "closes": ts + WINDOW, "window": win}
    return None


def _best(book):
    def rows(side):
        s = getattr(book, side, None)
        if s is None and isinstance(book, dict):
            s = book.get(side)
        out = []
        for e in (s or []):
            pr = getattr(e, "price", None) if not isinstance(e, dict) else e.get("price")
            sz = getattr(e, "size", None) if not isinstance(e, dict) else e.get("size")
            try:
                out.append((float(pr), float(sz)))
            except (TypeError, ValueError):
                continue
        return out
    asks = rows("asks"); bids = rows("bids")
    best_ask = min((p for p, _ in asks), default=None)
    best_bid = max((p for p, _ in bids), default=None)
    return best_ask, best_bid


def _fee(price):
    if price is None or price <= 0 or price >= 1:
        return 0.0
    return FEE_RATE * (price * (1 - price)) ** FEE_EXP


class DipLogger:
    def __init__(self, client):
        self.c = client
        self.window = -1
        self.market = None
        self.events = {}    # (side, T) -> event
        self.last = {}      # side -> (bid, ask)
        self.scans = 0
        self.stats = {T: {"n": 0, "tp": 0, "real": 0.0, "look": 0.0} for T in THRESHOLDS}

    def _finalize(self):
        if not self.events:
            self.last.clear()
            return
        yb = self.last.get("yes", (0.0, 0.0))[0]
        nb = self.last.get("no", (0.0, 0.0))[0]
        winner = "yes" if yb >= nb else "no"
        for (side, T), e in self.events.items():
            ea = e["entry_ask"]
            mb = e["max_bid"] if e["max_bid"] is not None else e["entry_bid"] or 0.0
            lb = e["last_bid"] if e["last_bid"] is not None else e["entry_bid"] or 0.0
            # LOOK-AHEAD (eski, sisik): tepe bid'e sat
            pnl_look = mb - ea - _fee(ea) - _fee(mb)
            # GERCEKCI: TP dolduysa ea+TP'den sat, yoksa son bid'e sat
            if e["tp_hit"]:
                exit_r = ea + TAKE_PROFIT
                pnl_real = TAKE_PROFIT - _fee(ea) - _fee(exit_r)
            else:
                exit_r = lb
                pnl_real = lb - ea - _fee(ea) - _fee(lb)
            won = (side == winner)
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "window": e["window"],
                   "side": side, "threshold": T, "entry_ask": round(ea, 3),
                   "entry_bid": round(e["entry_bid"] or 0.0, 3), "max_bid": round(mb, 3),
                   "last_bid": round(lb, 3), "tp_hit": e["tp_hit"], "take_profit": TAKE_PROFIT,
                   "exit_realistic": round(exit_r, 3),
                   "scalp_realistic": round(pnl_real, 4), "scalp_lookahead": round(pnl_look, 4),
                   "resolved_won": won, "sec_to_close_at_dip": e["stc"]}
            try:
                with open(HITS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception:
                pass
            s = self.stats[T]
            s["n"] += 1; s["tp"] += int(e["tp_hit"]); s["real"] += pnl_real; s["look"] += pnl_look
            print(f"[{time.strftime('%H:%M:%S')}] DIP {side.upper()} <= {T:.2f} "
                  f"ask={ea:.3f} {'TP-vurdu' if e['tp_hit'] else 'TP-yok'} "
                  f"gercekci={pnl_real:+.4f} (lookahead={pnl_look:+.4f}) "
                  f"{'(kazanan)' if won else ''}")
        self.events.clear(); self.last.clear()

    def scan_once(self):
        now = int(time.time())
        win = now - (now % WINDOW)
        if win != self.window:
            self._finalize()
            self.window = win
            self.market = _find_market(ALIASES.get(ASSET, [ASSET]))
            if self.market:
                print(f"[{time.strftime('%H:%M:%S')}] pencere {win} · {self.market['slug']}")
        self.scans += 1
        m = self.market
        if not m:
            return
        stc = m["closes"] - time.time()
        for side in ("yes", "no"):
            try:
                ask, bid = _best(self.c.get_order_book(m[side]))
            except Exception:
                continue
            if ask is None:
                continue
            self.last[side] = (bid if bid is not None else 0.0, ask)
            for T in THRESHOLDS:
                key = (side, T)
                if key not in self.events and ask <= T:
                    self.events[key] = {"window": win, "entry_ask": ask, "entry_bid": bid,
                                        "max_bid": bid, "last_bid": bid, "tp_hit": False,
                                        "t": now, "stc": round(stc)}
                if key in self.events and bid is not None:
                    e = self.events[key]
                    if e["max_bid"] is None or bid > e["max_bid"]:
                        e["max_bid"] = bid
                    e["last_bid"] = bid
                    if not e["tp_hit"] and bid >= e["entry_ask"] + TAKE_PROFIT:
                        e["tp_hit"] = True

    def summary(self):
        parts = []
        for T in THRESHOLDS:
            s = self.stats[T]
            if s["n"]:
                parts.append(f"<= {T:.2f}: n={s['n']} tp={100*s['tp']/s['n']:.0f}% "
                             f"GERCEKCI={s['real']/s['n']:+.4f} (look={s['look']/s['n']:+.4f})")
        return " | ".join(parts) if parts else "henuz dip yok"

    def run(self):
        print(f"Dip-logger v2 (GERCEKCI cikis). Esikler={THRESHOLDS} take_profit={TAKE_PROFIT} "
              f"aralik={INTERVAL}sn -> '{HITS}'. (Ctrl+C ile dur)")
        while True:
            t0 = time.time()
            try:
                self.scan_once()
                if self.scans % 20 == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] tarama={self.scans} | {self.summary()}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{time.strftime('%H:%M:%S')}] hata: {type(exc).__name__}: {exc}")
            time.sleep(max(0.0, INTERVAL - (time.time() - t0)))


def main():
    if not KEY:
        print("PRIVATE_KEY .env'de yok."); raise SystemExit(1)
    try:
        DipLogger(_client()).run()
    except KeyboardInterrupt:
        print("\nDip-logger durduruldu.")


if __name__ == "__main__":
    main()
