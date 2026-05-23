// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import { visit } from 'unist-util-visit';

const BASE = '/unread';

function rewriteDocLinks() {
  return (/** @type {any} */ tree) => {
    visit(tree, 'element', (/** @type {any} */ node) => {
      if (node.tagName !== 'a') return;
      const href = node.properties?.href;
      if (!href || typeof href !== 'string') return;
      if (/^(https?:)?\/\//i.test(href) || href.startsWith('mailto:') || href.startsWith('#')) return;

      if (href === '../README.md' || href === '../../README.md' || href === '../README.md#top') {
        node.properties.href = `${BASE}/`;
        return;
      }

      const m = href.match(/^(?:\.\.\/)?docs?\/([a-z0-9_-]+)\.md(#.*)?$/i);
      if (m) {
        node.properties.href = `${BASE}/docs/${m[1]}/${m[2] ?? ''}`;
        return;
      }
      const m2 = href.match(/^([a-z0-9_-]+)\.md(#.*)?$/i);
      if (m2) {
        node.properties.href = `${BASE}/docs/${m2[1]}/${m2[2] ?? ''}`;
        return;
      }
    });
  };
}

export default defineConfig({
  site: 'https://maxbolgarin.github.io',
  base: BASE,
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
  integrations: [
    sitemap({
      filter: (page) => !page.includes('/404'),
    }),
  ],
  markdown: {
    shikiConfig: {
      themes: { light: 'github-light', dark: 'github-dark' },
      wrap: true,
    },
    rehypePlugins: [/** @type {any} */ (rewriteDocLinks)],
  },
});
