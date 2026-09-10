# Version-controlled seed memory

These files hold small, reviewed, non-secret facts that are safe to share with all
repository collaborators. They are templates and do not replace runtime storage.

- `identity.md`: purpose and durable product constraints.
- `preferences.md`: explicitly stated working preferences.
- `projects.md`: high-level project status and next actions.
- `decisions.md`: append-only architecture decision index.

Use this format for each entry:

```yaml
id: stable-kebab-case-id
status: active | superseded
source: human | repository | external
verified_on: YYYY-MM-DD
expires_on: YYYY-MM-DD | never
sensitivity: public | internal
```

Do not commit conversation transcripts, personal profiles, customer content,
credentials, or inferred attributes. Pull requests provide review and provenance.
