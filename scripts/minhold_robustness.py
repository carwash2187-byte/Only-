"""Session 48 brick #2: is the min-hold-30 profit BROAD or one lucky symbol?

A +1.6% out-of-sample total means nothing if it's one symbol carrying it and
the rest bleed -- that's a fragile fluke, not an edge. This breaks the
min-hold-30 unseen-day P&L down PER SYMBOL, and also finer-sweeps the hold
(20/25/30/35/40) to confirm 30 is really the peak, not noise. Read-only.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/minhold_robustness.py
"""
from __future__ import annotations
import pandas as pd
from bots.learning.agent import QTraderAgent, extract_state
from bots import marketdata

TRAINED = {"2026-06-22","2026-06-10","2026-06-11","2026-05-18","2026-06-09","2026-06-08",
"2026-06-25","2026-06-24","2026-07-08","2026-07-06","2026-05-21","2026-05-20","2026-05-15",
"2026-05-19","2026-06-12","2026-06-05","2026-06-23","2026-07-21","2026-07-07","2026-07-13",
"2026-05-11","2026-06-18","2026-07-14","2026-07-02"}
SYMBOLS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL"]
COST = 0.0002

def net_for(agent, df, min_hold):
    holding=False; entry=0.0; held=0; net=0.0; trades=0
    for i in range(30,len(df)-1):
        s=extract_state(df,i,holding); a=agent.choose_action(s,explore=False)
        p=float(df["close"].iloc[i])
        if a=="buy" and not holding: holding=True; entry=p; held=0
        elif holding:
            held+=1
            if a=="sell" and held>=min_hold:
                net+=(p-entry)/entry-COST; trades+=1; holding=False
    return net, trades

def main():
    frames={}
    for sym in SYMBOLS:
        try:
            df=marketdata.get_history(marketdata.resolve_symbol(sym),period="8d",interval="1m")
            df.columns=[c.lower() for c in df.columns]
            if isinstance(df.index,pd.DatetimeIndex): frames[sym]=df
        except Exception: pass
    agent=QTraderAgent(); agent.load()

    print("=== PER-SYMBOL P&L at min-hold 30 (unseen days) ===")
    pos=neg=0
    for sym,df in frames.items():
        days=sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
        N=0.0; T=0
        for day in [d for d in days if d not in TRAINED]:
            d=df[df.index.normalize()==pd.Timestamp(day,tz=df.index.tz)]
            if len(d)>60:
                n,t=net_for(agent,d,30); N+=n; T+=t
        flag = "＋" if N>0 else "－"
        if N>0: pos+=1
        else: neg+=1
        print(f"  {flag} {sym:7s}: {N*100:+7.2f}%  ({T} trades)")
    print(f"\n  {pos}/{pos+neg} symbols profitable at min-hold 30 "
          f"({'BROAD -- real' if pos>neg else 'FRAGILE -- one-symbol-driven'})")

    print("\n=== finer hold sweep (is 30 really the peak?) ===")
    for mh in [20,25,30,35,40]:
        N=0.0; T=0
        for sym,df in frames.items():
            days=sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
            for day in [d for d in days if d not in TRAINED]:
                d=df[df.index.normalize()==pd.Timestamp(day,tz=df.index.tz)]
                if len(d)>60:
                    n,t=net_for(agent,d,mh); N+=n; T+=t
        print(f"  min-hold {mh}: {N*100:+7.2f}%  ({T} trades)")

if __name__=="__main__": main()
