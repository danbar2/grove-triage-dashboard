#!/usr/bin/env python3
"""Generate the Grove triage dashboard.

Fetches open issues/PRs from GitHub via GraphQL, computes the last
*meaningful* activity per item (comments, reviews, commits, force-pushes,
review-thread replies — label/milestone/assignment churn is ignored),
classifies each item by whose turn it is, and renders a static HTML
dashboard plus a data.json into dist/.
"""

import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

API = "https://api.github.com/graphql"

PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt
        author { login }
        labels(first: 10) { nodes { name } }
        reviewDecision
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        timelineItems(last: 60, itemTypes: [
          ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT,
          HEAD_REF_FORCE_PUSHED_EVENT, READY_FOR_REVIEW_EVENT]) {
          nodes {
            __typename
            ... on IssueComment { createdAt author { login } }
            ... on PullRequestReview { createdAt author { login } state }
            ... on PullRequestCommit {
              commit { committedDate author { user { login } } }
            }
            ... on HeadRefForcePushedEvent { createdAt actor { login } }
            ... on ReadyForReviewEvent { createdAt actor { login } }
          }
        }
        reviewThreads(first: 100) {
          nodes {
            isResolved
            comments(last: 1) { nodes { createdAt author { login } } }
          }
        }
      }
    }
  }
}
"""

# %s is replaced with the project-fields fragment when the token has
# read:project scope, empty string otherwise.
ISSUE_QUERY_TEMPLATE = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 50, after: $cursor,
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt
        author { login }
        issueType { name }
        labels(first: 10) { nodes { name } }
        timelineItems(last: 60, itemTypes: [ISSUE_COMMENT]) {
          nodes {
            __typename
            ... on IssueComment { createdAt author { login } }
          }
        }
        %s
      }
    }
  }
}
"""

PROJECT_FIELDS_FRAGMENT = """
        projectItems(first: 10, includeArchived: false) {
          nodes {
            fieldValues(first: 30) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2FieldCommon { name } }
                }
              }
            }
          }
        }
"""

SECTIONS = [
    ("needs_first_response", "Needs first response",
     "No maintainer has engaged yet."),
    ("awaiting_maintainer", "Awaiting maintainer",
     "The author/community acted last (replied, pushed, resolved threads) — your working queue."),
    ("awaiting_author", "Awaiting author",
     "A maintainer responded last — the ball is with the author."),
    ("triaged", "Triaged backlog",
     "Type, priority and severity are set — already triaged, no first response needed."),
    ("stale", "Stale",
     None),  # description filled in with stale_days at render time
]


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def gql(session, query, variables):
    r = session.post(API, json={"query": query, "variables": variables}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def fetch_all(session, query, variables, path):
    """Paginate through repository.<path> and return all nodes."""
    nodes, cursor = [], None
    while True:
        data = gql(session, query, {**variables, "cursor": cursor})
        conn = data["repository"][path]
        nodes.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = conn["pageInfo"]["endCursor"]


def is_bot(login, bots):
    if not login:
        return False
    return login.lower() in bots or login.lower().endswith("[bot]")


def collect_events(node, is_pr, bots):
    """Return list of (timestamp, actor_login, description) meaningful events."""
    author = (node.get("author") or {}).get("login") or "ghost"
    events = [(parse_ts(node["createdAt"]), author, "opened")]

    for item in node["timelineItems"]["nodes"]:
        t = item["__typename"]
        if t == "IssueComment":
            actor = (item.get("author") or {}).get("login")
            events.append((parse_ts(item["createdAt"]), actor, "commented"))
        elif t == "PullRequestReview":
            actor = (item.get("author") or {}).get("login")
            state = item.get("state", "")
            desc = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes",
                    "COMMENTED": "reviewed", "DISMISSED": "review dismissed"}.get(state, "reviewed")
            events.append((parse_ts(item["createdAt"]), actor, desc))
        elif t == "PullRequestCommit":
            commit = item["commit"]
            user = ((commit.get("author") or {}).get("user") or {}).get("login")
            events.append((parse_ts(commit["committedDate"]), user or author, "pushed a commit"))
        elif t == "HeadRefForcePushedEvent":
            actor = (item.get("actor") or {}).get("login")
            events.append((parse_ts(item["createdAt"]), actor, "force-pushed"))
        elif t == "ReadyForReviewEvent":
            actor = (item.get("actor") or {}).get("login")
            events.append((parse_ts(item["createdAt"]), actor, "marked ready for review"))

    if is_pr:
        for thread in node.get("reviewThreads", {}).get("nodes", []):
            for c in thread["comments"]["nodes"]:
                actor = (c.get("author") or {}).get("login")
                events.append((parse_ts(c["createdAt"]), actor, "replied in a review thread"))

    return [(ts, actor, desc) for ts, actor, desc in events if not is_bot(actor, bots)]


