"""Session 48: compress 'weeks of forward testing' into minutes -- honestly.

Real forward-testing takes weeks because it needs NEW data. The fair fast
proxy: use ~60 days of 5-minute bars (yfinance's real limit, ~20x more
history than the 8 days of 1m) and do a WALK-FORWARD -- train on an older
block, test on the NEXT (unseen) block, roll forward, repeat. An edge that
is REAL shows up consistently across MANY test windows; a lucky fluke shows
up in one and vanishes in the others. Per symbol, gradient-boosted brain.
Still backtesting (can't fully replace real forward data), but far more
statistical power than one 3-day holdout. Zero Claude tokens.

    PYTHONPATH=. python scripts/walkforward_sim.py
"""
from __future__ import annotations
import numpy as np, pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from bots import marketdata

SYMBOLS=["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL"]
HORIZON=6        # 6 x 5m = 30 min ahead (same 30-min thesis)
COST=0.0002
PROB=0.55
TRAIN_BARS=2000  # ~1 week of 5m
TEST_BARS=500    # ~2.5 days of 5m per window

def features(df):
    c=df["close"]; f=pd.DataFrame(index=df.index)
    f["ret1"]=c.pct_change(); f["ret5"]=c.pct_change(5); f["ret15"]=c.pct_change(15)
    f["ma_f"]=c.rolling(10).mean()/c-1; f["ma_s"]=c.rolling(30).mean()/c-1
    d=c.diff(); up=d.clip(lower=0).rolling(14).mean(); dn=(-d.clip(upper=0)).rolling(14).mean()
    f["rsi"]=100-100/(1+up/dn.replace(0,np.nan))
    f["vol"]=c.pct_change().rolling(20).std()
    ef=c.ewm(span=5).mean(); es=c.ewm(span=34).mean(); f["mom"]=(ef-es)/c
    return f

def main():
    print(f"WALK-FORWARD over ~60d of 5m bars | 30-min horizon | prob>{PROB}\n"
          f"per symbol: net% per test window, then how many windows were green\n")
    grand_green=grand_tot=0
    for sym in SYMBOLS:
        try:
            df=marketdata.get_history(marketdata.resolve_symbol(sym),period="60d",interval="5m")
            df.columns=[c.lower() for c in df.columns]; df=df[~df.index.duplicated()]
        except Exception:
            print(f"  {sym}: no data"); continue
        X=features(df); y=(df["close"].shift(-HORIZON)>df["close"]).astype(int)
        data=X.join(y.rename("target")).dropna()
        px=df["close"].reindex(data.index).values
        cols=[c for c in data.columns if c!="target"]
        n=len(data)
        wins=[]; start=0
        while start+TRAIN_BARS+TEST_BARS<=n:
            tr=data.iloc[start:start+TRAIN_BARS]
            te=data.iloc[start+TRAIN_BARS:start+TRAIN_BARS+TEST_BARS]
            te_px=px[start+TRAIN_BARS:start+TRAIN_BARS+TEST_BARS]
            clf=GradientBoostingClassifier(n_estimators=50,max_depth=3,learning_rate=0.05)
            clf.fit(tr[cols],tr["target"])
            proba=clf.predict_proba(te[cols])[:,1]
            net=0.0; i=0
            while i<len(te)-HORIZON:
                if proba[i]>PROB:
                    net+=(te_px[i+HORIZON]-te_px[i])/te_px[i]-COST; i+=HORIZON
                else: i+=1
            wins.append(net)
            start+=TEST_BARS  # roll forward, non-overlapping test windows
        if not wins:
            print(f"  {sym}: too little data"); continue
        g=sum(1 for w in wins if w>0); tot=len(wins)
        grand_green+=g; grand_tot+=tot
        total=sum(wins)*100
        verdict="CONSISTENT" if g>=0.6*tot and total>0 else ("mixed" if total>0 else "loses")
        print(f"  {sym:7s}: {g}/{tot} windows green | total {total:+6.2f}% | {verdict}")
    print(f"\n  ACROSS ALL: {grand_green}/{grand_tot} test windows green "
          f"({grand_green/grand_tot*100:.0f}%) -- 50% == coin flip == no edge")

if __name__=="__main__": main()
