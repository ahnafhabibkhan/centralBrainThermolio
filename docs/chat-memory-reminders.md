# Chat memory reminders

Central Brain uses the same approved memory store for ChatGPT and Claude. A proposal from one app becomes searchable from the other after an authorized reviewer approves it, provided both connections have access to the same workspace and the memory's visibility permits access.

## Chat-side behavior

The technology@thermolio.com accounts have a Central Brain reminder in ChatGPT custom instructions and Claude profile instructions. ChatGPT's existing writing preferences were preserved. The MCP server also supplies reminder guidance when a host loads the connector.

At the end of a final chat reply, the assistant should ask whether to save a specific new durable preference, decision, fact, or procedure. When nothing qualifies, it should say, "Central Brain: There is nothing new to save." After a successful proposal, it should report that human approval is pending. Reminders belong in chat replies, outside generated documents and code.

The assistant should obtain agreement before proposing, unless the user already requested saving. It should check existing relevant memories to avoid duplicates, omit secrets and unnecessary sensitive information, and never claim success when the connector is unavailable.

These are model instructions, so adherence can vary by model, conversation, and host settings. They are not a background process or a guaranteed after-message event. A connector UI is rendered in response to selected tool calls; this deployment does not inject a global popup into either chat app. Project or custom-assistant instructions can also affect behavior.

## Repeat the cross-app test

1. In ChatGPT, ask Central Brain to propose a clearly labelled temporary verification entry with a unique marker and this conversation as its source. Request the proposal ID.
2. Sign in to Central Brain, open the pending proposal, inspect it, and approve it.
3. In Claude, ask Central Brain to search for the verification entry and report its marker, ID, and source. Do not include the marker in Claude's prompt, because that would invalidate the retrieval check.
4. Compare the returned marker and ID with ChatGPT's proposal. Use the platform's deletion confirmation if you later choose to remove the temporary entry.

Pending proposals are excluded from approved-memory search. A missing result does not by itself prove that a particular pending proposal exists.

## References

OpenAI documents [UI resources linked to selected MCP tools](https://developers.openai.com/plugins/build/chatgpt-ui). Anthropic documents [profile instructions across conversations](https://support.claude.com/en/articles/10185728-understanding-claude-s-personalization-features).