def classify(node, is_pr, maintainers, bots, stale_cutoff, triaged_when_set):
    author = (node.get("author") or {}).get("login") or "ghost"
    events = collect_events(node, is_pr, bots)
    if not events:
        events = [(parse_ts(node["createdAt"]), author, "opened")]
    events.sort(key=lambda e: e[0])
    last_ts, last_actor, last_desc = events[-1]

    maintainer_engaged = any(
        (a or "").lower() in maintainers and (a or "").lower() != author.lower()
        for _, a, _ in events
    )

    if last_actor and last_actor.lower() in maintainers and last_actor.lower() != author.lower():
        state = "awaiting_author"
    elif not maintainer_engaged:
        state = "needs_first_response"
    else:
        state = "awaiting_maintainer"

    # Issues whose triage fields (config: triaged_when_set) are all filled in
    # are considered triaged: no first response needed even without a comment.
    issue_type = (node.get("issueType") or {}).get("name")
    fields = {}
    for pi in ((node.get("projectItems") or {}).get("nodes") or []):
        for fv in pi["fieldValues"]["nodes"]:
            fname = ((fv.get("field") or {}).get("name") or "").lower()
            if fname and fv.get("name"):
                fields[fname] = fv["name"]
    triage_values = {"issue_type": issue_type, **fields}
    if (state == "needs_first_response" and triaged_when_set
            and all(triage_values.get(f) for f in triaged_when_set)):
        state = "triaged"

    section = "stale" if last_ts < stale_cutoff else state

    unresolved = None
    ci = None
    if is_pr:
        threads = node.get("reviewThreads", {}).get("nodes", [])
        unresolved = sum(1 for t in threads if not t["isResolved"])
        commits = node.get("commits", {}).get("nodes", [])
        if commits and commits[0]["commit"].get("statusCheckRollup"):
            ci = commits[0]["commit"]["statusCheckRollup"]["state"]

    return {
        "type": "pr" if is_pr else "issue",
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "author": author,
        "created_at": node["createdAt"],
        "is_draft": node.get("isDraft", False),
        "labels": [l["name"] for l in node["labels"]["nodes"]],
        "issue_type": issue_type,
        "priority": fields.get("priority"),
        "severity": fields.get("severity"),
        "project_fields": fields,
        "review_decision": node.get("reviewDecision"),
        "ci_state": ci,
        "unresolved_threads": unresolved,
        "last_activity_at": last_ts.isoformat(),
        "last_activity_by": last_actor or "ghost",
        "last_activity_desc": last_desc,
        "state": state,
        "section": section,
    }


