"""Run one registered Python strategy in a dedicated process."""

import json
import sys
from contextlib import redirect_stdout
from importlib import import_module

from .scene import SceneSnapshot
from .strategy import Solver


def main() -> None:
    module, function = sys.argv[1].split(":")
    request = json.load(sys.stdin.buffer)
    scene = SceneSnapshot(tuple(tuple(row) for row in request["P"]), tuple(request["v"]))
    try:
        with redirect_stdout(sys.stderr):
            solve: Solver = getattr(import_module(module), function)
            allocation = solve(scene, request["parameters"], seed=request["seed"])
        result = json.dumps(allocation, allow_nan=False)
    except Exception as error:
        sys.exit(f"{type(error).__name__}: {error}")
    print(result)


if __name__ == "__main__":
    main()
