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

export async function bioEvaluate(credId, prfSalt) {
  const a = await navigator.credentials.get({ publicKey: {
    challenge: rnd(32), allowCredentials: [{ type: 'public-key', id: credId, transports: ['internal'] }], userVerification: 'required', timeout: 60000,
    extensions: { prf: { eval: { first: prfSalt } } } } });
  const r = a && a.getClientExtensionResults ? a.getClientExtensionResults() : {};
  const out = r.prf && r.prf.results && r.prf.results.first;
  if (!out) throw noPrf();
  return new Uint8Array(out);
}

export async function bioRegister() {
  const prfSalt = rnd(32);
  const cred = await navigator.credentials.create({ publicKey: {
    rp: { name: '동구 수거 (시제품)' }, user: { id: rnd(16), name: 'dg-phone', displayName: '이 휴대폰' }, challenge: rnd(32),
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
    authenticatorSelection: { authenticatorAttachment: 'platform', userVerification: 'required', residentKey: 'discouraged' },
    timeout: 60000, attestation: 'none', extensions: { prf: { eval: { first: prfSalt } } } } });
  const ext = cred.getClientExtensionResults ? cred.getClientExtensionResults() : {};
  const credId = new Uint8Array(cred.rawId);
  // 등록 때 PRF 결과를 주지 않는 인증기가 있다(예: Galaxy의 Samsung Pass는 등록 응답에 PRF가 없고 다음 인증부터 줌).
  // 그래서 등록 응답만 보고 "지원 안 함"으로 막지 않고, 값이 없으면 한 번 더 인증해서 실제로 PRF 값이 나오는지로 판단한다.
  const first = ext.prf && ext.prf.results && ext.prf.results.first;
  const prfOut = first ? new Uint8Array(first) : await bioEvaluate(credId, prfSalt);
  return { credId, prfSalt, prfOut };
}
