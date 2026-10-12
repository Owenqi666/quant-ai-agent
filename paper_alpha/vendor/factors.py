import numpy as np
from .operators import (delay, ts_delta, ts_sum, ts_mean, ts_std, ts_min, ts_max,
    ts_rank, ts_corr, ts_cov, rank, sign, log, abs, where)

# every factor takes the same panel set (open_, high, low, close, volume, returns) and returns a
# dates x tickers panel; unused args are kept so main.py / ic.py can compute the whole book in one loop.
# sign convention throughout: higher factor value = long leg, so reversal/lottery signals carry -1.
# doc shorthand -> operator: delta=ts_delta, stddev=ts_std, sum=ts_sum, mean=ts_mean, corr=ts_corr, cov=ts_cov

# --- reversal cluster (Alpha101) ---

def alpha004(open_, high, low, close, volume, returns):
    return -1 * ts_rank(rank(low), 9)

def alpha012(open_, high, low, close, volume, returns):
    return sign(ts_delta(volume, 1)) * (-1 * ts_delta(close, 1))

def alpha033(open_, high, low, close, volume, returns):
    return rank(-1 * (1 - open_ / close))

def alpha023(open_, high, low, close, volume, returns):
    cond = ts_mean(high, 20) < high
    return where(cond, -1 * ts_delta(high, 2), 0.0)

# --- momentum cluster (Alpha101) ---

def alpha101(open_, high, low, close, volume, returns):
    return (close - open_) / (high - low + 0.001)

def alpha019(open_, high, low, close, volume, returns):
    return -sign((close - delay(close, 7)) + ts_delta(close, 7)) * (1 + rank(1 + ts_sum(returns, 250)))

def alpha009(open_, high, low, close, volume, returns):
    dc = ts_delta(close, 1)
    inner = where(ts_max(dc, 5) < 0, dc, -dc)
    return where(ts_min(dc, 5) > 0, dc, inner)

def alpha046(open_, high, low, close, volume, returns):
    x = (delay(close, 20) - delay(close, 10)) / 10 - (delay(close, 10) - close) / 10
    inner = where(x < 0, 1.0, -ts_delta(close, 1))
    return where(x > 0.25, -1.0, inner)

# --- price-volume divergence cluster (Alpha101) ---

def alpha002(open_, high, low, close, volume, returns):
    return -1 * ts_corr(rank(ts_delta(log(volume), 2)), rank((close - open_) / open_), 6)

def alpha006(open_, high, low, close, volume, returns):
    return -1 * ts_corr(open_, volume, 10)

def alpha013(open_, high, low, close, volume, returns):
    return -1 * rank(ts_cov(rank(close), rank(volume), 5))

def alpha044(open_, high, low, close, volume, returns):
    return -1 * ts_corr(high, rank(volume), 5)

# --- volatility cluster (Alpha101) ---

def alpha022(open_, high, low, close, volume, returns):
    return -1 * ts_delta(ts_corr(high, volume, 5), 5) * rank(ts_std(close, 20))

def alpha040(open_, high, low, close, volume, returns):
    return -1 * rank(ts_std(high, 10)) * ts_corr(high, volume, 10)

def alpha034(open_, high, low, close, volume, returns):
    return rank((1 - rank(ts_std(returns, 2) / ts_std(returns, 5))) + (1 - rank(ts_delta(close, 1))))

def alpha018(open_, high, low, close, volume, returns):
    return -1 * rank(ts_std(abs(close - open_), 5) + (close - open_) + ts_corr(close, open_, 10))

# --- self-added (the interview battleground; each occupies a mechanism the 16 above do not) ---

def amihud(open_, high, low, close, volume, returns):
    # liquidity premium: price impact per dollar traded; illiquid -> demands higher future return (long)
    illiq = abs(returns) / (close * volume)
    # x1e6 is the conventional Amihud readability scaling; rank-invariant so IC is unaffected
    return ts_mean(illiq, 21) * 1e6

def high52w(open_, high, low, close, volume, returns):
    # anchoring: closeness to 52-week high; underreaction near the anchor -> continues up (long)
    return close / ts_max(high, 252)

def max1(open_, high, low, close, volume, returns):
    # lottery preference: biggest single-day pop last month; overbought -> low future return, so -1
    return -1 * ts_max(returns, 21)

def _top5_mean(w):
    return np.sort(w)[-5:].mean()

def max5(open_, high, low, close, volume, returns):
    # lottery preference, smoother variant: mean of top-5 daily returns last month; -1 same logic as max1
    return -1 * returns.rolling(21).apply(_top5_mean, raw=True)

def on_in_spread(open_, high, low, close, volume, returns):
    # clientele tug-of-war: overnight return persists, intraday reverses; the spread is orthogonal to
    # close-to-close momentum because subtracting the two cancels the common total-return component
    overnight = open_ / delay(close, 1) - 1
    intraday = close / open_ - 1
    return ts_mean(overnight - intraday, 21)

# explicit registry (not introspection -> _top5_mean stays out); 16 Alpha101 + 5 self-added = 21
factors = {
    "alpha004": alpha004, "alpha012": alpha012, "alpha033": alpha033, "alpha023": alpha023,
    "alpha101": alpha101, "alpha019": alpha019, "alpha009": alpha009, "alpha046": alpha046,
    "alpha002": alpha002, "alpha006": alpha006, "alpha013": alpha013, "alpha044": alpha044,
    "alpha022": alpha022, "alpha040": alpha040, "alpha034": alpha034, "alpha018": alpha018,
    "amihud": amihud, "high52w": high52w, "max1": max1, "max5": max5, "on_in_spread": on_in_spread,
}
