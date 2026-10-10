"""Next-session forecast model for the Nifty 50 (pure functions; no I/O).

What was tested (walk-forward, trained only on earlier years, tested 2015-2026, ~2,900 sessions):

  * SIZE of the move: a GJR-GARCH(1,1) with Student-t errors beats the old RiskMetrics EWMA
    (lambda 0.94) on a proper scoring rule, with ~8% narrower 70% bands and ~16% narrower
    95% bands at the same coverage. (VIX-implied vol, a regression on realised-vol measures,
    and conditioning on the overnight S&P move did not beat it.)
  * CENTRE and DIRECTION: only the S&P 500's last US session carries usable information.
    Adding Nasdaq, US VIX, Brent, USD/INR, the dollar index, India VIX or yesterday's return
    made it worse, so none are used. Technical indicators were tested earlier and also add
    nothing.
  * Bands come from the empirical quantiles of past standardised errors, not from a normal
    curve, because index returns have fat tails.

Everything here is deliberately small and dependency-light (numpy, pandas, arch).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# quantile levels used for the bands: 70% ("typical") and 95% ("wide")
Q70 = (0.15, 0.85)
Q95 = (0.025, 0.975)
RIDGE_ALPHA = 50.0
LOGIT_C = 0.2
PROB_QS = np.array([.05, .10, .15, .25, .35, .50, .65, .75, .85, .90, .95])


# ----------------------------------------------------------------------------- features
def spx_prior(nifty_index: pd.DatetimeIndex, spx_close: pd.Series) -> pd.Series:
    """For each Nifty date, the return of the last US session that closed strictly before it."""
    ret = spx_close.dropna().pct_change().dropna()
    pos = ret.index.searchsorted(nifty_index, side="left") - 1
    vals = np.where(pos >= 0, ret.values[np.clip(pos, 0, None)], np.nan)
    return pd.Series(vals, index=nifty_index)


# ----------------------------------------------------------------------------- volatility
def ewma_next_sigma(returns: pd.Series, lam: float = 0.94) -> float:
    """The old forecast, kept as a fallback and as the benchmark."""
    v = float(returns.iloc[:30].var())
    for x in returns.values:
        v = lam * v + (1 - lam) * float(x) ** 2
    return float(np.sqrt(v))


def fit_gjr(returns: pd.Series, last_obs: int | None = None):
    """Fit GJR-GARCH(1,1)-t on daily returns (decimal). Returns the arch result, or None on failure."""
    from arch import arch_model
    am = arch_model(returns * 100.0, mean="Zero", vol="GARCH", p=1, o=1, q=1, dist="t", rescale=False)
    res = am.fit(last_obs=last_obs, disp="off", show_warning=False)
    return res if res.convergence_flag == 0 else None


def gjr_next_sigma(res) -> float:
    """One-step-ahead sigma (decimal) from the last observation."""
    return float(np.sqrt(res.forecast(horizon=1).variance.values[-1, 0]) / 100.0)


def std_quantiles(z: np.ndarray, qs) -> np.ndarray:
    z = np.asarray(z, float)
    return np.quantile(z[np.isfinite(z)], list(qs))


# ----------------------------------------------------------------------------- the S&P signal
def fit_signal(x: np.ndarray, y_ret: np.ndarray) -> dict:
    """Ridge for the expected Nifty return and L2 logistic for P(up), both on the S&P move.

    x: prior-US-session S&P return; y_ret: the Nifty's next-session return. Standardised x,
    intercepts unpenalised (same objective as scikit-learn's Ridge(alpha=50) and
    LogisticRegression(C=0.2), which is what the backtest used).
    """
    ok = np.isfinite(x) & np.isfinite(y_ret)
    x, y = np.asarray(x)[ok], np.asarray(y_ret)[ok]
    mx, sx = float(x.mean()), float(x.std()) or 1.0
    xs = (x - mx) / sx
    ybar = float(y.mean())
    slope = float(np.sum(xs * (y - ybar)) / (np.sum(xs ** 2) + RIDGE_ALPHA))
    up = (y > 0).astype(float)
    b0, b1 = float(np.log(up.mean() / (1 - up.mean()))), 0.0
    for _ in range(50):                           # Newton's method on the penalised log-loss
        p = 1.0 / (1.0 + np.exp(-(b0 + b1 * xs)))
        g0, g1 = LOGIT_C * np.sum(p - up), b1 + LOGIT_C * np.sum((p - up) * xs)
        w = p * (1 - p)
        h00, h01, h11 = LOGIT_C * np.sum(w), LOGIT_C * np.sum(w * xs), 1.0 + LOGIT_C * np.sum(w * xs ** 2)
        det = h00 * h11 - h01 ** 2
        d0, d1 = (h11 * g0 - h01 * g1) / det, (-h01 * g0 + h00 * g1) / det
        b0, b1 = b0 - d0, b1 - d1
        if abs(d0) + abs(d1) < 1e-10:
            break
    return {"mx": mx, "sx": sx, "ybar": ybar, "ridge_slope": slope, "b0": float(b0), "b1": float(b1), "n": int(ok.sum())}


def fit_logit(x: np.ndarray, up: np.ndarray, C: float) -> dict:
    """L2 logistic regression of 'closed up' on one standardised feature (intercept unpenalised)."""
    x = np.asarray(x, float); up = np.asarray(up, float)
    mx, sx = float(x.mean()), float(x.std()) or 1.0
    xs = (x - mx) / sx
    b0, b1 = float(np.log(up.mean() / (1 - up.mean()))), 0.0
    for _ in range(50):
        p = 1.0 / (1.0 + np.exp(-(b0 + b1 * xs)))
        g0, g1 = C * np.sum(p - up), b1 + C * np.sum((p - up) * xs)
        w = p * (1 - p)
        h00, h01, h11 = C * np.sum(w), C * np.sum(w * xs), 1.0 + C * np.sum(w * xs ** 2)
        det = h00 * h11 - h01 ** 2
        d0, d1 = (h11 * g0 - h01 * g1) / det, (-h01 * g0 + h00 * g1) / det
        b0, b1 = b0 - d0, b1 - d1
        if abs(d0) + abs(d1) < 1e-10:
            break
    return {"mx": mx, "sx": sx, "b0": float(b0), "b1": float(b1)}


def logit_p(m: dict, x) -> np.ndarray:
    xs = (np.asarray(x, float) - m["mx"]) / m["sx"]
    return 1.0 / (1.0 + np.exp(-(m["b0"] + m["b1"] * xs)))


def fit_gap_call(gap_pct: np.ndarray, up: np.ndarray, noise_pct: float, draws: int = 5, seed: int = 7) -> dict:
    """P(Nifty closes up) from the opening gap (in %), trained on gaps blurred by `noise_pct` so that it
    expects GIFT Nifty's imperfect view of the true gap, not the gap itself."""
    rng = np.random.default_rng(seed)
    g = np.concatenate([gap_pct + rng.normal(0, noise_pct, len(gap_pct)) for _ in range(draws)])
    return fit_logit(g, np.tile(up, draws), C=1.0 * draws)


