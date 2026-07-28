#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from browser_backend import (
    BrowserLaunchError,
    CloakBrowserBackend,
    PlaywrightBrowserBackend,
)


async def smoke(backend_name: str, headless: bool, profile_dir: Path) -> None:
    backend = (
        CloakBrowserBackend(profile_dir, headless)
        if backend_name == "cloakbrowser"
        else PlaywrightBrowserBackend(profile_dir, headless)
    )
    try:
        context = await backend.start()
        page = context.pages[0] if context.pages else await context.new_page()
        response = await page.goto(
            "https://example.com", wait_until="domcontentloaded", timeout=30_000
        )
        print(f"backend={backend_name}")
        print(f"status={response.status if response else 'none'}")
        print(f"title={await page.title()}")
    finally:
        await backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manually open only example.com with the selected backend."
    )
    parser.add_argument(
        "--backend", choices=("cloakbrowser", "playwright"), default="cloakbrowser"
    )
    parser.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=False
    )
    parser.add_argument(
        "--profile-dir", type=Path, default=Path(".browser-smoke-profile")
    )
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args.backend, args.headless, args.profile_dir))
    except BrowserLaunchError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
