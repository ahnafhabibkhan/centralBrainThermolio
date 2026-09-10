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
3. For correction, use the review webpage to propose a new revision. The old record is superseded only when the human approves the replacement.
4. Expired records are excluded from retrieval. The pilot does not automatically archive them.
5. For export or deletion, direct the human to the review webpage. Deletion applies to one live record. Previous versions, backups, and assistant conversation copies require separate review. The pilot has no embedding or summary cleanup system.
6. Write an audit event with identifiers and outcome, not deleted content.

## Output

A maintenance receipt listing affected identifiers, action, timestamp, and actor.
