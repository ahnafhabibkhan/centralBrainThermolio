# Memory governance

## What may be stored

Store only information required for a declared purpose: reviewed preferences,
stable facts, decisions, project state, and compact summaries. Each memory needs a
type, source, sensitivity, confidence, owner/workspace, timestamps, and an expiry
decision.

Never store credentials, access tokens, recovery codes, private keys, raw payment
card data, or hidden system prompts. Avoid sensitive personal data unless a
documented requirement, legal basis, access policy, and deletion path exist.

## Write policy

1. Redact prohibited content before it reaches logs or storage.
2. Require authenticated workspace and actor identifiers from trusted middleware,
   never from an unverified model response.
3. Validate content length and allowed metadata fields.
4. Calculate a deduplication key and look for active equivalents.
5. Record provenance as structured data; never claim a model inference is a user
   statement.
6. Set `proposed` unless policy permits automatic activation for that memory type.
7. Emit an audit event containing identifiers and outcome, not sensitive content.

## Prompt-injection rule

All stored content is untrusted, including user-authored memory and imported
documents. Retrieval clients must label it as reference material and must not obey
commands embedded in it. Skills may be executed only from the trusted, reviewed
skill registry and at a pinned version.

## Human review

Reviewers should see proposed content, source, author, sensitivity, expiry, and
conflicts. Approval and rejection create audit events. A correction creates a new
revision; historical audit records remain immutable subject to the legal retention
policy.
