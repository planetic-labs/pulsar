import http.server
import json
import socketserver
import threading
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# 1. Generate RSA key pair
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Export public key parameters for JWKS
numbers = private_key.public_key().public_numbers()


# Helper to encode int as base64url
def int_to_base64url(val):
    import base64

    # convert int to bytes in big-endian
    length = (val.bit_length() + 7) // 8
    b = val.to_bytes(length, "big")
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


n_encoded = int_to_base64url(numbers.n)
e_encoded = int_to_base64url(numbers.e)

jwks_data = {
    "keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "test-key-id", "n": n_encoded, "e": e_encoded}]
}


# 2. Start a mock JWKS server
class JWKSHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress server logs to keep output clean
        return

    def do_GET(self):
        if self.path == "/.well-known/jwks.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(jwks_data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_jwks_server():
    # Use port 8085 for mock JWKS
    with socketserver.TCPServer(("127.0.0.1", 8085), JWKSHandler) as httpd:
        httpd.serve_forever()


# Start mock JWKS server in a background thread
t = threading.Thread(target=run_jwks_server, daemon=True)
t.start()

# Give server a moment to start
time.sleep(0.5)

# 3. Generate a signed JWT token
headers = {"kid": "test-key-id"}
payload = {
    "sub": "01H2VXDR3X",
    "status": "active",
    "jti": "01H2VXDR4Y",
    "roles": ["admin"],
    "exp": int(time.time()) + 3600,
}

# Sign JWT token with the private key
pem_private = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

token = jwt.encode(payload, pem_private, algorithm="RS256", headers=headers)

print("=" * 60)
print(" Ark Messenger Authorization Mock & Verification Helper")
print("=" * 60)
print("\n1. Настройки для вашего .env файла:")
print("   Убедитесь, что в .env файле Pulsar прописано следующее:")
print("   ARK_JWKS_URL=http://127.0.0.1:8085/.well-known/jwks.json")
print("   ARK_WEBHOOK_SECRET=test-webhook-secret")
print("\n2. Сгенерированный JWT-токен для тестирования:")
print(f"   {token}")
print("\n3. Инструкция для входа и проверки:")
print("   Вставьте этот токен на странице входа (вкладка 'Резервный ключ'): http://127.0.0.1:8350/login")
print("\n4. Интеграция API (Bearer):")
print("   Вы можете делать запросы к API с заголовком:")
print(f"   Authorization: Bearer {token}")
print("\n5. Нажмите Ctrl+C для выхода и остановки JWKS-сервера.")
print("=" * 60)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nОстановка сервера...")
