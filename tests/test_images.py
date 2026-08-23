"""T-13 — image golden: fixture OHLCV -> byte-identical renders (15x32/60x64/180x96,
bottom-fifth volume, 3 px/day)."""
import hashlib
import numpy as np

from src.models.cnn.render import render_image, SIZES

# Golden SHA-256 of the canonical fixture renders. Any renderer change that alters
# a single pixel changes these hashes and MUST be a deliberate, logged decision.
GOLDEN = {
    5: "0096505eef007c0a11efb326d7355f3875f9c5f6b238d0770b60f03086864253",
    20: "94cd0d6853bff0ad8ca6bb42d97a1b50c6430c69fc028196ce40d2efe0659898",
    60: "bebc35aab05e58c8a2df1deab28b423f621e0eb1c90124a3c24bcb00f6987b0d",
}


def _fixture(days, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, days)))
    op = close * (1 + rng.normal(0, 0.005, days))
    hi = np.maximum(close, op) * 1.01
    lo = np.minimum(close, op) * 0.99
    vol = rng.integers(1, 100, days).astype(float)
    ma = np.convolve(close, np.ones(days) / days, mode="full")[:days]
    return op, hi, lo, close, vol, ma


def test_t13_golden_images():
    for days, (W, H) in SIZES.items():
        img = render_image(*_fixture(days))
        assert img.shape == (H, W)
        assert img.dtype == np.uint8 and set(np.unique(img)) <= {0, 255}
        vol_h = H // 5
        assert img[H - vol_h:].any(), "volume area empty"
        assert not img[H - vol_h - 1].any(), "separator row must stay black"
        h = hashlib.sha256(img.tobytes()).hexdigest()
        assert h == GOLDEN[days], f"{days}d render drifted: {h}"


def test_t13_missing_hl_renders():
    op, hi, lo, close, vol, ma = _fixture(5)
    hi[2] = np.nan
    lo[2] = np.nan
    img = render_image(op, hi, lo, close, vol, ma)
    assert img[:, 6:9].any(), "day with missing H/L must still draw open/close/MA"
