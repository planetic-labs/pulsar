import httpx

from app.config import get_embedding_settings


def check_capabilities():
    settings = get_embedding_settings()
    url = f"{settings.api_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    try:
        r = httpx.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            model_info = data["data"][0]
            print(f"Модель: {model_info['id']}")
            print(f"Способности (Capabilities): {model_info.get('capabilities', [])}")

            if "lexical" in model_info.get("capabilities", []):
                print("✅ УРА! Sparse (lexical) векторы поддерживаются!")
            else:
                print("❌ Только Dense. Sparse всё еще не активен.")
        else:
            print(f"Ошибка: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Ошибка связи: {e}")


if __name__ == "__main__":
    check_capabilities()
