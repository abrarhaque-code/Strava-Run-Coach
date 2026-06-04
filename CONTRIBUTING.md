# Contributing

Thanks for your interest in improving strava-run-coach. This is a small,
readable codebase and the bar for changes is mostly about keeping it that way.

## Running the tests

```bash
python3 -m unittest discover -s tests
```

The suite uses the standard-library `unittest` runner. It must pass with no
`.env` file and no network access. CI runs it on Python 3.10 through 3.13.

## The one hard rule: stdlib only

No third-party dependencies. Pull requests that add a runtime dependency will be
declined. If you need to make an HTTP request, use `urllib`. If you need to read
tabular data, use `csv` and plain dicts. The whole point is that a fresh clone
runs with nothing but a Python 3.10+ interpreter and CI needs no install step.

## Code style

Match the surrounding code. The project leans on dataclasses, type hints, and
small pure functions, with module-level docstrings that explain the why. Keep
functions focused, prefer reading inputs through `config.py` and the existing
data loaders over re-parsing files, and avoid clever one-liners where a plain
loop is clearer.

## Adding a new analysis module

1. Write the module with a clear print or report function and a `main()` guarded
   by `if __name__ == "__main__":`.
2. Read athlete numbers through `config.py` and activity data through the loaders
   in `analysis.py`, `metrics.py`, or `fitness_tracker.py`.
3. Register it in `coach.py` so it is reachable as a sub-command and is included
   in the full report.
4. Add a `unittest` test under `tests/` that runs with no `.env` and no network.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map and the data
interface contract.
