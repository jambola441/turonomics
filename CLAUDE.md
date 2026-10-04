# Working in this repo

## Run the checks the way CI runs them

There are two toolchains on the box: `/root/.local/bin` is newer than the
project's own, and `which ruff` finds it first. Run everything through the
interpreter the project uses, so a clean local run means a clean CI run:

```sh
cd api
python3 -m pytest -q
python3 -m ruff check src tests
python3 -m mypy src          # --strict; CI gates on this
```

The bare `ruff` and `mypy` on PATH will both lie to you: `ruff` reports a
pre-existing UP012 in a file nobody touched, and `mypy` reports a hundred
missing-stub errors because it cannot see this interpreter's packages. Both
are noise, and the second one hid a real `--strict` failure that reached CI.

## Postgres

The local cluster does not survive between sessions. Start it before the
tests touch the database:

```sh
pg_isready -q || pg_ctlcluster 16 main start
```

Tests that need it skip rather than fail when it is down, so a run reporting
"135 skipped" means the database is not up, not that all is well.

## Mutation-test anything that masks or derives

Twice now a test here passed against a knowingly broken implementation, and
only breaking the code on purpose caught it. Before trusting a new test,
invert the thing it is supposed to be testing and confirm the test fails.
