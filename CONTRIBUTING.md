# Contributing

Bug reports and pull requests are welcome. This is a hobby project for a device
that sits next to a turntable, so the bar is "does it work reliably in a real
living room", not "does it handle every conceivable case".

## Getting set up

```bash
uv venv && uv pip install -e '.[dev]'
# or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

On Debian-based systems you also need `libportaudio2` for microphone capture.
Without it everything still imports and the test suite passes — you just cannot
open a real device.

```bash
pytest                     # full suite, no network or sound card needed
ruff check . && ruff format --check .
mypy nowspinning           # run under Python 3.12; numpy's stubs need it
```

## What the tests cover

Nothing in the suite touches the network or a sound card, which is what lets it
run in CI and on a laptop with no microphone:

- audio detection runs against synthetic signals built in `tests/conftest.py`
- Shazam parsing runs against recorded JSON in `tests/fixtures/`
- engine policy runs against `FakeRecognizer` and a hand-driven clock
- the pygame renderer draws to an offscreen surface with `SDL_VIDEODRIVER=dummy`

If you change engine timing or the gate, add a test that says what the new
behaviour is in terms of a record playing — `tests/test_engine.py` is written that
way on purpose, because the rules only make sense in those terms.

## Adding a recognition provider

Implement two methods:

```python
class MyRecognizer:
    name = "mine"

    async def identify(self, wav_path: Path) -> Track | None: ...
    async def aclose(self) -> None: ...
```

Then add it to `build_recognizer` in `nowspinning/recognize/__init__.py` and to the
`provider` literal in `nowspinning/config.py`. Keep response parsing in a pure
function so it can be tested against a recorded fixture, the way
`nowspinning/recognize/shazam.py` does — that is the part that breaks when an
upstream API changes, and you want a test that tells you so.

Raise `RecognizerError` for provider failures; return `None` for a clean no-match.
The engine treats them the same way but logs them differently.

## Reporting a bug

The useful details for a recognition problem:

- output of `now-spinning devices`
- output of `now-spinning identify` (it prints the captured level)
- a few lines of `now-spinning calibrate` with a record playing
- your config file, and the OS and Python version

## Style

Ruff handles formatting and linting; there is no separate style guide. Comments
should explain why something is the way it is — particularly in `engine.py`, where
most of the constants exist because of some specific way a turntable behaves.
