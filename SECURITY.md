# Security policy

BetterTradingAgents is a self-hosted, paper-trading-only research app, and that shapes what counts as a security bug here.

## In scope

- The FastAPI server in `app/`: anything that slips past the loopback guards (the `127.0.0.1` bind, the `Host`/`Origin` checks), SSRF through user-configured endpoints, prompt injection that reaches tool output, or secrets leaking into the database, logs, or API responses.
- The install surface: vulnerable dependencies, unsafe defaults in `.env.example` or `scripts/setup_wizard.py`.

## Out of scope

- Attacks that need a non-loopback origin. The server refuses external hosts by design; if you expose it beyond localhost, what happens next is between you and your reverse proxy.
- Live trading. There is no live path; the Alpaca client is hardcoded to `paper=True`.
- Pointing `LLM_BASE_URL` at a third-party endpoint you do not trust.

## Reporting

Use GitHub's private vulnerability reporting: open the **Security** tab and click **Report a vulnerability**. Please do not open a public issue for anything exploitable.

Include a short description, the affected route or module, and steps to reproduce (a minimal `curl` is ideal). You will hear back within a week, and any disclosure is coordinated with you first.

## If you self-host

Keys are stored in the OS keychain via [keyring](https://pypi.org/project/keyring/), never in the database, `.env`, or logs. The server listens on `127.0.0.1` only. Paper trading only; never point this at a live brokerage account.
