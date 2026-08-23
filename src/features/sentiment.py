"""M4 — F9 NLP/sentiment block (§F). GPU-friendly; Stage 2.

Per headline: FinBERT -> s = p_pos - p_neg in [-1, 1]. Per (ticker, day):
sent_mean, sent_ewm3 (3-day EWMA [IMPL]), news_count = log1p(n), has_news.
Headlines after the close roll to the NEXT session [MUST — EOD cutoff].
Sentiment is a FEATURE, never a standalone strategy (Lopez-Lira caveat).

Weights come from the PINNED local snapshot (data_finbert/, fetch_finbert.py) —
never from a moving HuggingFace `main` (G-10 reproducibility).
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

CLOSE_UTC_HOUR = 21   # 16:00 ET ~= 21:00 UTC (20:00 in DST; conservative later bound [IMPL])


def _load_headlines(news_dir: str | Path, tickers: set[str]) -> pd.DataFrame:
    rows = []
    for p in glob.glob(str(Path(news_dir) / "*.json")):
        t = Path(p).stem
        if t.startswith("_") or t not in tickers:
            continue
        try:
            arts = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        for a in arts:
            title = a.get("title")
            ts = a.get("date")
            if title and ts:
                rows.append({"ticker": t, "ts": ts, "title": title})
    df = pd.DataFrame(rows)
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce", format="ISO8601")
        df = df.dropna(subset=["ts"])
    return df


def resolve_snapshot(finbert_dir: str | Path) -> Path:
    """fetch_finbert.py stores files under a pinned-revision subdirectory."""
    d = Path(finbert_dir)
    if (d / "config.json").exists():
        return d
    subs = [p for p in d.iterdir() if p.is_dir() and (p / "config.json").exists()]         if d.exists() else []
    if not subs:
        raise FileNotFoundError(f"no FinBERT snapshot under {d}")
    return sorted(subs)[0]


def score_headlines(titles: list[str], finbert_dir: str | Path, device: str | None = None,
                    batch_size: int = 64) -> np.ndarray:
    """s = p_pos - p_neg per title, via the pinned local FinBERT snapshot."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    finbert_dir = resolve_snapshot(finbert_dir)
    tok = AutoTokenizer.from_pretrained(str(finbert_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(finbert_dir)) \
        .to(device).eval()
    id2label = {int(k): v.lower() for k, v in model.config.id2label.items()}
    i_pos = next(k for k, v in id2label.items() if "pos" in v)
    i_neg = next(k for k, v in id2label.items() if "neg" in v)
    out = np.empty(len(titles), dtype=np.float32)
    with torch.no_grad():
        for s in range(0, len(titles), batch_size):
            batch = titles[s:s + batch_size]
            enc = tok(batch, return_tensors="pt", truncation=True, max_length=64,
                      padding=True).to(device)
            p = torch.softmax(model(**enc).logits, dim=1).cpu().numpy()
            out[s:s + len(batch)] = p[:, i_pos] - p[:, i_neg]
    return out


def sentiment_features(news_dir: str | Path, finbert_dir: str | Path,
                       dates: pd.DatetimeIndex, tickers: pd.Index,
                       device: str | None = None,
                       cache_path: str | Path | None = None) -> dict[str, pd.DataFrame]:
    nanf = lambda: pd.DataFrame(np.nan, index=dates, columns=tickers)
    df = _load_headlines(news_dir, set(tickers))
    if df.empty:
        return {"sent_mean": nanf(), "sent_ewm3": nanf(),
                "news_count": nanf(), "has_news": nanf()}
    cache = Path(cache_path) if cache_path else None
    if cache and cache.exists():
        scored = pd.read_parquet(cache)
        df = df.merge(scored, on=["ticker", "ts", "title"], how="left")
        todo = df["s"].isna()
    else:
        df["s"] = np.nan
        todo = pd.Series(True, index=df.index)
    if todo.any():
        df.loc[todo, "s"] = score_headlines(df.loc[todo, "title"].tolist(), finbert_dir,
                                            device)
        if cache:
            df[["ticker", "ts", "title", "s"]].to_parquet(cache, index=False)

    # EOD cutoff: after-close headlines belong to the NEXT session
    eff = df["ts"].dt.tz_convert(None)
    after_close = eff.dt.hour >= CLOSE_UTC_HOUR
    eff_date = eff.dt.normalize() + pd.to_timedelta(after_close.astype(int), unit="D")
    pos = np.searchsorted(dates.values, eff_date.values, side="left")
    pos = np.clip(pos, 0, len(dates) - 1)
    df["session"] = dates.values[pos]

    g = df.groupby(["session", "ticker"])["s"]
    mean = g.mean().unstack("ticker").reindex(index=dates, columns=tickers)
    cnt = g.size().unstack("ticker").reindex(index=dates, columns=tickers)
    return {
        "sent_mean": mean,
        "sent_ewm3": mean.ewm(span=3, adjust=False, min_periods=1).mean()
                         .where(cnt.notna().rolling(10, min_periods=1).max() > 0),
        "news_count": np.log1p(cnt),
        "has_news": cnt.notna().astype(float),
    }
