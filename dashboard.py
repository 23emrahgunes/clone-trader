"""Lightweight web dashboard for paper-trade PnL.

Serves a single auto-refreshing HTML page plus a JSON API, backed by the
PaperLedger. Runs in-process as another asyncio task alongside the bot, so
`pm2` keeps it alive with everything else.

Security: if ``DASHBOARD_TOKEN`` is set, every request must carry ``?key=<token>``.
The page is read-only (PnL numbers only — no secrets, no order controls), but
since it may be exposed on a public IP you should set a token.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

from paper import PaperLedger

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="tr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clone Trader — Paper PnL</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3;
         margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#8b949e; font-size:13px; margin-bottom:20px; }
  .cards { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:10px;
          padding:16px 20px; min-width:130px; }
  .card .label { color:#8b949e; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:24px; font-weight:600; margin-top:6px; }
  .pos { color:#3fb950; } .neg { color:#f85149; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:right; padding:10px 12px; border-bottom:1px solid #21262d; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  th { color:#8b949e; font-weight:500; text-transform:uppercase; font-size:11px; letter-spacing:.04em; }
  .muted { color:#6e7681; }
  .wrap { overflow-x:auto; border:1px solid #30363d; border-radius:10px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#3fb950; margin-right:6px; }
</style></head><body>
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <h1><span class="dot"></span>Clone Trader — PnL</h1>
    <div>
      <button onclick="reset('paper')" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px">🧪 Paper'ı sıfırla</button>
      <button onclick="reset('all')" style="background:#3d1418;color:#f85149;border:1px solid #5a1e24;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px">Hepsini sil</button>
    </div>
  </div>
  <div class="sub" id="meta">yükleniyor…</div>
  <div class="sub" id="modes"></div>
  <div class="cards" id="cards"></div>
  <h2 style="font-size:14px;color:#8b949e;margin:8px 0">Balina bazında</h2>
  <div class="wrap" style="margin-bottom:24px"><table>
    <thead><tr>
      <th>Balina</th><th>İşlem</th><th>Maliyet</th><th>Değer</th><th>PnL</th><th>PnL %</th>
    </tr></thead><tbody id="whales"></tbody>
  </table></div>
  <h2 style="font-size:14px;color:#8b949e;margin:8px 0">İşlemler</h2>
  <div class="wrap"><table>
    <thead><tr>
      <th>Pazar</th><th>Balina</th><th>Mod</th><th>Yön</th><th>Giriş</th><th>Size</th>
      <th>Maliyet</th><th>Güncel</th><th>Değer</th><th>PnL</th>
    </tr></thead><tbody id="rows"></tbody>
  </table></div>
<script>
const key = new URLSearchParams(location.search).get('key');
const money = v => (v===null||v===undefined) ? '—' : '$'+Number(v).toFixed(2);
const cls = v => v>0 ? 'pos' : (v<0 ? 'neg' : '');
const shortw = w => w ? (w.slice(0,6)+'…'+w.slice(-4)) : '?';
const badge = m => m==='live'
  ? '<span style="color:#f0883e;font-weight:600">● LIVE</span>'
  : '<span class="muted">paper</span>';
async function refresh(){
  try {
    const r = await fetch('/api/pnl' + (key ? ('?key='+encodeURIComponent(key)) : ''));
    if(!r.ok){ document.getElementById('meta').textContent='API hata: '+r.status; return; }
    const d = await r.json();
    const t = d.totals;
    document.getElementById('cards').innerHTML = [
      ['Balina', t.whale_count||0],
      ['İşlem', t.count],
      ['Maliyet', money(t.cost)],
      ['Güncel Değer', money(t.value)],
      ['PnL', '<span class="'+cls(t.pnl)+'">'+money(t.pnl)+'</span>'],
      ['PnL %', '<span class="'+cls(t.pnl)+'">'+t.pnl_pct.toFixed(2)+'%</span>'],
    ].map(([l,v])=>'<div class="card"><div class="label">'+l+'</div><div class="value">'+v+'</div></div>').join('');
    const md = d.modes||{};
    document.getElementById('modes').innerHTML = ['live','paper'].filter(m=>md[m]).map(m=>{
      const x=md[m];
      return badge(m)+' '+x.count+' işlem · PnL <span class="'+cls(x.pnl)+'">'+money(x.pnl)+'</span>';
    }).join(' &nbsp;|&nbsp; ');
    document.getElementById('whales').innerHTML = (d.whales||[]).map(w=>
      '<tr><td title="'+w.whale+'">'+shortw(w.whale)+'</td><td>'+w.count+'</td>'+
      '<td>'+money(w.cost)+'</td><td>'+money(w.value)+'</td>'+
      '<td class="'+cls(w.pnl)+'">'+money(w.pnl)+'</td>'+
      '<td class="'+cls(w.pnl)+'">'+w.pnl_pct.toFixed(2)+'%</td></tr>'
    ).join('') || '<tr><td colspan="6" class="muted">—</td></tr>';
    document.getElementById('rows').innerHTML = d.rows.slice().reverse().map(p=>{
      const ts = new Date(p.ts).toLocaleString('tr-TR');
      return '<tr><td>'+(p.market||p.token_id.slice(0,10))+'<div class="muted" style="font-size:11px">'+ts+'</div></td>'+
        '<td title="'+(p.whale||'')+'">'+shortw(p.whale)+'</td>'+
        '<td'+(p.tx?(' title="'+p.tx+'"'):'')+'>'+badge(p.mode)+'</td>'+
        '<td>'+p.side+'</td><td>'+Number(p.entry_price).toFixed(3)+'</td><td>'+p.size+'</td>'+
        '<td>'+money(p.cost_usdc)+'</td>'+
        '<td>'+(p.current_price===null?'—':Number(p.current_price).toFixed(3))+'</td>'+
        '<td>'+money(p.current_value)+'</td>'+
        '<td class="'+cls(p.pnl)+'">'+money(p.pnl)+'</td></tr>';
    }).join('') || '<tr><td colspan="10" class="muted">Henüz işlem yok — balina hareketi bekleniyor.</td></tr>';
    document.getElementById('meta').textContent =
      'Son güncelleme: ' + new Date(d.generated_at).toLocaleTimeString('tr-TR') + ' · 10 sn\\'de bir yenilenir';
  } catch(e){ document.getElementById('meta').textContent = 'Bağlantı hatası'; }
}
async function reset(scope){
  const msg = scope==='all' ? 'TÜM işlemler (paper + LIVE) silinecek. Emin misin?'
                            : 'Paper işlemler silinecek (canlı kayıtlar kalır). Emin misin?';
  if(!confirm(msg)) return;
  try {
    const q = 'scope='+scope+(key?('&key='+encodeURIComponent(key)):'');
    const r = await fetch('/api/reset?'+q, {method:'POST'});
    if(r.status===401){ alert('Yetkisiz (key gerekli).'); return; }
    const j = await r.json();
    alert((j.removed||0)+' kayıt silindi.');
    refresh();
  } catch(e){ alert('Sıfırlama hatası'); }
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""


class Dashboard:
    """aiohttp web server exposing the paper PnL page + JSON API."""

    def __init__(
        self,
        ledger: PaperLedger,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        token: Optional[str] = None,
    ) -> None:
        self._ledger = ledger
        self._host = host
        self._port = port
        self._token = token
        self._runner: Optional[web.AppRunner] = None

    def _authorized(self, request: web.Request) -> bool:
        if not self._token:
            return True
        return request.query.get("key") == self._token

    async def _index(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        return web.Response(text=_PAGE, content_type="text/html")

    async def _api(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            data = await self._ledger.compute_pnl()
        except Exception:  # noqa: BLE001 - dashboard must not crash the process
            logger.exception("compute_pnl failed")
            return web.json_response({"error": "compute_failed"}, status=500)
        return web.json_response(data)

    async def _reset(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        scope = request.query.get("scope", "paper")
        if scope not in ("paper", "live", "all"):
            scope = "paper"
        removed = self._ledger.reset(scope)
        logger.info("Ledger reset via dashboard (scope=%s, removed=%d)", scope, removed)
        return web.json_response({"removed": removed, "scope": scope})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/pnl", self._api)
        app.router.add_post("/api/reset", self._reset)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Dashboard listening on http://%s:%d (auth=%s)",
                    self._host, self._port, "on" if self._token else "off")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
