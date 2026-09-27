# ThreadSatchel: a local MCP archive for carrying project context between chats

I got tired of explaining the same projects every time I started a new chat, so I built ThreadSatchel with help from Codex/ChatGPT. The idea is simple: keep the original conversation text in an archive I control, and let an assistant search it when it needs context.

It uses Python, SQLite/FTS5, and MCP. It can import notes and conversation excerpts, capture supported local Codex conversations incrementally, and collect queued messages from multiple computers over SSH. The capture/import pipeline doesn't use model calls or paid APIs.

One thing I cared about was avoiding duplicates when smaller imports overlap a later export. Repeated imports are safe, matching source IDs reuse existing records, and uncertain matches are kept for review rather than blindly merged. Originals and provenance stay preserved.

It's an early release, with setup instructions and tests. It isn't universal automatic ChatGPT/Claude chat capture or semantic memory, and full OpenAI export support still needs validation against a real dump.

GitHub: https://github.com/stevej52/threadsatchel

I'd appreciate feedback on setup, supported transcript formats, and overlap handling—especially from people switching between machines or AI clients.
