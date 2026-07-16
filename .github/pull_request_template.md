## Summary

Describe the user-visible or architectural outcome and why the change is needed.

## Design and risk

- Related L2 design or ADR:
- Main failure modes:
- Rollback or feature-gate strategy:

## Verification

- [ ] Fast tests pass locally (`pytest -m "not postgres"`)
- [ ] Lint passes locally (`ruff check .`)
- [ ] New or changed behavior has automated coverage
- [ ] Database migrations are backward/forward tested, or no migration is needed
- [ ] Documentation and examples are updated, or no documentation change is needed
- [ ] No credentials, private prompts, production data, or generated runtime artifacts are included

## Reviewer notes

Call out the files and decisions that deserve the closest review.
