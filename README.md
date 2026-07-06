# Grove triage board

A Jira-style Kanban board for triaging
[ai-dynamo/grove](https://github.com/ai-dynamo/grove) issues and PRs, rebuilt
twice a day (06:00 / 18:00 UTC) by GitHub Actions and published on GitHub
Pages.

**Board:** https://danbar2.github.io/grove-triage-dashboard/

## What it shows

For every open issue/PR it computes the **last meaningful activity** — comments,
reviews, commits, force-pushes, review-thread replies, ready-for-review. Label,
milestone, assignment, and project churn is ignored, as is bot activity.

Each stage is a column; cards are sorted by last meaningful activity, newest
first:

| Column | Meaning |
|---|---|
| **Needs first response** | No maintainer has engaged yet |
| **Awaiting maintainer** | Author/community acted last (replied, pushed, resolved threads) — the working queue |
| **Awaiting author** | A maintainer responded last; ball is with the author |
| **Triaged backlog** | Issues with the `triaged_when_set` fields (issue type + Priority) all set — already triaged, no first response needed |
| **Stale** | No meaningful activity for 80+ days (either direction) |

Classification rule: if the last non-bot actor is a maintainer (and not the
item's own author), the item is *awaiting author*; otherwise it's *awaiting
maintainer* (or *needs first response* if no maintainer has ever engaged).

## Editing from the board

On issue cards, the **type icon** (Task/Bug/Feature) and the **priority badge**
(P0/P1/P2) are clickable and update GitHub directly — setting both moves a card
from *Needs first response* to *Triaged backlog* on the spot. Editing calls the
GitHub API from your browser, so it needs a token: click **⚙ Token** in the
header and paste a classic PAT with `repo` + `project` scopes, SSO-authorized
for `ai-dynamo`, expiration ≤ 1 year. The token is stored only in your
browser's localStorage. Issues not yet on the project are added to
"Grove - New" automatically when you set a priority.

### Project fields (Priority / Severity)

The type comes from GitHub's native issue type; Priority and Severity are read
from the issue's GitHub Project (v2) single-select fields named `Priority` and
`Severity` (case-insensitive, any linked project). Reading Projects v2 requires
a token with the `read:project` scope, which neither the default Actions token
nor a plain `repo` PAT has. To enable the check, add a repo secret named
**`PROJECTS_TOKEN`** containing a PAT with `repo` + `read:project` scopes
(Settings → Secrets and variables → Actions). Note: the NVIDIA enterprise
rejects classic PATs with a lifetime over 366 days, so set the token's
expiration to a year or less. Without a working token the dashboard still
works — it just can't see those fields, logs a warning, and treats no issue as
triaged.

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
