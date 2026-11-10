// HTTP readiness only: not an MCP handshake or a first-reply assertion.
try {
  const endpoint = new URL(process.env.TRANSPORT_READINESS_URL);
  if (endpoint.username || endpoint.password || endpoint.hash ||
      (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' &&
        endpoint.hostname === 'localhost'))) throw new Error();
  const response = await fetch(endpoint, {method: 'GET', redirect: 'error',
    signal: AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error();
  await response.body?.cancel();
} catch {
  console.error('Transport readiness preflight failed.');
  process.exitCode = 1;
}
