/// <reference lib="webworker" />

type LoadMessage = { type: 'load-json'; key: string; url: string };
const protocolCache = new Map<string, unknown>();
let entityCache: unknown;

async function decodeJson(response: Response): Promise<unknown> {
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const gzip = bytes[0] === 0x1f && bytes[1] === 0x8b;
  if (!gzip) return JSON.parse(new TextDecoder().decode(bytes));

  if ('DecompressionStream' in globalThis) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    return JSON.parse(await new Response(stream).text());
  }

  const { gunzipSync, strFromU8 } = await import('fflate');
  return JSON.parse(strFromU8(gunzipSync(bytes)));
}

self.addEventListener('message', async (event: MessageEvent<LoadMessage>) => {
  if (event.data?.type !== 'load-json') return;
  const { key, url } = event.data;
  try {
    if (key === 'entities' && entityCache !== undefined) {
      self.postMessage({ type: 'loaded', key, payload: entityCache });
      return;
    }
    if (key.startsWith('protocol:') && protocolCache.has(key)) {
      const payload = protocolCache.get(key);
      protocolCache.delete(key);
      protocolCache.set(key, payload);
      self.postMessage({ type: 'loaded', key, payload });
      return;
    }
    const response = await fetch(url, { cache: 'force-cache' });
    const payload = await decodeJson(response);
    if (key === 'entities') entityCache = payload;
    if (key.startsWith('protocol:')) {
      protocolCache.set(key, payload);
      while (protocolCache.size > 3) protocolCache.delete(protocolCache.keys().next().value as string);
    }
    self.postMessage({ type: 'loaded', key, payload });
  } catch (error) {
    self.postMessage({ type: 'error', key, message: error instanceof Error ? error.message : String(error) });
  }
});

export {};
