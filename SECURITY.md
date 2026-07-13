# Public repository data safety

- Never commit real AIME documents, source excerpts, model outputs, database dumps, or secrets.
- Keep local artifacts under `.local/`, which is ignored by Git.
- Tests and examples must use invented companies, symbols, URLs, and numbers.
- Before every push, run `event-wiki security-scan` and inspect `git diff --cached`.
- If private material is committed, stop publishing and rotate any exposed credential before
  rewriting history.

Security reports should be sent privately to the repository owner rather than filed publicly.
