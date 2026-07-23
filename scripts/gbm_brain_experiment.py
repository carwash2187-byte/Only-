"""Session 48 brick #4: does a DIFFERENT brain (gradient-boosted trees) find
a broad edge where the Q-agent couldn't?

Honest test of the mainstream quant workhorse (not the LSTM hype, which is a
known look-ahead illusion). Per symbol: engineer standard features, label
each bar by whether price is higher N bars ahead, train a GradientBoosting
classifier on TRAIN days, then on UNSEEN days trade its high-confidence
signals (prob>0.55) with the same min-hold-30 exit. Strict out-of-sample.
If it makes MANY symbols green, it's a real edge; if not, honest negative.

    PYTHONPATH=. python scripts/gbm_brain_experiment.py
"""
from __future__ import annotations
import numpy as np, pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from bots import marketdata

SYMBOLS=["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL"]
HORIZON=30    # predict direction 30 bars (min) ahead -- matches the 30-min hold
COST=0.0002
PROB=0.55     # only act on confident signals

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
    print(f"gradient-boosted directional brain | horizon {HORIZON}m | prob>{PROB} | unseen days\n")
    green=0
    for sym in SYMBOLS:
        try:
            df=marketdata.get_history(marketdata.resolve_symbol(sym),period="8d",interval="1m")
            df.columns=[c.lower() for c in df.columns]
        except Exception: 
            print(f"  {sym}: no data"); continue
        df=df[~df.index.duplicated()]
        X=features(df); y=(df["close"].shift(-HORIZON)>df["close"]).astype(int)
        data=X.join(y.rename("target")).dropna()
        days=sorted({d.strftime("%Y-%m-%d") for d in data.index.normalize()})
        if len(days)<4: 
            print(f"  {sym}: too few days"); continue
        split=int(len(days)*0.6); train_d=set(days[:split]); test_d=set(days[split:])
        tr=data[data.index.normalize().isin([pd.Timestamp(d,tz=data.index.tz) for d in train_d])]
        te=data[data.index.normalize().isin([pd.Timestamp(d,tz=data.index.tz) for d in test_d])]
        if len(tr)<200 or len(te)<100:
            print(f"  {sym}: insufficient rows"); continue
        cols=[c for c in data.columns if c!="target"]
        clf=GradientBoostingClassifier(n_estimators=60,max_depth=3,learning_rate=0.05)
        clf.fit(tr[cols], tr["target"])
        # trade the test set: enter long when prob(up)>PROB, hold HORIZON bars
        proba=clf.predict_proba(te[cols])[:,1]
        prices=te["close"] if "close" in te else None
        px=df["close"].reindex(te.index).values
        net=0.0; trades=0; i=0
        while i < len(te)-HORIZON:
            if proba[i]>PROB:
                r=(px[i+HORIZON]-px[i])/px[i]-COST
                net+=r; trades+=1; i+=HORIZON
            else:
                i+=1
        acc=( (proba>0.5).astype(int)==te["target"].values ).mean()
        flag="＋" if net>0 else "－"
        if net>0: green+=1
        print(f"  {flag} {sym:7s} net {net*100:+6.2f}%  ({trades} trades, dir-acc {acc*100:.1f}%)")
    print(f"\n  {green}/{len(SYMBOLS)} symbols profitable out-of-sample "
          f"({'BROAD EDGE -- worth integrating' if green>=5 else 'NOT broad -- honest negative'})")

if __name__=="__main__": main()
