#!/usr/bin/env bash
# Weather-Bot sicil ozeti (paper + canli, sehir kirilimli).
cd "$(dirname "$0")" || exit 1
F=weather_trades.jsonl
[ -f "$F" ] || { echo "$F yok (henuz cozumlenmis islem yok)"; exit 0; }
echo "=== Weather-Bot sicil ($(date +%H:%M:%S)) ==="
awk '{
  i=index($0,"\"pnl\": "); p=substr($0,i+7)+0;
  w=($0 ~ /"won": true/); live=($0 ~ /"mode": "live"/);
  P+=p; n++; if(w)k++; if(live){lp+=p;ln++;if(w)lk++}
} END{
  if(n) printf "TOPLAM : islem=%d kazanc=%d (%.0f%%) PnL=%+.2f USDC\n", n,k,100*k/n,P;
  if(ln)printf "CANLI  : islem=%d kazanc=%d (%.0f%%) PnL=%+.2f USDC\n", ln,lk,100*lk/ln,lp;
}' "$F"
echo "-- sehir kirilimi --"
awk '{
  ci=index($0,"\"city\": \""); cs=substr($0,ci+9); c=substr(cs,1,index(cs,"\"")-1);
  i=index($0,"\"pnl\": "); p=substr($0,i+7)+0; w=($0 ~ /"won": true/);
  P[c]+=p; N[c]++; if(w)K[c]++
} END{for(x in N)printf "  %-10s islem=%-3d kazanc=%.0f%% PnL=%+.2f\n",x,N[x],100*K[x]/N[x],P[x]}' "$F"
