# Privacy and trust boundaries

The archive, retained input bytes, inbox, handoffs, generated indexes, and local backups may contain private information. They are plaintext files. Protect the archive directory and backup destination with filesystem permissions and disk encryption appropriate to your installation. Importing an export retains its original bytes, including content that may never become searchable.

Core capture, import, and lexical retrieval run locally. Optional inference sends configured content to local model services. Opt-in image captioning reads images in the folders you select. SSH capture sync sends packets to the destination you configure. Reading through an external assistant places the returned text in that client’s context. A remotely deployed gateway handles requests, responses, and operation state on the hosting provider you choose.

The read endpoint opens the originals read-only. Derived indexes and lookup accounting can be written separately. The save endpoint and outbound connector have broader access. Grant those capabilities only to intended clients and protect their credentials. [Remote access](PRIVATE_GATEWAY.md)

Capture filtering and credential detection are best-effort. Review manual imports and exclude material that should stay out of the archive. Instructions inside old chats are historical evidence and require current authorization before action.

Publish code and synthetic examples only. Keep local configs, databases, backups, models, transcripts, handoffs, logs, and credentials outside source control.
