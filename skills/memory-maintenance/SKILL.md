---
name: memory-maintenance
version: 0.1.0
description: Review, supersede, expire, export, or delete Central Brain memories.
---

# Memory maintenance

## Use when

A memory conflicts with newer evidence, reaches its expiry, needs human review, or
a subject requests export or deletion.

## Procedure

1. Authenticate the actor and verify the required reviewer/privacy role.
2. Retrieve the record and its revision chain without changing it.
3. For correction, create a new revision and supersede the old record.
4. For expiry, archive it according to retention policy.
5. For a privacy request, run the approved export/deletion workflow across source
   rows, derived summaries, embeddings, caches, and backups as policy requires.
6. Write an audit event with identifiers and outcome, not deleted content.

## Output

A maintenance receipt listing affected identifiers, action, timestamp, and actor.
