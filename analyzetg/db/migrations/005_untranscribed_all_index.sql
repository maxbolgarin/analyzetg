-- 005_untranscribed_all_index: add a partial index that helps when
-- `untranscribed_media(chat_id=None)` scans the whole DB (the `transcribe`
-- maintenance command + stats queries). The pre-existing idx_msg_untranscr
-- leads with chat_id, so the chat_id=None path used to degrade to a full
-- table scan.

CREATE INDEX IF NOT EXISTS idx_msg_untranscr_all
    ON messages(date)
    WHERE media_doc_id IS NOT NULL AND transcript IS NULL AND media_type IS NOT NULL;
