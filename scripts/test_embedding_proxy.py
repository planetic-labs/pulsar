"""Send a test embedding request and print the proxy response unchanged."""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the configured OpenAI-compatible embedding proxy.")
    parser.add_argument("text", nargs="?", default="тестовый запрос", help="Text to embed")
    parser.add_argument(
        "--openrouter",
        action="store_true",
        help="Send the request directly to OpenRouter instead of EMBEDDING_API_URL",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Request timeout in seconds (default: 60)")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    model = os.getenv("EMBEDDING_MODEL_ID", "")

    if args.openrouter:
        base_url = "https://openrouter.ai/api/v1"
        token = os.getenv("EMBEDDING_OPENROUTER_API_KEY", "")
        openrouter_key = ""
        token_variable = "EMBEDDING_OPENROUTER_API_KEY"
    else:
        base_url = os.getenv("EMBEDDING_API_URL", "").rstrip("/")
        token = os.getenv("EMBEDDING_API_TOKEN", "")
        openrouter_key = os.getenv("EMBEDDING_OPENROUTER_API_KEY", "")
        token_variable = "EMBEDDING_API_TOKEN"

    missing = [
        name
        for name, value in (
            ("EMBEDDING_API_URL" if not args.openrouter else "", base_url),
            (token_variable, token),
            ("EMBEDDING_MODEL_ID", model),
        )
        if name and not value
    ]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    url = base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pulsar.i28.ru",
        "X-Title": "Pulsar proxy test",
    }
    if openrouter_key:
        headers["X-OpenRouter-Key"] = openrouter_key
    payload = {"model": model, "input": [args.text]}

    print(f"> POST {url}")
    print(f"> target: {'OpenRouter' if args.openrouter else 'configured proxy'}")
    print(f"> model: {model}")
    print(f"> input: {args.text!r}")
    print("> authorization: Bearer [hidden]")
    if openrouter_key:
        print("> x-openrouter-key: [hidden]")

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
