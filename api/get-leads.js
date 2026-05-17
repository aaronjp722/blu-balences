export default async function handler(req, res) {
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method not allowed' });

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return res.status(500).json({ error: 'Supabase env vars not configured' });

  const page   = parseInt(req.query.page  || '0');
  const limit  = parseInt(req.query.limit || '50');
  const search = (req.query.search || '').trim();
  const filter = req.query.filter || 'all';
  const offset = page * limit;

  let query = `${url}/rest/v1/clean_leads?select=id,email,first_name,last_name,phone,tags,email_valid,smtp_valid,smtp_checked&order=id.desc&offset=${offset}&limit=${limit}`;
  if (search) query += `&email=ilike.*${encodeURIComponent(search)}*`;
  if (filter === 'valid')   query += '&email_valid=eq.true';
  if (filter === 'invalid') query += '&email_valid=eq.false';

  try {
    const r = await fetch(query, {
      headers: {
        'apikey': key,
        'Authorization': `Bearer ${key}`,
        'Range-Unit': 'items',
        'Range': `${offset}-${offset + limit - 1}`,
        'Prefer': 'count=exact',
      },
    });
    const data  = await r.json();
    const range = r.headers.get('content-range') || '';
    const total = parseInt(range.split('/')[1]) || 0;
    return res.status(200).json({ data, total });
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
}
