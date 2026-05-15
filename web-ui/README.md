# CodexBot Web UI

Vite + React + TypeScript front-end for the CodexBot web transport.

Uses [pnpm](https://pnpm.io) as the package manager (pinned via the
`packageManager` field in `package.json`).

## Setup

```bash
cd web-ui
pnpm install
```

## Development

```bash
# Run the Python backend in another terminal with WEB_UI_PASSWORD set.
pnpm dev             # Vite on http://127.0.0.1:5173 (proxies /api → :8787)
```

## Production build

```bash
pnpm build           # outputs to web-ui/dist
```

The Python backend serves `web-ui/dist/` at `/` when present, so a single
`uv run codexbot` boot delivers both the API and the bundled SPA.

Override the dist location with `CODEXBOT_WEB_DIST=/path/to/dist`.
