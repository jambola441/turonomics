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

The local cluster does not survive between sessions, and half the suite gates
on an environment variable rather than on whether the cluster is reachable.
Both are needed, every time:

```sh
pg_isready -q || pg_ctlcluster 16 main start
export TEST_DATABASE_URL="postgresql+psycopg://postgres:devpass@127.0.0.1:5432/turonomics_test"
cd api && python3 -m pytest -q      # 397 passed, 0 skipped
```

**A clean run has no skips.** An earlier version of this note said a run
reporting "135 skipped" meant the database was down; it did not. The database
was up and `TEST_DATABASE_URL` was unset, so `tests/test_trip_ingest.py` and
everything like it never ran at all — through a whole session of changes to
exactly that code. CI sets the variable, so CI was the only thing running them.

If pytest reports any number of skips, stop and fix the environment before
reading the result as a pass.

## Mutation-test anything that masks or derives

Twice now a test here passed against a knowingly broken implementation, and
only breaking the code on purpose caught it. Before trusting a new test,
invert the thing it is supposed to be testing and confirm the test fails.
