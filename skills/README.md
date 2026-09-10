# Skills

A skill is a reviewed, model-neutral operating procedure. It declares when to use
it, inputs, outputs, safety constraints, and steps. Skills must not contain secrets
or silently broaden tool permissions. Runtime deployments should ingest a pinned
version and store its content hash in `skill_versions`.

The `SKILL.md` files in this directory are portable source definitions. Provider
adapters may format them differently, but must preserve their requirements and
safety constraints.
