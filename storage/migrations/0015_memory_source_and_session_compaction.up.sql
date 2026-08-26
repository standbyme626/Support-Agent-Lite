-- #6 记忆系统增强(pi compaction 同款语义):
-- 1) memories.source: 记忆来源标记(v2.md §49 —— Requester-confirmed closure
--    优先于 force-closed;空串为历史数据的中性值)
-- 2) session_compactions: 会话级滚动摘要条目(追加式,上下文取最新一条;
--    first_kept_message_id 即 pi CompactionEntry.firstKeptEntryId 的对应物)
ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS session_compactions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  summary TEXT NOT NULL,
  first_kept_message_id TEXT,
  messages_compacted INTEGER NOT NULL,
  chars_before INTEGER NOT NULL,
  summarizer TEXT NOT NULL CHECK (summarizer IN ('llm', 'deterministic')),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_compactions_session ON session_compactions(session_id, created_at);
