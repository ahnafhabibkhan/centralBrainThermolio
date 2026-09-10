---
name: memory-write
version: 0.1.0
description: Propose a durable memory with provenance and an explicit lifecycle.
---

# Memory write

## Use when

The user explicitly asks to remember something, or an approved product policy
permits proposing a durable fact, preference, decision, project state, or summary.

## Inputs

- authenticated workspace and actor (supplied by trusted middleware)
- concise proposed content and memory type
- source/provenance, confidence, sensitivity, and expiry

## Procedure

1. Refuse prohibited secrets and redact unnecessary sensitive data.
2. Separate direct statements from inference; label inference clearly.
3. Make the memory atomic, concise, and independent of the conversation transcript.
4. Select an expiry or explicitly mark the information durable.
5. Submit through the Brain API with an idempotency/deduplication key.
6. Keep it `proposed` unless policy explicitly allows activation.
7. Return the memory ID and approval state; never claim an unconfirmed write.

## Output

A write receipt containing memory ID, status, revision, and whether review is
required.
