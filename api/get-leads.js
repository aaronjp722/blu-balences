export default async function handler(req, res) {
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method not allowed' });

  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return res.status(500).json({ error: 'Supabase env vars not configured' });

  const page   = parseInt(req.query.page  || '0');
  const limit  = parseInt(req.query.limit || '50');
  const search = (req.query.search || '').trim();
  const filter = req.query.filter || 'all';
  const tags   = req.query.tags   || '';
  const ids    = req.query.ids    || '';
  const offset = page * limit;

  let query = `${url}/rest/v1/clean_leads?select=id,email,first_name,last_name,phone,tags,email_valid,smtp_valid,smtp_checked&order=id.desc&offset=${offset}&limit=${limit}`;

  if (ids) {
    const idList = ids.split(',').map(n => parseInt(n)).filter(n => !isNaN(n));
    if (idList.length) query += `&id=in.(${idList.join(',')})`;
  } else {
    if (search) query += `&or=(email.ilike.*${encodeURIComponent(search)}*,first_name.ilike.*${encodeURIComponent(search)}*,last_name.ilike.*${encodeURIComponent(search)}*)`;
    if (filter === 'smtp_valid')     query += '&smtp_valid=eq.true&smtp_checked=eq.true';
    if (filter === 'smtp_invalid')   query += '&smtp_valid=eq.false&smtp_checked=eq.true';
    if (filter === 'smtp_unchecked') query += '&smtp_checked=eq.false';
    if (tags) {
      const tagList = tags.split(',').map(t => t.trim()).filter(Boolean);
      tagList.forEach(t => { query += `&tags=cs.{${encodeURIComponent(t)}}`; });
    }
  }

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
