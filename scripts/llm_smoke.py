from __future__ import annotations

import argparse
import os
from pathlib import Path

from main import cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual LLM provider smoke test")
    parser.add_argument(
        "--provider",
        required=True,
        choices=("ollama", "mistral", "openai_compatible"),
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args(argv)
    os.environ["LLM_PROVIDER"] = args.provider
    command = ["--check-llm", "--env-file", str(args.env_file)]
    if args.profile is not None:
        command.extend(("--profile", str(args.profile)))
    return cli(command)


if __name__ == "__main__":
    raise SystemExit(main())
