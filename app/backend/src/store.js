// Paper-book persistence. Production keeps the book in MongoDB (collection
// `paper_books`, one document per report bundle); without MONGO_URI it is a
// single JSON file written atomically — the dev/test mode.
import fs from 'node:fs';
import path from 'node:path';
import { getDb, mongoEnabled, BUNDLE, DB_NAME } from './db.js';

// A FACTORY, never a shared literal: `{...TEMPLATE}` is a shallow copy, so a
// literal would hand every caller the SAME positions array — push() would then
// mutate the template itself and reset() would write the stale positions back.
export const emptyBook = () => ({
  positions: [], nav: 100000, account_equity: null,
  created_utc: null, updated_utc: null, seq: 0,
});

const DATA_DIR = process.env.PAPER_DATA_DIR || path.resolve(process.cwd(), 'data');
const FILE = path.join(DATA_DIR, 'paper_book.json');

export const bookLocation = () => (mongoEnabled()
  ? `MongoDB (${DB_NAME}.paper_books/${BUNDLE})` : FILE);

export async function loadBook() {
  if (mongoEnabled()) {
    const db = await getDb();
    const doc = await db.collection('paper_books').findOne({ _id: BUNDLE });
    if (doc) {
      const { _id, ...rest } = doc;
      return { ...emptyBook(), ...rest };
    }
    return { ...emptyBook(), created_utc: new Date().toISOString() };
  }
  try {
    return { ...emptyBook(), ...JSON.parse(fs.readFileSync(FILE, 'utf8')) };
  } catch {
    return { ...emptyBook(), created_utc: new Date().toISOString() };
  }
}

export async function saveBook(state) {
  state.updated_utc = new Date().toISOString();
  if (mongoEnabled()) {
    const db = await getDb();
    const { _id, ...doc } = state;
    await db.collection('paper_books')
      .replaceOne({ _id: BUNDLE }, { ...doc, bundle: BUNDLE }, { upsert: true });
    return state;
  }
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const tmp = `${FILE}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 1));
  fs.renameSync(tmp, FILE);          // atomic
  return state;
}
