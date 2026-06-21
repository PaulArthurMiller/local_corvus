"""
Milestone 7 smoke test — verify Gemma 4 E4B accepts image_url content-parts via Ollama.
Run from repo root: python tests/smoke_test_multimodal.py
"""
import base64
import pathlib
import sys

import httpx

IMG_PATH = pathlib.Path(__file__).parent / "test_images" / "test_image.jpg"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "gemma4:e4b"


def run_smoke_test():
    if not IMG_PATH.exists():
        print(f"ERROR: test image not found at {IMG_PATH}")
        sys.exit(1)

    img_b64 = base64.b64encode(IMG_PATH.read_bytes()).decode()
    print(f"Image size: {IMG_PATH.stat().st_size} bytes")
    print(f"Sending to {OLLAMA_URL} with model {MODEL} ...")

    # Try OpenAI-compatible format with nested image_url object
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image in one sentence."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]
        }],
        "stream": False,
    }

    try:
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=120)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            print(f"Response: {content}")
            print("\nSMOKE TEST PASSED — nested image_url object format works")
            return "nested_object"
        else:
            print(f"Response body: {resp.text[:500]}")
            print("\nTrying flat url format...")
    except Exception as e:
        print(f"Request failed: {e}")
        print("\nTrying flat url format...")

    # Try flat url format as fallback
    payload["messages"][0]["content"][1] = {
        "type": "image_url",
        "url": f"data:image/jpeg;base64,{img_b64}"
    }

    try:
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=120)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            print(f"Response: {content}")
            print("\nSMOKE TEST PASSED — flat url format works")
            return "flat_url"
        else:
            print(f"Response body: {resp.text[:500]}")
            print("\nSMOKE TEST FAILED — neither format worked")
            sys.exit(1)
    except Exception as e:
        print(f"Request failed: {e}")
        print("\nSMOKE TEST FAILED")
        sys.exit(1)


if __name__ == "__main__":
    run_smoke_test()
