export const SITE_URL = 'https://maxbolgarin.github.io';
export const BASE = '/unread';
export const SITE_NAME = 'unread';
export const SITE_TITLE = 'unread — your unread is now read.';
export const SITE_DESCRIPTION =
  'Open-source local CLI that turns Telegram chats, YouTube videos, web pages, and files into Markdown reports with citations. OpenAI, Anthropic, Gemini, OpenRouter, or local LLMs.';
export const SITE_TAGLINE = 'Your unread is now read.';
export const AUTHOR = 'Max Bolgarin';
export const AUTHOR_URL = 'https://github.com/maxbolgarin';
export const AUTHOR_EMAIL = 'mxbolgarin@gmail.com';
export const GH_URL = 'https://github.com/maxbolgarin/unread';
export const GH_ISSUES_URL = 'https://github.com/maxbolgarin/unread/issues';
export const GH_DISCUSSIONS_URL = 'https://github.com/maxbolgarin/unread/discussions';
export const GH_CHANGELOG_URL = 'https://github.com/maxbolgarin/unread/blob/main/CHANGELOG.md';
export const GH_CONTRIBUTING_URL = 'https://github.com/maxbolgarin/unread/blob/main/CONTRIBUTING.md';
export const GH_SECURITY_URL = 'https://github.com/maxbolgarin/unread/blob/main/SECURITY.md';
export const PYPI_URL = 'https://pypi.org/project/unread/';
export const LICENSE_URL = 'https://github.com/maxbolgarin/unread/blob/main/LICENSE';
export const VERSION = '0.1.1';

export const NAV: { label: string; href: string }[] = [
  { label: 'Docs', href: `${BASE}/docs/` },
  { label: 'Install', href: `${BASE}/docs/install/` },
  { label: 'GitHub', href: GH_URL },
];

export const DOCS_NAV: { label: string; href: string; slug: string }[] = [
  { label: 'Overview', href: `${BASE}/docs/`, slug: 'index' },
  { label: 'Install', href: `${BASE}/docs/install/`, slug: 'install' },
  { label: 'Sources', href: `${BASE}/docs/sources/`, slug: 'sources' },
  { label: 'CLI reference', href: `${BASE}/docs/reference/`, slug: 'reference' },
  { label: 'Configuration', href: `${BASE}/docs/configuration/`, slug: 'configuration' },
  { label: 'Security', href: `${BASE}/docs/security/`, slug: 'security' },
];
