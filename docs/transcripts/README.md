# Build transcripts

This charm was built by **Claude Fable 5** (Anthropic's AI model) running in
Claude Code inside a sandbox VM, directed by a human through a series of
prompts. These are the complete, unedited session transcripts of that work —
every command, file edit, test run, deploy, failure and fix.

## Reading the transcripts

- **[build-session.md](build-session.md)** — the full session as a single
  Markdown file (rendered directly by GitHub; generated with
  [`claude-transcript`](https://pypi.org/project/claude-transcript/)).
- **[html/](html/)** — the same session as a paginated, mobile-friendly HTML
  archive (generated with
  [`claude-code-transcripts`](https://pypi.org/project/claude-code-transcripts/)).
  GitHub shows HTML files as source; to read these, either clone the repo and
  open `html/index.html` in a browser, or view them through
  [htmlpreview.github.io](https://htmlpreview.github.io/).

## Caveats

- The transcripts necessarily end at the moment they were exported, so the
  very last commits (adding this directory and the README warning) are not
  themselves captured.
- Tool output shown in the transcripts (test results, deploy logs) reflects
  the sandbox environment at build time.
