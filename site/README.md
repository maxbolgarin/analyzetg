# unread — site

Marketing + docs site for [unread](https://github.com/maxbolgarin/unread), built with [Astro](https://astro.build) and deployed to GitHub Pages at <https://maxbolgarin.github.io/unread/>.

## Develop

```bash
cd site
npm install
npm run dev      # http://localhost:4321/unread/
npm run build    # outputs to site/dist/
npm run preview  # serve the production build
```

## How docs work

The five guides under `../docs/*.md` are imported at build time via `import.meta.glob` from inside `src/pages/docs/*.astro` — single source of truth, no duplication. A small rehype plugin in `astro.config.mjs` rewrites `*.md` cross-links to the site routes.

## Deploy

Pushed via `.github/workflows/deploy-site.yml`. Triggered on changes under `site/`, `docs/`, or the workflow file itself.

One-time GitHub setup:

1. Settings → Pages → **Source: GitHub Actions**.
