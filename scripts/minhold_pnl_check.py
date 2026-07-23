"""Confirm the min-hold win-rate gain is REAL profit, not just more wins.
Measures total return + expectancy per trade across unseen days, per min-hold.
Read-only on the live Q-table."""
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

def run(agent, df, min_hold):
    holding=False; entry=0.0; held=0; net=0.0; trades=0
    for i in range(30,len(df)-1):
        s=extract_state(df,i,holding); a=agent.choose_action(s,explore=False)
        p=float(df["close"].iloc[i])
        if a=="buy" and not holding: holding=True; entry=p; held=0
        elif holding:
            held+=1
            if a=="sell" and held>=min_hold:
                net += (p-entry)/entry - COST; trades+=1; holding=False
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
    print(f"model {len(agent.q)} states, {len(frames)} symbols (unseen days only)\n")
    for mh in [0,15,30,60]:
        NET=0.0; T=0
        for sym,df in frames.items():
            days=sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
            for day in [d for d in days if d not in TRAINED]:
                d=df[df.index.normalize()==pd.Timestamp(day,tz=df.index.tz)]
                if len(d)>60:
                    n,t=run(agent,d,mh); NET+=n; T+=t
        exp = NET/T*100 if T else 0
        print(f"min-hold {mh:3d}: {T:5d} trades | TOTAL net {NET*100:+7.2f}% | "
              f"expectancy/trade {exp:+.4f}% | {'PROFITABLE' if NET>0 else 'loses money'}")

if __name__=="__main__": main()
