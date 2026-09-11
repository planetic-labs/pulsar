"""Send a test embedding request and print the proxy response unchanged."""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

PROXY_BASE_URL = "https://orp.pre.m6z.ru/api/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the OpenRouter embedding proxy.")
    parser.add_argument("text", nargs="?", default="тестовый запрос", help="Text to embed")
    parser.add_argument("--timeout", type=float, default=60.0, help="Request timeout in seconds (default: 60)")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    proxy_token = os.getenv("PROXY_TOKEN", "")
    model = os.getenv("EMBEDDING_MODEL_ID", "")

    missing = [
        name
        for name, value in (
            ("OPENROUTER_API_KEY", openrouter_key),
            ("PROXY_TOKEN", proxy_token),
            ("EMBEDDING_MODEL_ID", model),
        )
        if not value
    ]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    url = f"{PROXY_BASE_URL}/embeddings"
    headers = {
        "Authorization": f"Bearer {openrouter_key}",
        "X-Proxy-Token": proxy_token,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pulsar.i28.ru",
        "X-Title": "Pulsar proxy test",
    }
    payload = {"model": model, "input": [args.text]}

    print(f"> POST {url}")
    print("> target: OpenRouter proxy")
    print(f"> model: {model}")
    print(f"> input: {args.text!r}")
    print("> authorization: Bearer [hidden]")
    print("> x-proxy-token: [hidden]")

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=args.timeout)
    except httpx.RequestError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1

    print(f"< HTTP {response.status_code} {response.reason_phrase}")
    print("< content-type:", response.headers.get("content-type", "not provided"))
    print()
    print(response.text)
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
