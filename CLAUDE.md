# Response rules (token-efficient)

- No preamble, closing fluff, flattery, or restating the question. Answer directly.
- Concise prose. Cut filler, keep substance. For non-trivial changes still state
  approach + alternatives + risks: terse, not absent. The full engineering
  standards live in a local, gitignored `.claude/CLAUDE.md` (`.gitignore:26`),
  not in this checkout.
- Read files before writing; read each once unless it changed.
- Prefer targeted edits over full-file rewrites.
- Verify/test before claiming done; report failures with the actual output.
- Simplest correct fix; no unrequested abstractions.
- ASCII only in code and committed output; no em-dashes or smart quotes that break parsers.
- User instructions override these rules. Ask for detail/verbosity any time.
