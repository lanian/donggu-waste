"""가상 관리자의 실제 인증 앱 QR을 읽어 등록·승인하는 브라우저 시험 도우미."""
import base64
import hashlib
import hmac
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit


class ApprovalApp:
    def __init__(self, context, url, password):
        self.context = context
        self.url = url.rstrip("/") + "/api/"
        self.password = password
        setup = self.post("devices/approval/setup/start", {"password": password})
        reader = context.new_page()
        try:
            reader.add_script_tag(path=Path(__file__).resolve().parents[1] / "web/vendor/jsQR.js")
            uri = reader.evaluate("""async src => {
                const image = new Image(); image.src = src; await image.decode();
                const canvas = document.createElement('canvas');
                canvas.width = image.width; canvas.height = image.height;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                return jsQR(ctx.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height).data;
            }""", setup["qr_image"])
        finally:
            reader.close()
        assert uri.startswith("otpauth://totp/")
        seed = parse_qs(urlsplit(uri).query)["secret"][0]
        self.key = base64.b32decode(seed + "=" * (-len(seed) % 8))
        now = int(time.time())
        self.used = now // 30 - (1 if now % 30 < 25 else 0)
        self.post("devices/approval/setup/finish", {"otp": self.number(self.used)})

    def post(self, path, body):
        response = self.context.request.post(self.url + path, data=body, headers={"X-DG-CSRF": "1"})
        assert response.ok, (path, response.status)
        return response.json()

    def number(self, step):
        digest = hmac.new(self.key, step.to_bytes(8, "big"), hashlib.sha1).digest()
        start = digest[-1] & 15
        return f'{(int.from_bytes(digest[start:start + 4], "big") & 0x7fffffff) % 1000000:06d}'

    def credentials(self):
        slot = max(int(time.time()) // 30, self.used + 1)
        # 한 시험에서 세 대 이상 등록할 때는 이미 사용한 번호 대신 다음 유효 구간까지 기다린다.
        wait = (slot - 1) * 30 - time.time()
        if wait > 0:
            time.sleep(wait + 0.05)
        self.used = slot
        return {"admin_password": self.password, "admin_otp": self.number(slot)}

    def approve(self, device):
        return self.post(f"devices/{device}/confirm", self.credentials())

    def approve_dialog(self, page):
        body = self.credentials()
        dialog = page.get_by_role("dialog", name="새 수거폰 등록 승인")
        dialog.get_by_label("관리자 비밀번호", exact=True).fill(body["admin_password"])
        dialog.get_by_label("관리자 OTP 6자리", exact=True).fill(body["admin_otp"])
        dialog.get_by_role("button", name="등록 승인", exact=True).click()
