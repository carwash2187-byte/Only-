"""Freqtrade + FreqAI strategy: a model that keeps retraining itself.

FreqAI retrains the underlying ML model every `live_retrain_hours` on the most
recent data, so predictions adapt as the market changes -- the production-grade
version of "learning from mistakes".

Setup (see scripts/install_trading_stack.sh):
    pip install freqtrade[freqai]
    cp bots/learning/freqai/AdaptiveLearnerStrategy.py user_data/strategies/
    freqtrade trade --config bots/learning/freqai/config-freqai.json \
        --strategy AdaptiveLearnerStrategy \
        --freqaimodel LightGBMRegressor

Runs in dry-run (fake money) mode by default via the bundled config.
"""

import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class AdaptiveLearnerStrategy(IStrategy):
    minimal_roi = {"0": 0.05}
    stoploss = -0.04
    timeframe = "5m"
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 40

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, **kwargs) -> DataFrame:
        dataframe["%-rsi"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-sma"] = ta.SMA(dataframe, timeperiod=period)
        dataframe["%-relative_volume"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, **kwargs) -> DataFrame:
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, **kwargs) -> DataFrame:
        # Predict the mean close change `label_period_candles` ahead.
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-target"] = (
            dataframe["close"].shift(-label_period).rolling(label_period).mean()
            / dataframe["close"]
            - 1
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        enter_long = (dataframe["do_predict"] == 1) & (dataframe["&-target"] > 0.005)
        dataframe.loc[enter_long, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = (dataframe["do_predict"] == 1) & (dataframe["&-target"] < -0.002)
        dataframe.loc[exit_long, "exit_long"] = 1
        return dataframe
