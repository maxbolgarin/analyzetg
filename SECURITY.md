# Security policy

## Scope

This policy covers the `unread` CLI itself — code in this repository,
the published PyPI wheel, and the GitHub Actions workflows that build
it. Vulnerabilities in upstream dependencies (Telethon, OpenAI / Anthropic
/ Google SDKs, `yt-dlp`, etc.) should be reported to those projects;
flag anything `unread` could mitigate as well.

## Supported versions

Until a stable 1.x line lands, only the latest published `0.x` release
on PyPI receives fixes.

| Version    | Supported |
| ---------- | --------- |
| Latest 0.x | ✅        |
| Older 0.x  | ❌        |

## Reporting a vulnerability

**Please do not file public GitHub issues for security problems.**

- Preferred: open a [private GitHub Security Advisory](https://github.com/maxbolgarin/unread/security/advisories/new).
- Alternative: email **mxbolgarin@gmail.com** with `[unread security]` in the subject.

Include enough detail to reproduce — affected version, OS, command line,
expected vs. observed behavior, and a minimal proof-of-concept if you
have one.

## What to expect

- Acknowledgement within **5 business days**.
- A first triage assessment (severity, scope, planned fix window) within **10 business days**.
- Coordinated disclosure: please give us a reasonable window to ship a
  fix before publishing details. Ninety days is a fair default, shorter
  for actively-exploited issues.

Reports made in good faith will be credited in the release notes unless
you'd rather stay anonymous.
