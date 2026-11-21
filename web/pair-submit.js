// 업무 화면과 저장소에 접근할 수 없는 sandbox에서 등록 암호문 하나만 전송한다.
addEventListener('message', async function receive(event) {
  if (event.source !== parent || event.origin !== new URL(location.href).origin || event.ports.length !== 1) return;
  removeEventListener('message', receive);
  const port = event.ports[0];
  let status = 0;
  try {
    const { endpoint, payload } = event.data;
    const url = new URL(endpoint);
    const local = url.protocol === 'http:' && url.hostname === '127.0.0.1';
    if ((url.protocol !== 'https:' && !local) || url.search || url.hash || url.username || url.password ||
        !/\/v1\/rooms\/[a-f0-9]{64}\/submit$/.test(url.pathname) || !/^[A-Za-z0-9_-]{100,2048}$/.test(payload)) return;
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload }), credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' });
    status = response.status;
  } catch { /* 일시 연결 실패도 같은 암호문으로 다시 시도할 수 있다. */ }
  finally { port.postMessage(status); port.close(); }
});
