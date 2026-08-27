"""Allow ``python -m nowspinning`` as an alias for the ``now-spinning`` script."""

from nowspinning.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