def call_tier(basis: str, p_up: float) -> str:
    """Confidence tier for a red/green call. Thresholds are on |P(up) - 0.5|."""
    d = abs(p_up - 0.5)
    if basis == "gift":
        return "very high" if d >= 0.25 else "good" if d >= 0.10 else "weak"
    if basis == "us":
        return "strong" if d >= 0.10 else "moderate" if d >= 0.05 else "weak"
    return "weak"


def predict_signal(m: dict, x) -> tuple[np.ndarray, np.ndarray]:
    xs = (np.asarray(x, float) - m["mx"]) / m["sx"]
    return m["ybar"] + m["ridge_slope"] * xs, 1.0 / (1.0 + np.exp(-(m["b0"] + m["b1"] * xs)))


def tier(p_up: float) -> str:
    """Wording tiers, tied to measured hit rates (see forecast_backtest.json)."""
    d = p_up - 0.5
    if d >= 0.10:
        return "leans up"
    if d <= -0.10:
        return "leans down"
    if d >= 0.05:
        return "slight tilt up"
    if d <= -0.05:
        return "slight tilt down"
    return "no clear lean"


# ----------------------------------------------------------------------------- walk-forward backtest
POOL_YEARS = 5      # calibrate the bands on the model's own out-of-sample errors of the last 5 years


def walk_forward(nifty_close: pd.Series, spx_close: pd.Series, first_test_year: int = 2015) -> pd.DataFrame:
    """Out-of-sample forecasts for every session from `first_test_year` on.

    For each calendar year Y the models are fitted on data strictly before Y (GJR parameters
    frozen at the end of Y-1, then updated daily with the realised returns; signal refit
    yearly), and used to forecast every session in Y. Returns one row per test session with the
    forecast sigma, centre, P(up) and the standardised error z = (r - mu) / sigma.
    """
    ret = nifty_close.pct_change().dropna()
    x_all = spx_prior(ret.index, spx_close)
    ew = pd.Series(np.sqrt((ret ** 2).ewm(alpha=0.06, adjust=False).mean().shift(1)).values, index=ret.index)
    rows = []
    for Y in range(first_test_year, int(ret.index[-1].year) + 1):
        tr = ret.index.year < Y
        te = ret.index.year == Y
        if te.sum() == 0:
            continue
        nobs = int(tr.sum())
        res = fit_gjr(ret, last_obs=nobs)
        if res is None:                       # a failed fit degrades that year to the EWMA, it never drops the year
            sig = ew.values
        else:
            sig = np.sqrt(res.forecast(start=nobs, horizon=1, reindex=True).variance.values[:, 0]) / 100.0
        sm = fit_signal(x_all.values[tr], ret.values[tr])
        mu, p = predict_signal(sm, x_all.values[te])
        for i, ix in enumerate(np.where(te)[0]):
            r = float(ret.iloc[ix])
            rows.append({"date": ret.index[ix], "year": Y, "r": r, "sigma_gjr": float(sig[ix]), "sigma_ewma": float(ew.iloc[ix]),
                         "mu": float(mu[i]), "p_up": float(p[i]), "spx": float(x_all.iloc[ix]),
                         "z": (r - float(mu[i])) / float(sig[ix]), "z_old": r / float(ew.iloc[ix])})
    cols = ["year", "r", "sigma_gjr", "sigma_ewma", "mu", "p_up", "spx", "z", "z_old"]
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(columns=cols)


def pool_quantiles(wf: pd.DataFrame, col: str, year: int, qs, years: int = POOL_YEARS) -> np.ndarray:
    """Quantiles of the standardised out-of-sample errors from the `years` years before `year`."""
    z = wf.loc[(wf["year"] < year) & (wf["year"] >= year - years), col].values.astype(float) if len(wf) else np.array([])
    z = z[np.isfinite(z)]
    if len(z) < 250:       # no usable history: normal quantiles, shrunk by the measured out-of-sample ratio (~0.85)
        from statistics import NormalDist
        return np.array([NormalDist().inv_cdf(q) * 0.85 for q in qs])
    return std_quantiles(z, qs)
