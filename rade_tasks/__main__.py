"""Validate a contest scene and print its features."""

import argparse
import json
from pathlib import Path

from .scene import SceneState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to a scene JSON file")
    path = parser.parse_args().path
    try:
        features = SceneState(path.read_text(encoding="utf-8")).summary()
    except (OSError, ValueError) as error:
        parser.exit(1, f"{path}: {error}\n")
    print(json.dumps(features, indent=2))


if __name__ == "__main__":
    main()
