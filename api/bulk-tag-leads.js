export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return res.status(500).json({ error: 'Supabase env vars not configured' });

  const { updates, selectAll, filter, search, tags } = req.body;

  if (selectAll) {
    if (!Array.isArray(tags) || !tags.length) return res.status(400).json({ error: 'No tags provided' });
    try {
      const r = await fetch(`${url}/rest/v1/rpc/append_tags_to_matching`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'apikey': key,
          'Authorization': `Bearer ${key}`,
        },
        body: JSON.stringify({
          p_tags:   tags,
          p_filter: filter || 'all',
          p_search: search || '',
        }),
      });
      const count = await r.json();
      return res.status(200).json({ ok: count, failed: 0, total: count });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  }

  if (!Array.isArray(updates) || !updates.length) return res.status(400).json({ error: 'No updates provided' });

  let ok = 0, failed = 0;
  for (const { id, tags: rowTags } of updates) {
    try {
      const r = await fetch(`${url}/rest/v1/clean_leads?id=eq.${id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'apikey': key,
          'Authorization': `Bearer ${key}`,
          'Prefer': 'return=minimal',
        },
        body: JSON.stringify({ tags: rowTags }),
      });
      r.ok ? ok++ : failed++;
    } catch { failed++; }
  }
  return res.status(200).json({ ok, failed });
}
