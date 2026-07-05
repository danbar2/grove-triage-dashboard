# Grove triage dashboard

A static dashboard for triaging [ai-dynamo/grove](https://github.com/ai-dynamo/grove)
issues and PRs, rebuilt twice a day (06:00 / 18:00 UTC) by GitHub Actions and
published on GitHub Pages.

**Dashboard:** https://danbar2.github.io/grove-triage-dashboard/

## What it shows

For every open issue/PR it computes the **last meaningful activity** — comments,
reviews, commits, force-pushes, review-thread replies, ready-for-review. Label,
milestone, assignment, and project churn is ignored, as is bot activity.

Items are grouped by whose turn it is:

| Section | Meaning | Sort |
|---|---|---|
| **Needs first response** | No maintainer has engaged yet | longest-waiting first |
| **Awaiting maintainer** | Author/community acted last (replied, pushed, resolved threads) — the working queue | longest-waiting first |
| **Awaiting author** | A maintainer responded last; ball is with the author | most recent first |
| **Stale** | No meaningful activity for 80+ days (either direction) | oldest first |

Classification rule: if the last non-bot actor is a maintainer (and not the
item's own author), the item is *awaiting author*; otherwise it's *awaiting
maintainer* (or *needs first response* if no maintainer has ever engaged).

## Configuration

Everything lives in [`config.yaml`](config.yaml): watched repo, the maintainers
list, ignored bots, and the staleness threshold (`stale_days: 80`).

## Running locally

```sh
pip install requests pyyaml
GITHUB_TOKEN=$(gh auth token) python generate.py
open dist/index.html
```

## Refresh

The workflow also runs on every push to `main` and can be triggered manually:

```sh
gh workflow run dashboard.yml -R danbar2/grove-triage-dashboard
```

`data.json` is published next to the page for building other views.
