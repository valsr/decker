"""Entry point: python -m decker_plugin_sdk <entry.py>."""

import importlib.util
import os
import sys
from typing import BinaryIO, cast

from ._actions import registered_actions
from ._runtime import registered_cards, run


def main(argv: list[str]) -> int:
    """Import the plugin entry file and run its cards over the real stdio."""
    if len(argv) != 2:
        print("usage: python -m decker_plugin_sdk <entry.py>", file=sys.stderr)
        return 2
    # Keep a private copy of the protocol pipe, then point fd 1 at stderr so stray prints, child
    # processes and C extensions can never corrupt the protocol stream.
    sys.stdout.flush()
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    stdout = cast(BinaryIO, os.fdopen(proto_fd, "wb"))
    spec = importlib.util.spec_from_file_location("decker_plugin_entry", argv[1])
    if spec is None or spec.loader is None:
        print(f"cannot load plugin entry {argv[1]}", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = run(registered_cards(), cast(BinaryIO, sys.stdin.buffer), stdout, registered_actions())
    stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # pylint: disable=protected-access  # do not wait for blocked handler threads


if __name__ == "__main__":
    sys.exit(main(sys.argv))
