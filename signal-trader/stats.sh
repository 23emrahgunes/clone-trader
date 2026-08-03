#!/usr/bin/env bash
# Normal vs TERS sicil ozeti (islem sayisi, kazanc orani, PnL).
cd "$(dirname "$0")" || exit 1
echo "=== Signal-Trader sicil ($(date +%H:%M:%S)) ==="
for f in signal_trades.jsonl reverse_trades.jsonl; do
  if [ -f "$f" ]; then
    awk -v F="$f" '{
      i=index($0,"\"pnl\": "); p=substr($0,i+7)+0;
      w=($0 ~ /"won": true/); P+=p; n++; if(w)k++
    } END{
      if(n) printf "%-22s islem=%-4d kazanc=%d (%.0f%%)  PnL=%+.2f USDC\n", F, n, k, 100*k/n, P;
      else  printf "%-22s (henuz islem yok)\n", F
    }' "$f"
  else
    printf "%-22s (dosya yok)\n" "$f"
  fi
done
