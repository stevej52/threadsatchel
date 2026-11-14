# Architecture

The primary store is a SQLite database. The importer retains input bytes by SHA-256, records receipts, and commits searchable records and FTS5 entries together. Source identities and immutable text revisions support repeat-safe imports and exact retrieval. Ambiguous matches are recorded for review. [Import details](IMPORTING.md)

The read server opens the originals with SQLite read-only mode. Ranked discovery combines lexical candidates with optional local embeddings. It gives source documents, exact title matches and named versions extra weight. The retrieval layer builds revision-specific block maps, returns small previews and stores short references and per-lookup allowances in a separate cache. The optional shared reader keeps a single process-local cache across clients and authenticates loopback requests with an archive-specific key. [Read tools](CONNECTING.md)

The optional local AI worker builds derivative indexes and source-linked interpretations. Chat inference, vector generation, and background scheduling have separate configuration and resource controls. [Local models](LOCAL_MODELS.md)

Local capture can write packets or queue them for verified SSH transfer to a central importer. The inbox sweep checks receipts and maintains a separate recovery journal. Scheduled copies can go to an operator-selected destination. [Capture](CAPTURE.md) · [Recovery](BACKUPS.md)

The local save server and the optional outbound connector use the same archive handler for packet imports, operation receipts, and versioned handoffs. The remote gateway publishes the tool contract and routes authenticated operations to that connector. [Remote access](PRIVATE_GATEWAY.md)
