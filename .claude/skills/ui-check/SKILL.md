---
name: ui-check
description: Drive a real browser against the live observability UIs (Grafana, Temporal UI, Tempo, Pyroscope) to check them and attach screenshots to a PR. Use when a change touches a UI or dashboard.
---

# UI check

Check a live UI in a real browser and put the screenshot in the PR body.

## Browser contract

- The repo ships no browser launcher. The browser comes from the owner's dotfiles (`browser-cdp`, `chrome-devtools-axi`, `playwright-axi`).
- Always use the browser instance named `disyard`: `BROWSER_CDP_INSTANCE=disyard`.
  Use that same value for `start`, `status`, `stop` and `env`.
  With no instance set, `browser-cdp` means the shared default browser that other work depends on.
  Never run `start`, `stop` or restart without the instance, and never touch another instance.
- Start it and load the endpoint from the environment:

  ```sh
  BROWSER_CDP_INSTANCE=disyard browser-cdp start
  eval "$(BROWSER_CDP_INSTANCE=disyard browser-cdp env)"
  ```

  `env` exports `CHROME_DEVTOOLS_AXI_BROWSER_URL`, `PLAYWRIGHT_MCP_CDP_ENDPOINT` and `CHROME_DEVTOOLS_AXI_SESSION`.
  The session export is essential: `chrome-devtools-axi` keeps one bridge per session and ignores a changed URL.
- No host, IP or port is hard-coded anywhere. Read them from the environment.
- A new instance is a new browser profile with no logins. Grafana is anonymous, so none is needed.
- If `browser-cdp` is not set up on the machine, stop and report instead of improvising.

## Tools

- `chrome-devtools-axi` is the default for live checks: `open`, `snapshot`, `eval`, `console`, `network`, `screenshot`.
- `playwright-axi` is for scripted multi-step flows.

## One-shot check

```sh
scripts/ui-check.sh <url> <png-path>
```

It opens the URL, reads the page title and URL, saves the screenshot and prints `ok|FAIL <step> <detail>` lines.
It exits 2 and names what to set when the browser env is missing or the endpoint does not answer.

## URLs to check

| UI | URL |
|----|-----|
| Grafana | `http://127.0.0.1:13000` |
| Temporal UI | `http://127.0.0.1:18233` |
| Tempo | the Tempo explore view in Grafana |
| Pyroscope | the Pyroscope view in Grafana |

Take the screenshots after the stack is up, and put them in the PR body with the command output.
