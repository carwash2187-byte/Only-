"""Session 48 brick #3: does a trend-strength (ADX) entry filter make the
edge BROAD instead of one-symbol (OIL)?

Hypothesis (most robust cross-market edge there is): the agent loses on 7/9
symbols because it enters in CHOP. Requiring a real trend at entry (ADX >=
threshold) should skip the chop and lift more symbols positive. Tested
per-symbol on unseen days, with the min-hold-30 exit already applied.
Read-only, zero Claude tokens.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/trend_filter_experiment.py
"""
from __future__ import annotations
import pandas as pd
from bots.learning.agent import QTraderAgent, extract_state
from bots.organization import adx_value
from bots import marketdata

TRAINED = {"2026-06-22","2026-06-10","2026-06-11","2026-05-18","2026-06-09","2026-06-08",
"2026-06-25","2026-06-24","2026-07-08","2026-07-06","2026-05-21","2026-05-20","2026-05-15",
"2026-05-19","2026-06-12","2026-06-05","2026-06-23","2026-07-21","2026-07-07","2026-07-13",
"2026-05-11","2026-06-18","2026-07-14","2026-07-02"}
SYMBOLS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL"]
COST = 0.0002
MIN_HOLD = 30
ADX_WIN = 30

def net_for(agent, df, adx_min):
    holding=False; entry=0.0; held=0; net=0.0; trades=0
    for i in range(30,len(df)-1):
        s=extract_state(df,i,holding); a=agent.choose_action(s,explore=False)
        p=float(df["close"].iloc[i])
        if a=="buy" and not holding:
            if adx_min > 0:
                sub = df.iloc[max(0,i-ADX_WIN):i+1]
                adx = adx_value(sub)
                if adx is None or adx < adx_min:
                    continue  # skip: not enough trend
            holding=True; entry=p; held=0
        elif holding:
            held+=1
            if a=="sell" and held>=MIN_HOLD:
                net+=(p-entry)/entry-COST; trades+=1; holding=False
    return net, trades

def eval_all(agent, frames, adx_min):
    per={}
    for sym,df in frames.items():
        days=sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
        N=0.0;T=0
        for day in [d for d in days if d not in TRAINED]:
            d=df[df.index.normalize()==pd.Timestamp(day,tz=df.index.tz)]
            if len(d)>60:
                n,t=net_for(agent,d,adx_min); N+=n;T+=t
        per[sym]=(N,T)
    return per

def main():
    frames={}
    for sym in SYMBOLS:
        try:
            df=marketdata.get_history(marketdata.resolve_symbol(sym),period="8d",interval="1m")
            df.columns=[c.lower() for c in df.columns]
            if isinstance(df.index,pd.DatetimeIndex): frames[sym]=df
        except Exception: pass
    agent=QTraderAgent(); agent.load()
    print(f"{len(frames)} symbols, min-hold {MIN_HOLD}, testing ADX entry filters\n")

    for adx_min in [0, 20, 25, 30]:
        per=eval_all(agent,frames,adx_min)
        total=sum(N for N,_ in per.values())
        wins=sum(1 for N,_ in per.values() if N>0)
        tot_tr=sum(T for _,T in per.values())
        label = "no filter" if adx_min==0 else f"ADX>={adx_min}"
        print(f"--- {label} --- total {total*100:+.2f}%, {wins}/{len(per)} symbols green, {tot_tr} trades")
        for sym,(N,T) in per.items():
            print(f"     {'＋' if N>0 else '－'} {sym:7s} {N*100:+6.2f}% ({T})")
        print()

if __name__=="__main__": main()
