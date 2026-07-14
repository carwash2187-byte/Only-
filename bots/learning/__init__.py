"""Reinforcement-learning trading agent.

The core agent (bots.learning.agent.QTraderAgent) is dependency-light
(numpy/pandas only) and learns by trial and error: every losing action lowers
the value of taking that action again in the same market state, every winning
action raises it. For a production-grade version of the same idea, see the
freqtrade + FreqAI setup in bots/learning/freqai/.
"""

from bots.learning.agent import QTraderAgent, extract_state

__all__ = ["QTraderAgent", "extract_state"]
