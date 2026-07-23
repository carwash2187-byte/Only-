"""Session 48: pivot to YEARS of real daily data (free, unlike intraday which
caps at ~60 days) -- tests a multi-day SWING edge instead of intraday, since
intraday is now conclusively dead (249-window test, 11% green). Walk-forward
across years, many independent windows, gradient-boosted brain, same honest
out-of-sample discipline. Zero Claude tokens.

    PYTHONPATH=. python scripts/swing_walkforward.py
"""
from __future__ import annotations
import numpy as np, pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from bots import marketdata

SYMBOLS=["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL",
         "US30","NAS100","US500"]
HORIZON=5         # 5 trading days ahead (~1 week swing)
COST=0.0005       # wider cost proxy for a swing hold (spread+slippage over days)
PROB=0.55
TRAIN_BARS=250    # ~1 trading year
TEST_BARS=60      # ~3 months per window

def features(df):
    c=df["close"]; f=pd.DataFrame(index=df.index)
    f["ret1"]=c.pct_change(); f["ret5"]=c.pct_change(5); f["ret20"]=c.pct_change(20)
    f["ma_f"]=c.rolling(10).mean()/c-1; f["ma_s"]=c.rolling(50).mean()/c-1
    d=c.diff(); up=d.clip(lower=0).rolling(14).mean(); dn=(-d.clip(upper=0)).rolling(14).mean()
    f["rsi"]=100-100/(1+up/dn.replace(0,np.nan))
    f["vol"]=c.pct_change().rolling(20).std()
    ef=c.ewm(span=12).mean(); es=c.ewm(span=26).mean(); f["mom"]=(ef-es)/c
    return f

def main():
    print(f"SWING walk-forward | YEARS of daily bars | {HORIZON}-day horizon | prob>{PROB}\n")
    grand_green=grand_tot=0
    for sym in SYMBOLS:
        try:
            df=marketdata.get_history(marketdata.resolve_symbol(sym),period="5y",interval="1d")
            df.columns=[c.lower() for c in df.columns]; df=df[~df.index.duplicated()]
        except Exception as e:
            print(f"  {sym}: no data ({e})"); continue
        X=features(df); y=(df["close"].shift(-HORIZON)>df["close"]).astype(int)
        data=X.join(y.rename("target")).dropna()
        px=df["close"].reindex(data.index).values
        cols=[c for c in data.columns if c!="target"]
        n=len(data)
        if n < TRAIN_BARS+TEST_BARS*2:
            print(f"  {sym}: only {n} daily bars, too little history"); continue
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
            start+=TEST_BARS
        if not wins:
            print(f"  {sym}: no windows"); continue
        g=sum(1 for w in wins if w>0); tot=len(wins); total=sum(wins)*100
        grand_green+=g; grand_tot+=tot
        verdict="CONSISTENT EDGE" if g>=0.6*tot and total>0 else ("mixed" if total>0 else "loses")
        yrs=(data.index[-1]-data.index[0]).days/365.0
        print(f"  {sym:7s}: {g}/{tot} windows green over {yrs:.1f}y | total {total:+6.2f}% | {verdict}")
    if grand_tot:
        print(f"\n  ACROSS ALL: {grand_green}/{grand_tot} windows green "
              f"({grand_green/grand_tot*100:.0f}%) -- 50%=coin flip")

if __name__=="__main__": main()
