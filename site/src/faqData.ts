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
      'Yes. unread is Apache 2.0 licensed and developed in the open on GitHub. You only pay your LLM provider for the tokens you use. No service, no telemetry, no account.',
    aHtml:
      'Yes. <code>unread</code> is Apache 2.0 licensed and developed in the open on <a href="https://github.com/maxbolgarin/unread" rel="noopener" target="_blank">GitHub</a>. You only pay your LLM provider for the tokens you use. No service, no telemetry, no account.',
  },
  {
    q: 'Can I run this as a Telegram bot?',
    aText:
      'Yes. unread bot run turns the CLI inside out — message your own @BotFather bot with a file, web URL, YouTube link, forwarded message, or a t.me/ link and get a Markdown summary back as a document with a cost-and-timing caption. It is single-user by design: allowlisted to one Telegram ID (yours), everyone else is silently dropped. Run it locally or deploy on a VM with docker-compose; see the bot guide for details.',
    aHtml:
      'Yes. <code>unread bot run</code> turns the CLI inside out — message your own <code>@BotFather</code> bot with a file, web URL, YouTube link, forwarded message, or a <code>t.me/</code> link and get a Markdown summary back as a document with a cost-and-timing caption. It is single-user by design: allowlisted to one Telegram ID (yours), everyone else is silently dropped. Run it locally or deploy on a VM with docker-compose — see the <a href="/unread/docs/bot/">bot guide</a> for details.',
  },
  {
    q: 'Can it transcribe voice messages and videos?',
    aText:
      'Yes — both standalone files (unread ./voice.ogg, unread ./meeting.mp4) and voice notes / video circles inside Telegram chats. Speech-to-text runs through OpenAI Whisper at roughly $0.006 per minute. Video files have their audio track extracted by ffmpeg first. Forwarded voice messages dedupe across chats — Whisper runs once and the result is cached by Telegram document_id.',
    aHtml:
      'Yes — both standalone files (<code>unread ./voice.ogg</code>, <code>unread ./meeting.mp4</code>) and voice notes / video circles inside Telegram chats. Speech-to-text runs through OpenAI <strong>Whisper</strong> at roughly <strong>$0.006 per minute</strong>. Video files have their audio track extracted by <code>ffmpeg</code> first. Forwarded voice messages dedupe across chats — Whisper runs once and the result is cached by Telegram <code>document_id</code>.',
  },
];
