// MongoDB connection — one client, cached on globalThis so a warm Vercel
// function reuses it across invocations instead of re-handshaking per request.
//
//   MONGO_URI       mongodb+srv://user:<db_password>@host/?appName=...
//   DB_PASSWORD     substituted for the <db_password> placeholder (URL-encoded)
//   MONGO_DB        database name            (default InvestOpediaClaude)
//   REPORT_BUNDLE   which published bundle    (default latest)
//
// Without MONGO_URI the app falls back to the local report files + a JSON
// paper book — that is the dev/test mode, never what runs on Vercel.
import { MongoClient } from 'mongodb';

export const DB_NAME = process.env.MONGO_DB || 'InvestOpediaClaude';
export const BUNDLE = process.env.REPORT_BUNDLE || 'latest';

export function mongoUri() {
  const raw = process.env.MONGO_URI;
  if (!raw) return null;
  if (!raw.includes('<db_password>')) return raw;
  const pw = process.env.DB_PASSWORD;
  if (pw == null) throw new Error('MONGO_URI has a <db_password> placeholder but DB_PASSWORD is not set');
  return raw.replace('<db_password>', encodeURIComponent(pw));
}

export const mongoEnabled = () => Boolean(process.env.MONGO_URI);

const KEY = '__investopedia_mongo_client';

export async function getClient() {
  if (!globalThis[KEY]) {
    const client = new MongoClient(mongoUri(), {
      serverSelectionTimeoutMS: 10_000,
      connectTimeoutMS: 10_000,
      maxPoolSize: 5,
    });
    globalThis[KEY] = client.connect().catch((err) => {
      delete globalThis[KEY];          // let the next request retry, not inherit the failure
      throw err;
    });
  }
  return globalThis[KEY];
}

export async function getDb() {
  return (await getClient()).db(DB_NAME);
}

export async function closeDb() {
  const p = globalThis[KEY];
  if (!p) return;
  delete globalThis[KEY];
  try { (await p).close(); } catch { /* already gone */ }
}
