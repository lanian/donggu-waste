// 지문·얼굴 빠른 잠금 해제 (WebAuthn 플랫폼 인증기 + PRF 확장)
// - 인증기는 이 휴대폰 안의 것만 쓴다(authenticatorAttachment: platform, 사용자 확인 필수).
// - PRF 값은 메모리에서만 쓰고 저장하지 않는다. 저장되는 것은 credId, PRF 소금, 감싼 데이터 키(store.js)뿐이다.
// - 서버가 없으므로 challenge는 기기에서 만든 난수다. 인증의 근거는 서명이 아니라 PRF 값으로 데이터 키가 풀리는지다.

const rnd = (n) => crypto.getRandomValues(new Uint8Array(n));

export async function bioAvailable() {
  try {
    if (!window.PublicKeyCredential || !PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable) return false;
    if (!(await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable())) return false;
    if (PublicKeyCredential.getClientCapabilities) {
      const c = await PublicKeyCredential.getClientCapabilities();
      if (c && c['extension:prf'] === false) return false;
    }
    return true;
  } catch { return false; }
}

const noPrf = () => Object.assign(new Error('이 휴대폰의 생체 인증(또는 지금 쓰는 비밀번호 관리자)이 키 보호(PRF)를 지원하지 않아 쓸 수 없습니다. Chrome·Samsung 인터넷을 최신으로 올리거나 아이폰은 iOS 18 이상에서 다시 해 보세요. 그 전까지는 PIN을 쓰세요.'), { code: 'NO_PRF' });

export async function bioEvaluate(credId, prfSalt, transports = null) {
  // transports는 등록 때 인증기가 알려 준 값(있으면). 'internal'로 못 박으면 일부 비밀번호 관리자(Samsung Pass 등)에서 못 찾을 수 있다.
  const allow = { type: 'public-key', id: credId, ...(transports && transports.length ? { transports } : {}) };
  const a = await navigator.credentials.get({ publicKey: {
    challenge: rnd(32), allowCredentials: [allow], userVerification: 'required', timeout: 60000,
    extensions: { prf: { eval: { first: prfSalt } } } } });
  const r = a && a.getClientExtensionResults ? a.getClientExtensionResults() : {};
  const out = r.prf && r.prf.results && r.prf.results.first;
  if (!out) throw noPrf();
  return new Uint8Array(out);
}

// 1단계: 인증 정보 만들기. 등록 응답에 PRF 값이 있으면 바로 쓰고, 없으면 prfOut=null
// (예: Galaxy의 Samsung Pass는 등록 응답에 PRF가 없고 다음 인증부터 준다 → 화면에서 한 번 더 눌러 bioEvaluate).
export async function bioCreate() {
  const prfSalt = rnd(32);
  const cred = await navigator.credentials.create({ publicKey: {
    rp: { name: '동구 수거 (시제품)' }, user: { id: rnd(16), name: 'dg-phone', displayName: '이 휴대폰' }, challenge: rnd(32),
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
    authenticatorSelection: { authenticatorAttachment: 'platform', userVerification: 'required', residentKey: 'required', requireResidentKey: true },
    timeout: 60000, attestation: 'none', extensions: { prf: { eval: { first: prfSalt } } } } });
  const ext = cred.getClientExtensionResults ? cred.getClientExtensionResults() : {};
  const credId = new Uint8Array(cred.rawId);
  let transports = null;
  try { transports = cred.response && cred.response.getTransports ? cred.response.getTransports() : null; } catch {}
  const first = ext.prf && ext.prf.results && ext.prf.results.first;
  return { credId, prfSalt, transports, prfOut: first ? new Uint8Array(first) : null, prfReported: !!ext.prf };
}

// 한 번에 등록(시험·자동 흐름용): 등록 응답에 PRF 값이 없으면 바로 한 번 더 인증한다.
export async function bioRegister() {
  const c = await bioCreate();
  if (!c.prfOut) c.prfOut = await bioEvaluate(c.credId, c.prfSalt, c.transports);
  return c;
}
