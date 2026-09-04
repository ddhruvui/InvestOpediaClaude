# InvestOpediaClaude — frontend

React + Vite console over the backend API: Today (broker ticket), Dashboard
(G-11 verdict, equity curve), Suggestions, Backtest explorer, Paper trading.

This directory is `app/frontend/` of the main repo and is mirrored to its own
GitHub repo with `git subtree` (`scripts/publish_repos.sh` there). Edit it in the
main repo.

## Configuration

One variable, read at **build** time:

| var | meaning |
|---|---|
| `VITE_API_BASE` | backend origin, e.g. `https://investopediaclaude-be.vercel.app`. Unset = same origin (dev proxy / backend-served build). |

## Run locally

```sh
npm install
npm run dev            # http://localhost:5173, /api proxied to localhost:8787
# or build once and let the backend serve it from ../backend:
npm run build
```

## Deploy to Render

1. New → **Static Site** from the `InvestOpediaClaudeFE` repo (or New → Blueprint,
   which reads `render.yaml`).
2. Build command `npm ci && npm run build`, publish directory `dist`.
3. Environment variable `VITE_API_BASE` = the Vercel backend URL (no trailing slash).
4. Add a rewrite rule `/*` → `/index.html` (the blueprint already has it).
5. Deploy. Then put this site's origin into the backend's `CORS_ORIGIN`.

Changing `VITE_API_BASE` needs a redeploy — it is compiled into the bundle.
