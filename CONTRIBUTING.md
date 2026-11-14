# Contributing

Thanks for wanting to help. The setup here is simple: `dev` is where work happens, `main` is what ships.

## Branches

`main` is protected. Changes land there only through a pull request with green CI, never pushed directly. `dev` is the long-lived integration branch; it runs ahead of `main` and gets merged up whenever a batch is ready.

For your own work, branch off `dev` and use a prefix that says what it is: `feat/`, `fix/`, `docs/`, or `exp/` for experiments (usually the ones that spend API budget). Delete your branch after it merges; `dev` and `main` stay.

## The usual loop

1. `git switch dev && git pull`, then `git switch -c feat/<slug>`.
2. Make the change in the narrowest module that already owns it.
3. Run `uv run test` until it is green. These are the offline checks, no browser, and they finish fast.
4. Merge into `dev` with small commits that say what and why.

When `dev` has a shippable batch, run `uv run test all` (adds the Playwright browser smoke), open a PR from `dev` to `main`, and merge once CI is green. That is the whole ceremony.

## House rules

A few things that will get a PR rejected, so worth a skim:

- Secrets live in the OS keychain, never in `.env`, the database, or a commit. Do not read or edit `.env`; `.env.example` lists the variables.
- Paper trading only. The Alpaca client is hardcoded to `paper=True` and should stay that way.
- The server binds to `127.0.0.1` and rejects non-loopback `Host`/`Origin` headers. Leave those guards intact.
- Each `scripts/check_*.py` sets up its own environment and runs as its own process. Never import two checks into one process. New tests follow the same pattern and get picked up by `uv run test all`.
- Keep the README short; the wiki holds the depth.
- Anything that spends real API money (live-LLM backtests, `exp/` sweeps) needs owner sign-off first.

Not sure whether an idea fits? Open an issue and describe the problem you are hitting; we can figure out the solution together.
