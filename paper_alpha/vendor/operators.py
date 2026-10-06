import numpy as np
import pandas as pd

# all operators take dates x tickers panels and return the same shape; rolling is trailing
# (window ends at the current row, includes today and the prior d-1 days) so there is no look-ahead

# --- time-series: mechanical reductions, trust pandas (correct + fast + as clear as a hand-written loop) ---

def delay(x, d):
    return x.shift(d)

def ts_delta(x, d):
    return x - x.shift(d)

def ts_sum(x, d):
    return x.rolling(d).sum()

def ts_mean(x, d):
    return x.rolling(d).mean()

def ts_std(x, d):
    # pandas rolling std is sample std (ddof=1)
    return x.rolling(d).std()

def ts_min(x, d):
    return x.rolling(d).min()

def ts_max(x, d):
    return x.rolling(d).max()

# --- time-series: design choices made explicit (the §12 teaching points) ---

def ts_argmax(x, d):
    # day within the window where the max occurred; convention 0 = oldest day, d-1 = today
    return x.rolling(d).apply(np.argmax, raw=True)

def ts_rank(x, d):
    # time-series percentile rank of today's value within the trailing window, in (0,1];
    # ties use average method, 1.0 = today is the window max
    def rank_last(w):
        return pd.Series(w).rank(pct=True).iloc[-1]
    return x.rolling(d).apply(rank_last, raw=True)

def ts_corr(x, y, d):
    # rolling Pearson corr via population moments -- sidesteps the pairwise-alignment ambiguity
    # of DataFrame.rolling().corr(other); column alignment is then automatic
    mx = x.rolling(d).mean()
    my = y.rolling(d).mean()
    cov = (x * y).rolling(d).mean() - mx * my
    vx = (x * x).rolling(d).mean() - mx * mx
    vy = (y * y).rolling(d).mean() - my * my
    return cov / np.sqrt((vx * vy).clip(lower=0))

def ts_cov(x, y, d):
    # population covariance via moments, same alignment reasoning as ts_corr
    return (x * y).rolling(d).mean() - x.rolling(d).mean() * y.rolling(d).mean()

def decay_linear(x, d):
    # linear-weighted moving average; weight d for today down to 1 for d-1 days ago, weights sum to 1
    weights = np.arange(d, 0, -1)
    num = sum(weights[k] * x.shift(k) for k in range(d))
    return num / weights.sum()

# --- cross-sectional: rank/scale across tickers each day (axis=1) ---

def rank(x):
    # cross-sectional percentile rank per day, in (0,1], average method for ties
    return x.rank(axis=1, pct=True)

def scale(x, a=1):
    # rescale each day's cross-section so sum of abs values equals a
    return x.mul(a / x.abs().sum(axis=1), axis=0)

# --- element-wise ---

def sign(x):
    return np.sign(x)

def log(x):
    return np.log(x)

def abs(x):
    return x.abs()

def signedpower(x, a):
    return np.sign(x) * x.abs() ** a

def where(condition, a, b):
    # vectorized ternary for the conditional factors (#9/#23/#46); branches may be scalars or
    # panels, and any panel branch must share condition's index/columns (true by construction here)
    return pd.DataFrame(np.where(condition, a, b), index=condition.index, columns=condition.columns)
