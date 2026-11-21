const REPO = "milkmann/gpv-zoe-zaporizhzhia";
const WORKFLOW = "update.yml";

async function dispatch(env) {
  if (!env.GH_TOKEN) return { ok: false, status: 0, error: "GH_TOKEN secret missing" };
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "zoe-gpv-dispatch-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  return { ok: r.status === 204, status: r.status, error: r.status === 204 ? null : (await r.text()).slice(0, 300) };
}

export default {
  async scheduled(event, env, ctx) {
    const res = await dispatch(env);
    console.log(JSON.stringify(res));
    if (!res.ok) throw new Error(`dispatch failed: ${res.status} ${res.error}`);
  },
  async fetch(request, env) {
    return Response.json({
      worker: "zoe-gpv-dispatch",
      repo: REPO,
      cron: "*/10 * * * *",
      token_configured: Boolean(env.GH_TOKEN),
    });
  },
};
