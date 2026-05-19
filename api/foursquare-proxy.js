const https = require("https");

module.exports = async function handler(req, res) {
  const { query, near, cursor } = req.query;
  if (!query || !near)
    return res.status(400).json({ error: "query and near are required" });

  const params = new URLSearchParams({
    query,
    near,
    limit: "50",
    fields: "fsq_id,name,tel,website,location,categories,rating",
    ...(cursor ? { cursor } : {}),
  });

  const apiKey = process.env.FOURSQUARE_API_KEY;
  const url = `https://places-api.foursquare.com/places/search?${params}`;

  try {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${apiKey}`,
        "X-Places-Api-Version": "2025-06-17",
      },
    });
    const data = await response.json();
    return res.status(response.status).json(data);
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
};
