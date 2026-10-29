# Issue tracker: GitHub

Specs and tickets live in GitHub Issues for `lsjpwr/sunnypilot`. Always pass `--repo lsjpwr/sunnypilot`; the clone has several remotes.

## Operations

- Publish: write the exact Markdown body to a UTF-8 file, then run `gh issue create --repo lsjpwr/sunnypilot --title "..." --body-file <path>`.
- Read: `gh issue view <number> --repo lsjpwr/sunnypilot --json number,title,body,labels,comments`.
- List: `gh issue list --repo lsjpwr/sunnypilot --state open --json number,title,body,labels`.
- Comment: use `gh issue comment <number> --repo lsjpwr/sunnypilot --body-file <path>`.
- Label: `gh issue edit <number> --repo lsjpwr/sunnypilot --add-label <label>`.
- Close: `gh issue close <number> --repo lsjpwr/sunnypilot`.

## Parent and blocking relationships

Create one spec issue and one issue per implementation ticket. Link tickets as sub-issues of the spec. With GitHub CLI 2.94 or newer, use `gh issue create --parent <spec-number> ...`; otherwise use GitHub's sub-issue API.

Record blocking edges using GitHub's native dependency API. Resolve the blocker's database ID with `gh api repos/lsjpwr/sunnypilot/issues/<number> --jq .id`, then POST it as `issue_id` to `repos/lsjpwr/sunnypilot/issues/<blocked-number>/dependencies/blocked_by`.

If native relationships are unavailable, include `Parent: #<spec-number>` and `Blocked by: #<blocker-number>` in each ticket body. A ticket is ready to start when every blocker is closed.

Use the labels in `triage-labels.md`. Work on one ready ticket at a time unless parallel implementation has been requested. Keep the spec open until the complete integration has passed its acceptance checks.

## Pull requests as a triage surface

PRs as a request surface: no.
