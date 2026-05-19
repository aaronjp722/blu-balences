export default async function handler(req, res) {
  const { query, near, cursor } = req.query;
  if (!query || !near) return res.status(400).json({ error: "query and near are required" });

  const params = new URLSearchParams({
    query,
    near,
    limit: "50",
    fields: "fsq_id,name,tel,website,location,categories,rating",
    ...(cursor ? { cursor } : {}),
  });

  const fsRes = await fetch(
    `https://places-api.foursquare.com/places/search?${params}`,
    {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${process.env.FOURSQUARE_API_KEY}`,
        "X-Places-Api-Version": "2025-06-17",
      },
    }
  );

  const data = await fsRes.json();
  res.status(fsRes.status).json(data);
}
