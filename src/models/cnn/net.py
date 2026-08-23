"""M8.2 — VGG-style CNN (§C, all values fixed).

Blocks: conv(5x3) -> batch-norm -> LeakyReLU(0.01) -> 2x1 max-pool; 2/3/4 blocks
for 5/20/60-day images. Filters 64 doubling per block (64->128->256->512 — the
figure caption's 32 is wrong, main text says 64). FC head, 50% dropout, 2-class
softmax + cross-entropy. Adam 1e-5, batch 128, Xavier init, early-stop patience 2,
forecasts averaged over 5 retrainings.
"""
from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn

N_BLOCKS = {5: 2, 20: 3, 60: 4}


class JKXCNN(nn.Module):
    def __init__(self, image_days: int = 5, base_filters: int = 64,
                 dropout_fc: float = 0.5):
        super().__init__()
        from src.models.cnn.render import SIZES
        W, H = SIZES[image_days]
        blocks, ch = [], 1
        f = base_filters
        for b in range(N_BLOCKS[image_days]):
            blocks += [nn.Conv2d(ch, f, kernel_size=(5, 3), padding=(2, 1)),
                       nn.BatchNorm2d(f), nn.LeakyReLU(0.01),
                       nn.MaxPool2d(kernel_size=(2, 1))]
            ch, f = f, f * 2
        self.features = nn.Sequential(*blocks)
        with torch.no_grad():
            flat = self.features(torch.zeros(1, 1, H, W)).numel()
        self.head = nn.Sequential(nn.Flatten(), nn.Dropout(dropout_fc),
                                  nn.Linear(flat, 2))
        self.apply(self._init)

    @staticmethod
    def _init(m):
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.zeros_(m.bias)

    def forward(self, x):                            # x: (B, 1, H, W) in [0,1]
        return self.head(self.features(x))


def train_cnn(images_train: dict, images_valid: dict, up_label: "pd.DataFrame",
              image_days: int, cfg, seed: int, device: str | None = None,
              max_epochs: int = 50):
    """5-retraining average (§C): trains n_retrainings_avg models, returns the list.
    up_label: wide bool frame 1[fwd_20 > 0] (G-02 lag baked into fwd)."""
    import pandas as pd
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    batch = int(cfg.cnn.batch_size)

    def _flat(images):
        X, y = [], []
        for d, (imgs, names) in images.items():
            if d not in up_label.index:
                continue
            lab = up_label.loc[d].reindex(names).to_numpy(dtype=float)
            ok = np.isfinite(lab)
            X.append(imgs[ok])
            y.append(lab[ok].astype(np.int64))
        if not X:
            return None, None
        return np.concatenate(X), np.concatenate(y)

    Xtr, ytr = _flat(images_train)
    Xva, yva = _flat(images_valid)
    models = []
    for k in range(int(cfg.cnn.n_retrainings_avg)):
        torch.manual_seed(seed + 1000 + k)
        model = JKXCNN(image_days, int(cfg.cnn.base_filters),
                       float(cfg.cnn.dropout_fc)).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=float(cfg.cnn.lr))
        best_loss, best_state, patience = np.inf, None, 0
        n = len(Xtr)
        rng = np.random.default_rng(seed + k)
        for epoch in range(max_epochs):
            model.train()
            order = rng.permutation(n)
            for s in range(0, n, batch):
                bidx = order[s:s + batch]
                xb = torch.from_numpy(Xtr[bidx]).float().div_(255.0).unsqueeze(1).to(device)
                yb = torch.from_numpy(ytr[bidx]).to(device)
                opt.zero_grad()
                loss = nn.functional.cross_entropy(model(xb), yb)
                loss.backward()
                opt.step()
            model.eval()
            with torch.no_grad():
                vl = 0.0
                for s in range(0, len(Xva), batch):
                    xb = torch.from_numpy(Xva[s:s + batch]).float().div_(255.0) \
                        .unsqueeze(1).to(device)
                    yb = torch.from_numpy(yva[s:s + batch]).to(device)
                    vl += float(nn.functional.cross_entropy(model(xb), yb,
                                                            reduction="sum"))
                vl /= max(1, len(Xva))
            if vl < best_loss - 1e-5:
                best_loss, patience = vl, 0
                best_state = {k2: v.cpu().clone() for k2, v in model.state_dict().items()}
            else:
                patience += 1
                if patience >= int(cfg.cnn.early_stop_patience):
                    break
        if best_state:
            model.load_state_dict(best_state)
        model.eval()
        models.append(model)
    return models


def predict_cnn(models, images: dict, tickers, device: str | None = None):
    """P(up) averaged over the retraining ensemble -> wide frame."""
    import pandas as pd
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    rows = {}
    with torch.no_grad():
        for d, (imgs, names) in images.items():
            xb = torch.from_numpy(imgs).float().div_(255.0).unsqueeze(1).to(device)
            ps = [torch.softmax(m(xb), dim=1)[:, 1].cpu().numpy() for m in models]
            rows[d] = pd.Series(np.mean(ps, axis=0), index=names)
    return pd.DataFrame(rows).T.reindex(columns=tickers)
