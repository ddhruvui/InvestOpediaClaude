// Vercel entry point. vercel.json rewrites every path here, and Express sees
// the original URL, so the /api/* routes in src/app.js match unchanged.
import app from '../src/app.js';

export default app;
