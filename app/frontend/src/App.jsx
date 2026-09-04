import { useEffect, useState } from 'react';
import { api } from './api.js';
import Today from './pages/Today.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Suggestions from './pages/Suggestions.jsx';
import Backtest from './pages/Backtest.jsx';
import Paper from './pages/Paper.jsx';

const PAGES = [
  ['today', 'Today', Today],
  ['dashboard', 'Dashboard', Dashboard],
  ['suggestions', 'Suggestions', Suggestions],
  ['backtest', 'Backtest', Backtest],
  ['paper', 'Paper trading', Paper],
];

export default function App() {
  const [page, setPage] = useState(() => window.location.hash.slice(1) || 'today');
  const [health, setHealth] = useState(null);
  const [theme, setTheme] = useState(
    () => localStorage.getItem('theme') || 'auto');

  useEffect(() => {
    const onHash = () => setPage(window.location.hash.slice(1) || 'today');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => { api.health().then(setHealth).catch(() => setHealth(null)); }, []);

  useEffect(() => {
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  const Active = (PAGES.find(([id]) => id === page) || PAGES[0])[2];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          EOD Swing &amp; Position Trading
          <small>
            research console
            {health?.generated_utc
              ? ` · report built ${health.generated_utc.replace('T', ' ').replace('+00:00', 'Z')}`
              : health ? ' · report bundle not published' : ' · API unreachable'}
            {health?.published_utc
              && ` · published ${health.published_utc.replace('T', ' ').replace('+00:00', 'Z')}`}
          </small>
        </div>
        <nav className="nav">
          {PAGES.map(([id, label]) => (
            <button key={id} onClick={() => { window.location.hash = id; }}
                    aria-current={page === id ? 'page' : undefined}>
              {label}
            </button>
          ))}
        </nav>
        <div className="spacer" />
        <button className="icon-btn" title="Toggle colour theme"
                onClick={() => setTheme(
                  theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto')}>
          {theme === 'auto' ? '◐ Auto' : theme === 'light' ? '☀ Light' : '☾ Dark'}
        </button>
      </header>
      <main><Active /></main>
    </div>
  );
}
