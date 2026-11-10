// Package this file as check.mjs in the reviewed, digest-pinned check archive.
// The operator must choose an endpoint that actually authenticates the token.
try {
  const endpoint = new URL(process.env.INFERENCE_GATE_URL);
  if (endpoint.username || endpoint.password || endpoint.hash ||
      (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' &&
        endpoint.hostname === 'localhost'))) throw new Error();
  const token = process.env.INFERENCE_GATE_TOKEN;
  if (!token || /[\r\n]/.test(token)) throw new Error();
  const response = await fetch(endpoint, {method: 'GET', redirect: 'error',
    headers: {Authorization: `Bearer ${token}`}, signal: AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error();
  await response.body?.cancel();
} catch {
  console.error('Inference gate preflight failed.');
  process.exitCode = 1;
}
