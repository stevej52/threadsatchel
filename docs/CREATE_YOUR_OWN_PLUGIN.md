# Connect a personal assistant to ThreadSatchel

Choose a connection based on where the assistant runs and whether it should be able to save.

**A desktop or CLI client on the archive computer** can launch the included read-only MCP server over standard input and output. Enable the separate save server if the assistant should save notes or handoffs. This route needs no gateway or hosting account. [Local server configuration](CONNECTING.md)

**A web, mobile, or remote client** can connect to a private gateway that you deploy. ThreadSatchel includes the gateway source and the outbound connector. Each installation supplies its own hosting, credentials, archive computer, and client connection. The connector uses an outbound connection so the archive computer can stay behind its firewall. The gateway and authorized clients can see the content that passes through them. [Gateway setup and authentication](PRIVATE_GATEWAY.md)

After connecting, search for a synthetic note you have imported. Use the returned reference and lookup ID to read back a known passage. If you enable writes, confirm an importer receipt for a synthetic save and retry it with the same key. A queued or timed-out operation still needs a confirmed result. [Save results](SAVE_RESULTS.md)

For clients that support standing instructions, there is an optional example covering what to retrieve and when to save. [Example instructions](STANDING_INSTRUCTIONS.md)