def rel_time(ts, now):
    delta = now - parse_ts(ts)
    days = delta.days
    if days >= 365:
        return f"{days // 365}y {days % 365 // 30}mo ago"
    if days >= 60:
        return f"{days // 30}mo ago"
    if days >= 1:
        return f"{days}d ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    return f"{max(delta.seconds // 60, 1)}m ago"


def chips(item):
    out = []
    if item["type"] == "pr":
        out.append('<span class="chip pr">PR</span>')
    else:
        out.append('<span class="chip issue">Issue</span>')
    if item["is_draft"]:
        out.append('<span class="chip draft">draft</span>')
    rd = item["review_decision"]
    if rd == "APPROVED":
        out.append('<span class="chip ok">approved</span>')
    elif rd == "CHANGES_REQUESTED":
        out.append('<span class="chip warn">changes requested</span>')
    ci = item["ci_state"]
    if ci in ("FAILURE", "ERROR"):
        out.append('<span class="chip bad">CI failing</span>')
    elif ci == "PENDING":
        out.append('<span class="chip pending">CI pending</span>')
    if item["unresolved_threads"]:
        out.append(f'<span class="chip warn">{item["unresolved_threads"]} unresolved</span>')
    for key in ("issue_type", "priority", "severity"):
        if item.get(key):
            out.append(f'<span class="chip">{html.escape(item[key])}</span>')
    return " ".join(out)


def state_chip(state):
    labels = {
        "needs_first_response": ("needs first response", "bad"),
        "awaiting_maintainer": ("awaiting maintainer", "warn"),
        "awaiting_author": ("awaiting author", "muted"),
        "triaged": ("triaged", "muted"),
    }
    text, cls = labels.get(state, (state, "muted"))
    return f'<span class="chip {cls}">{text}</span>'


def render_rows(items, now, show_state=False):
    rows = []
    for it in items:
        title = html.escape(it["title"])
        last_by = html.escape(it["last_activity_by"])
        desc = html.escape(it["last_activity_desc"])
        extra = f" {state_chip(it['state'])}" if show_state else ""
        rows.append(f"""
        <tr data-type="{it['type']}">
          <td class="num"><a href="{it['url']}">#{it['number']}</a></td>
          <td class="title"><a href="{it['url']}">{title}</a><div class="chips">{chips(it)}{extra}</div></td>
          <td class="author">{html.escape(it['author'])}</td>
          <td class="activity"><b>{last_by}</b> {desc}</td>
          <td class="when" title="{it['last_activity_at']}">{rel_time(it['last_activity_at'], now)}</td>
        </tr>""")
    return "\n".join(rows)


FILTER_SCRIPT = """
<script>
(function () {
  var buttons = document.querySelectorAll('.filter button');
  function applyFilter(f) {
    buttons.forEach(function (b) {
      b.classList.toggle('active', b.dataset.filter === f);
    });
    document.querySelectorAll('details.section').forEach(function (sec) {
      var visible = 0;
      sec.querySelectorAll('tbody tr[data-type]').forEach(function (tr) {
        var show = f === 'all' || tr.dataset.type === f;
        tr.style.display = show ? '' : 'none';
        if (show) visible++;
      });
      sec.querySelector('.empty-row').style.display = visible ? 'none' : '';
      sec.querySelector('.count').textContent = visible;
      var card = document.querySelector(
        '.card[data-section="' + sec.dataset.section + '"] .n');
      if (card) card.textContent = visible;
    });
    try { history.replaceState(null, '', f === 'all' ? location.pathname : '#' + f); } catch (e) {}
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () { applyFilter(b.dataset.filter); });
  });
  var initial = location.hash.replace('#', '');
  if (initial === 'pr' || initial === 'issue') applyFilter(initial);
})();
</script>"""


def render_html(items, cfg, now):
    by_section = {key: [] for key, _, _ in SECTIONS}
    for it in items:
        by_section[it["section"]].append(it)

    # Working queues: longest-waiting first. Awaiting author: most recent first.
    for key in ("needs_first_response", "awaiting_maintainer", "stale"):
        by_section[key].sort(key=lambda i: i["last_activity_at"])
    for key in ("awaiting_author", "triaged"):
        by_section[key].sort(key=lambda i: i["last_activity_at"], reverse=True)

    stale_desc = (f"No meaningful activity for {cfg['stale_days']}+ days "
                  "(in either direction).")
    sections_html = []
    for key, title, desc in SECTIONS:
        rows = by_section[key]
        desc = desc or stale_desc
        empty_style = ' style="display:none"' if rows else ""
        body = (render_rows(rows, now, show_state=(key == "stale"))
                + f'\n<tr class="empty-row"{empty_style}>'
                  '<td colspan="5" class="empty">Nothing here 🎉</td></tr>')
        open_attr = "" if key in ("awaiting_author", "triaged") else " open"
        sections_html.append(f"""
    <details class="section {key}" data-section="{key}"{open_attr}>
      <summary><h2>{title} <span class="count">{len(rows)}</span></h2><p>{desc}</p></summary>
      <table>
        <thead><tr><th>#</th><th>Title</th><th>Author</th><th>Last meaningful activity</th><th>When</th></tr></thead>
        <tbody>{body}</tbody>
      </table>
    </details>""")

    repo = f"{cfg['repo']['owner']}/{cfg['repo']['name']}"
    counts = {key: len(by_section[key]) for key, _, _ in SECTIONS}
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grove triage — {repo}</title>
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --fg: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922;
    --bad: #f85149; --pending: #a371f7;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
         font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; }}
  header h1 {{ margin: 0 0 4px; font-size: 22px; }}
  header .sub {{ color: var(--muted); margin-bottom: 20px; }}
  header a {{ color: var(--accent); text-decoration: none; }}
  .toolbar {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start;
              justify-content: space-between; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .filter {{ display: flex; border: 1px solid var(--border); border-radius: 8px;
             overflow: hidden; }}
  .filter button {{ background: var(--panel); color: var(--muted); border: none;
                    padding: 8px 16px; font: inherit; cursor: pointer; }}
  .filter button + button {{ border-left: 1px solid var(--border); }}
  .filter button.active {{ background: var(--border); color: var(--fg); font-weight: 600; }}
  .filter button:hover {{ color: var(--fg); }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
           padding: 12px 18px; min-width: 150px; }}
  .card .n {{ font-size: 26px; font-weight: 700; }}
  .card .l {{ color: var(--muted); font-size: 12px; }}
  .card.needs_first_response .n {{ color: var(--bad); }}
  .card.awaiting_maintainer .n {{ color: var(--warn); }}
  .card.awaiting_author .n {{ color: var(--muted); }}
  .card.triaged .n {{ color: var(--muted); }}
  .card.stale .n {{ color: var(--pending); }}
  details.section {{ background: var(--panel); border: 1px solid var(--border);
                     border-radius: 8px; margin-bottom: 20px; overflow: hidden; }}
  summary {{ cursor: pointer; padding: 14px 18px; list-style: none; }}
  summary::-webkit-details-marker {{ display: none; }}
  summary h2 {{ display: inline; font-size: 16px; margin: 0; }}
  summary p {{ display: inline; color: var(--muted); margin: 0 0 0 10px; font-size: 12px; }}
  .count {{ background: var(--border); border-radius: 10px; padding: 1px 9px;
            font-size: 12px; vertical-align: 2px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase;
        letter-spacing: .04em; padding: 8px 12px; border-top: 1px solid var(--border); }}
  td {{ padding: 10px 12px; border-top: 1px solid var(--border); vertical-align: top; }}
  td.num {{ white-space: nowrap; color: var(--muted); }}
  td a {{ color: var(--fg); text-decoration: none; font-weight: 600; }}
  td.num a {{ color: var(--accent); font-weight: 400; }}
  td a:hover {{ color: var(--accent); }}
  td.when {{ white-space: nowrap; color: var(--muted); }}
  td.author, td.activity {{ color: var(--muted); }}
  td.activity b {{ color: var(--fg); font-weight: 600; }}
  td.empty {{ color: var(--muted); text-align: center; padding: 22px; }}
  .chips {{ margin-top: 4px; }}
  .chip {{ display: inline-block; font-size: 11px; padding: 0 8px; border-radius: 10px;
           border: 1px solid var(--border); color: var(--muted); margin-right: 4px; }}
  .chip.pr {{ color: var(--ok); border-color: var(--ok); }}
  .chip.issue {{ color: var(--accent); border-color: var(--accent); }}
  .chip.ok {{ color: var(--ok); border-color: var(--ok); }}
  .chip.warn {{ color: var(--warn); border-color: var(--warn); }}
  .chip.bad {{ color: var(--bad); border-color: var(--bad); }}
  .chip.pending {{ color: var(--pending); border-color: var(--pending); }}
  .chip.draft {{ color: var(--muted); }}
  footer {{ color: var(--muted); font-size: 12px; margin-top: 8px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Grove triage dashboard</h1>
    <div class="sub">
      <a href="https://github.com/{repo}">{repo}</a> ·
      generated {now.strftime('%Y-%m-%d %H:%M UTC')} ·
      activity signal ignores labels/milestones/assignments ·
      <a href="data.json">data.json</a>
    </div>
  </header>
  <div class="toolbar">
    <div class="cards">
      <div class="card needs_first_response" data-section="needs_first_response"><div class="n">{counts['needs_first_response']}</div><div class="l">needs first response</div></div>
      <div class="card awaiting_maintainer" data-section="awaiting_maintainer"><div class="n">{counts['awaiting_maintainer']}</div><div class="l">awaiting maintainer</div></div>
      <div class="card awaiting_author" data-section="awaiting_author"><div class="n">{counts['awaiting_author']}</div><div class="l">awaiting author</div></div>
      <div class="card triaged" data-section="triaged"><div class="n">{counts['triaged']}</div><div class="l">triaged backlog</div></div>
      <div class="card stale" data-section="stale"><div class="n">{counts['stale']}</div><div class="l">stale ({cfg['stale_days']}d+)</div></div>
    </div>
    <div class="filter" role="group" aria-label="Filter by type">
      <button class="active" data-filter="all">All</button>
      <button data-filter="pr">PRs</button>
      <button data-filter="issue">Issues</button>
    </div>
  </div>
  {''.join(sections_html)}
  <footer>Refreshed twice a day (06:00 / 18:00 UTC) by GitHub Actions.
  Maintainers list lives in <code>config.yaml</code>.</footer>
</div>
{FILTER_SCRIPT}
</body>
</html>
"""


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required")

    cfg = yaml.safe_load(Path(__file__).with_name("config.yaml").read_text())
    maintainers = {m.lower() for m in cfg["maintainers"]}
    bots = {b.lower() for b in cfg.get("bots", [])}
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=cfg["stale_days"])
    variables = {"owner": cfg["repo"]["owner"], "name": cfg["repo"]["name"]}

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    prs = fetch_all(session, PR_QUERY, variables, "pullRequests")

    # Issues: try the project-fields query with PROJECTS_TOKEN first, then the
    # default token; a rejected/underscoped token must never break the build.
    issues = None
    full_query = ISSUE_QUERY_TEMPLATE % PROJECT_FIELDS_FRAGMENT
    for tok, label in ((os.environ.get("PROJECTS_TOKEN"), "PROJECTS_TOKEN"),
                       (token, "default token")):
        if not tok or issues is not None:
            continue
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {tok}"
        try:
            issues = fetch_all(s, full_query, variables, "issues")
        except RuntimeError as e:
            print(f"WARNING: project-fields issue query failed with {label}: {e}")
    if issues is None:
        issues = fetch_all(session, ISSUE_QUERY_TEMPLATE % "", variables, "issues")
    print(f"Fetched {len(prs)} open PRs, {len(issues)} open issues")

    triaged_when_set = [f.lower() for f in cfg.get("triaged_when_set", [])]
    items = ([classify(n, True, maintainers, bots, stale_cutoff, triaged_when_set)
              for n in prs]
             + [classify(n, False, maintainers, bots, stale_cutoff, triaged_when_set)
                for n in issues])

    if not any(it["priority"] or it["severity"] for it in items):
        print("NOTE: no Priority/Severity project field values visible on any "
              "issue. If the project does use them, the token cannot see the "
              "project items — set a PROJECTS_TOKEN secret with read:project.")

    dist = Path(__file__).with_name("dist")
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(render_html(items, cfg, now))
    (dist / "data.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "repo": cfg["repo"], "items": items}, indent=2))
    counts = {}
    for it in items:
        counts[it["section"]] = counts.get(it["section"], 0) + 1
    print(f"Wrote dist/index.html and dist/data.json — sections: {counts}")


if __name__ == "__main__":
    main()
