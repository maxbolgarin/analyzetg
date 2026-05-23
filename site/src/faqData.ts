export interface FaqItem {
  q: string;
  /** Plain-text answer used for the JSON-LD FAQPage schema */
  aText: string;
  /** Rendered HTML used in the page (may include <code>, <a>) */
  aHtml: string;
}

export const faqItems: FaqItem[] = [
  {
    q: 'Does this ship my Telegram history to OpenAI?',
    aText:
      'Only the messages within the time window you pick, and only the fields the LLM needs. Everything is stored locally in SQLite under ~/.unread/. You can also enable --redact to scrub PII from the API payload while keeping the originals on your disk.',
    aHtml:
      'Only the messages within the time window you pick, and only the fields the LLM needs. Everything is stored locally in <code>SQLite</code> under <code>~/.unread/</code>. You can also enable <code>--redact</code> to scrub PII (phones, emails, IBANs, Luhn-valid card numbers) from the API payload while keeping the originals on your disk.',
  },
  {
    q: "What if I don't use Telegram?",
    aText:
      'Skip Telegram setup during unread init. The same <ref> syntax works for YouTube videos, web pages, and local files (PDF, DOCX, audio, video, images, stdin).',
    aHtml:
      'Skip Telegram setup during <code>unread init</code>. The same <code>&lt;ref&gt;</code> syntax works for YouTube videos, web pages, and local files (PDF, DOCX, audio, video, images, stdin).',
  },
  {
    q: 'What languages does it actually support?',
    aText:
      'Source language and report language are independent. Hand-tuned preset structures exist for English and Russian; other languages use the English preset as a template and still produce coherent reports. Example: --report-language es turns Russian chats into Spanish digests.',
    aHtml:
      'Source language and report language are <strong>independent</strong>. Hand-tuned preset structures exist for English and Russian; other languages use the English preset as a template and still produce coherent reports. Example: <code>--report-language es</code> turns Russian chats into Spanish digests.',
  },
  {
    q: 'Will it cost me money?',
    aText:
      'Yes — every LLM call costs something. unread keeps it small: --max-cost aborts before overspend, --dry-run estimates without calling, and unread stats shows lifetime spend by chat, preset, and day. A week of group chat is typically pennies on gpt-5.4-mini.',
    aHtml:
      'Yes — every LLM call costs something. <code>unread</code> keeps it small: <code>--max-cost</code> aborts before overspend, <code>--dry-run</code> estimates without calling, and <code>unread stats</code> shows lifetime spend by chat, preset, and day. A week of group chat is typically pennies on <code>gpt-5.4-mini</code>.',
  },
  {
    q: 'Is it actually fast?',
    aText:
      'Yes. Big histories are chunked and analyzed in parallel via map-reduce. There are two caches: a local analysis cache (re-running unchanged input is free) and the provider prompt cache (server-side discount on repeated prefixes). Run unread cache stats to see hit rate.',
    aHtml:
      'Yes. Big histories are chunked and analyzed in parallel via map-reduce. There are two caches: a local analysis cache (re-running unchanged input is free) and the provider prompt cache (server-side discount on repeated prefixes). Run <code>unread cache stats</code> to see hit rate.',
  },
  {
    q: 'Can I run it on a server or in cron?',
    aText:
      'Yes. Non-TTY mode skips interactive prompts. unread watch --interval 1h loops in foreground. API keys can come from environment variables or ~/.unread/.env.',
    aHtml:
      'Yes. Non-TTY mode skips interactive prompts. <code>unread watch --interval 1h</code> loops in foreground. API keys can come from environment variables or <code>~/.unread/.env</code>.',
  },
  {
    q: 'Is it really free and open source?',
    aText:
      'Yes. unread is MIT-licensed and developed in the open on GitHub. You only pay your LLM provider for the tokens you use. There is no service, no telemetry, no account.',
    aHtml:
      'Yes. <code>unread</code> is MIT-licensed and developed in the open on <a href="https://github.com/maxbolgarin/unread" rel="noopener" target="_blank">GitHub</a>. You only pay your LLM provider for the tokens you use. There is no service, no telemetry, no account.',
  },
];
