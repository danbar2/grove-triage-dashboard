# Triage boards

Jira-style Kanban boards for triaging the issues and PRs of several GitHub
repos, rebuilt every ~15 minutes by GitHub Actions (using the repo's stored
`PROJECTS_TOKEN`) and published on GitHub Pages. Read-only: refreshing the
page always serves the latest snapshot — no viewer tokens, no editing.

**Boards index:** https://danbar2.github.io/triage-boards/

| Board | Watched repo |
|---|---|
| [Grove](https://danbar2.github.io/triage-boards/grove/) | [ai-dynamo/grove](https://github.com/ai-dynamo/grove) |
| [KAI Scheduler](https://danbar2.github.io/triage-boards/kai/) | [kai-scheduler/KAI-Scheduler](https://github.com/kai-scheduler/KAI-Scheduler) |
| [Karta](https://danbar2.github.io/triage-boards/karta/) | [run-ai/karta](https://github.com/run-ai/karta) |

## What each board shows

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

The header has a PRs/Issues toggle and a **user filter** — a dropdown of every
person appearing on any item (author, assignee, requested reviewer, commenter,
committer), most-active first. Both filters are kept in the URL hash, so a
view like `…/grove/#pr&user=alice` can be bookmarked or shared.

## Configuration — adding a board

Everything lives in [`config.yaml`](config.yaml): shared `defaults` (ignored
bots, `stale_days: 80`, the triage-field rule) and a `projects` map with one
entry per board. The key is the URL slug; each project sets its watched repo,
its maintainers list, and may override any default.

To add a project, add an entry under `projects:` and push — the next workflow
run publishes it at `/<slug>/` and lists it on the index page.

## Data freshness

Every board is a static snapshot regenerated every ~15 minutes by the
scheduled workflow (GitHub Actions cron is best-effort, so expect 15–25
minutes in practice). The header shows the snapshot's age. The token used for
fetching lives only in the repo's Actions secrets — the pages themselves
contain no credentials and perform no GitHub operations. For an immediate
rebuild, run the workflow manually (Actions tab → "Run workflow", or
`gh workflow run dashboard.yml -R danbar2/triage-boards`).

### Project fields (Priority / Severity)

The type comes from GitHub's native issue type; Priority and Severity are read
from the issue's GitHub Project (v2) single-select fields named `Priority` and
`Severity` (case-insensitive, any linked project — e.g. Grove's roadmap board
and [run-ai/projects/7](https://github.com/orgs/run-ai/projects/7) for Karta).
Reading Projects v2 requires a token with the `read:project` scope, which
neither the default Actions token nor a plain `repo` PAT has. To enable the
check, add a repo secret named **`PROJECTS_TOKEN`** containing a PAT with
`repo` + `read:project` scopes (Settings → Secrets and variables → Actions),
authorized for every org whose projects it must read. Note: the NVIDIA
enterprise rejects classic PATs with a lifetime over 366 days, so set the
token's expiration to a year or less. Without a working token the boards still
work — they just can't see those fields, log a warning, and treat no issue as
triaged.

## Running locally

```sh
pip install requests pyyaml
GITHUB_TOKEN=$(gh auth token) python generate.py          # all boards
GITHUB_TOKEN=$(gh auth token) python generate.py karta    # just one
open dist/index.html
```

## Refresh

The workflow also runs on every push to `main` and can be triggered manually:

```sh
gh workflow run dashboard.yml -R danbar2/triage-boards
```

Each board publishes a `data.json` next to its page (e.g.
[`grove/data.json`](https://danbar2.github.io/triage-boards/grove/data.json))
for building other views.
