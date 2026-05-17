// Wrapper fetch centralisé. À utiliser par les nouveaux call-sites ; les
// modules legacy (tabs/*) continuent d'appeler fetch() directement pour ne
// pas casser le comportement d'erreur granulaire.
//
// Politique :
//   - credentials: 'same-origin' (cookie Flask)
//   - 401 → redirect /login (côté Flask, qui re-bounce sur Keycloak)
//   - 5xx → throw avec body si JSON, sinon HTTP status
//   - 2xx → JSON.parse(body) si content-type JSON, sinon body brut

export async function apiFetch(url, opts) {
  const finalOpts = Object.assign({ credentials: 'same-origin' }, opts || {});
  let resp;
  try {
    resp = await fetch(url, finalOpts);
  } catch (e) {
    throw new Error(`Réseau indisponible : ${e.message || e}`);
  }
  if (resp.status === 401) {
    // Délègue à Flask qui sait re-bounce sur Keycloak avec la bonne URL retour.
    window.location.href = '/login';
    throw new Error('not_authenticated');
  }
  const ct = (resp.headers.get('Content-Type') || '').toLowerCase();
  const isJson = ct.includes('application/json');
  let body = null;
  try {
    body = isJson ? await resp.json() : await resp.text();
  } catch (e) {
    body = null;
  }
  if (!resp.ok) {
    const msg = (body && typeof body === 'object' && body.error) || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

window.apiFetch = apiFetch;
