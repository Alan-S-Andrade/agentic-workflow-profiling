#!/usr/bin/env python3
"""Minimal Gemini API smoke test; no third-party packages required."""

import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Google's current highest-capability general-purpose free-tier Gemini endpoint.
MODEL = "gemini-3.8-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def api_key() -> str:
    """Use the exported environment value, or read its simple export from ~/.bashrc."""
    if key := os.environ.get("GEMINI_API_KEY"):
        return key

    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        pattern = re.compile(r"^\s*export\s+GEMINI_API_KEY\s*=\s*(['\"]?)([^'\"\s#]+)\1\s*$")
        for line in bashrc.read_text(encoding="utf-8").splitlines():
            if match := pattern.match(line):
                return match.group(2)

    raise RuntimeError(
        "GEMINI_API_KEY was not found. Run `source ~/.bashrc` or add "
        "`export GEMINI_API_KEY=...` to ~/.bashrc."
    )


def main() -> None:
    prompt = "Reply with exactly: Gemini API connection successful."
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            body = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Gemini API: {exc.reason}") from exc

    try:
        print(body["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(body)}") from exc


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
