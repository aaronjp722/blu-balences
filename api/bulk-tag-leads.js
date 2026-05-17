export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return res.status(500).json({ error: 'Supabase env vars not configured' });

  const { updates } = req.body;
  if (!Array.isArray(updates) || !updates.length) return res.status(400).json({ error: 'No updates provided' });

  let ok = 0, failed = 0;
  for (const { id, tags } of updates) {
    try {
      const r = await fetch(`${url}/rest/v1/clean_leads?id=eq.${id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'apikey': key,
          'Authorization': `Bearer ${key}`,
          'Prefer': 'return=minimal',
        },
        body: JSON.stringify({ tags }),
      });
      r.ok ? ok++ : failed++;
    } catch { failed++; }
  }
  return res.status(200).json({ ok, failed });
}
