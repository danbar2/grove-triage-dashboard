#!/usr/bin/env python3
"""Generate the Grove triage board.

Fetches open issues/PRs from GitHub via GraphQL, computes the last
*meaningful* activity per item (comments, reviews, commits, force-pushes,
review-thread replies — label/milestone/assignment churn is ignored),
classifies each item by whose turn it is, and renders a Jira-style Kanban
board (static HTML + client-side JS) plus a data.json into dist/.

The board is read-only: no viewer tokens, no GitHub operations from the
page. Freshness comes from the scheduled workflow re-running this script
every ~15 minutes with the repo's stored PROJECTS_TOKEN.
"""

import json
import os
import sys
from collections import Counter
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
        id number title url createdAt
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
            id
            project { id title }
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

PROJECT_META_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      id title
      field(name: "Priority") {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      }
    }
  }
}
"""

SECTIONS = [
    ("needs_first_response", "Needs first response",
     "No maintainer has engaged yet."),
    ("awaiting_maintainer", "Awaiting maintainer",
     "The author/community acted last (replied, pushed, resolved threads) — the working queue."),
    ("awaiting_author", "Awaiting author",
     "A maintainer responded last — the ball is with the author."),
    ("triaged", "Triaged backlog",
     "Type and priority are set — already triaged, no first response needed."),
    ("stale", "Stale",
     "No meaningful activity for a long time (in either direction)."),
]

ISSUE_TYPES = ["Task", "Bug", "Feature"]


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
    project_items = []
    for pi in ((node.get("projectItems") or {}).get("nodes") or []):
        project_items.append({"id": pi["id"],
                              "project_id": (pi.get("project") or {}).get("id")})
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
        "id": node.get("id"),
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
        "project_items": project_items,
        "review_decision": node.get("reviewDecision"),
        "ci_state": ci,
        "unresolved_threads": unresolved,
        "last_activity_at": last_ts.isoformat(),
        "last_activity_by": last_actor or "ghost",
        "last_activity_desc": last_desc,
        "state": state,
        "section": section,
    }


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grove triage board</title>
<style>
  :root {
    --text: #172B4D; --subtle: #626F86; --bg: #F7F8F9; --colbg: #F1F2F4;
    --card: #FFFFFF; --hover: #FAFBFC; --line: #DCDFE4; --blue: #0C66E4;
    --red: #C9372C; --orange: #B65C02; --yellow: #946F00; --green: #216E4E;
    --purple: #5E4DB2; --sel-bg: #E9F2FF; --red-bg: #FFECEB; --or-bg: #FFF3EB;
    --yel-bg: #FFF7D6; --grn-bg: #DCFFF1; --pur-bg: #DFD8FD; --mut-bg: #DCDFE4;
    --shadow: 0 1px 1px rgba(9,30,66,.25), 0 0 1px rgba(9,30,66,.31);
  }
  body.dark {
    --text: #C7D1DB; --subtle: #8C9BAB; --bg: #161A1D; --colbg: #1D2125;
    --card: #22272B; --hover: #282E33; --line: #38414A; --blue: #579DFF;
    --red: #F87168; --orange: #FEA362; --yellow: #F5CD47; --green: #4BCE97;
    --purple: #9F8FEF; --sel-bg: #1C2B41; --red-bg: #42221F; --or-bg: #3A2C1F;
    --yel-bg: #332E1B; --grn-bg: #1C3329; --pur-bg: #2B273F; --mut-bg: #2C333A;
    --shadow: 0 1px 2px rgba(0,0,0,.5);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         transition: background .2s, color .2s; }
  a { color: var(--blue); text-decoration: none; }

  header { background: var(--card); border-bottom: 1px solid var(--line);
           padding: 16px 28px; display: flex; align-items: center; gap: 16px;
           flex-wrap: wrap; position: sticky; top: 0; z-index: 5; }
  header h1 { font-size: 20px; margin: 0; }
  header .sub { color: var(--subtle); font-size: 13px; }
  .spacer { flex: 1; }
  .seg { display: flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  .seg button { background: var(--card); border: none; padding: 8px 16px; font: inherit;
                color: var(--subtle); cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--line); }
  .seg button.active { background: var(--sel-bg); color: var(--blue); font-weight: 600; }
  .gear { background: var(--card); border: 1px solid var(--line); border-radius: 6px;
          padding: 8px 14px; font: inherit; cursor: pointer; color: var(--subtle); }

  .board { display: flex; gap: 16px; align-items: flex-start; padding: 28px 32px;
           overflow-x: auto; min-height: calc(100vh - 72px);
           justify-content: safe center; }
  .col { background: var(--colbg); border-radius: 12px; width: 330px; flex: none; }
  .colhead { display: flex; justify-content: space-between; align-items: center;
             padding: 14px 16px 8px; font-size: 12.5px; font-weight: 600;
             color: var(--subtle); text-transform: uppercase; letter-spacing: .03em; }
  .cnt { background: var(--mut-bg); border-radius: 10px; padding: 1px 9px; font-size: 12px; }
  .cnt.needs_first_response { background: var(--red-bg); color: var(--red); }
  .cnt.awaiting_maintainer { background: var(--or-bg); color: var(--orange); }
  .cnt.awaiting_author { background: var(--mut-bg); color: var(--subtle); }
  .cnt.triaged { background: var(--grn-bg); color: var(--green); }
  .cnt.stale { background: var(--pur-bg); color: var(--purple); }
  .cards { display: flex; flex-direction: column; gap: 10px; padding: 6px 10px 12px; }
  .empty { color: var(--subtle); text-align: center; padding: 20px 0 26px; font-size: 13px; }

  .card { background: var(--card); border-radius: 8px; padding: 12px 14px;
          box-shadow: var(--shadow); }
  .card:hover { background: var(--hover); }
  .card .title { display: block; color: var(--text); font-weight: 500; margin-bottom: 6px; }
  .card .title:hover { text-decoration: underline; }
  .badges { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
  .badge { font-size: 12px; padding: 1px 7px; border-radius: 4px; font-weight: 600; }
  .badge.ok { background: var(--grn-bg); color: var(--green); }
  .badge.warn { background: var(--yel-bg); color: var(--yellow); }
  .badge.bad { background: var(--red-bg); color: var(--red); }
  .badge.mut { background: var(--mut-bg); color: var(--subtle); }
  .meta { color: var(--subtle); font-size: 13px; margin-bottom: 8px; }
  .meta b { color: var(--text); font-weight: 600; }
  .foot { display: flex; justify-content: space-between; align-items: center; }
  .foot .left, .foot .right { display: flex; align-items: center; gap: 7px; }
  .key { color: var(--subtle); font-size: 13px; font-weight: 600; }
  .key:hover { text-decoration: underline; color: var(--blue); }
  .av { width: 22px; height: 22px; border-radius: 50%; }

  .ticon { width: 20px; height: 20px; border-radius: 4px; padding: 0;
           color: #fff; font-size: 12px; line-height: 20px; text-align: center;
           display: inline-block; font-weight: 700; }
  .ticon.pr { background: var(--purple); }
  .ticon.bug { background: #E2483D; }
  .ticon.task { background: #388BFF; }
  .ticon.feature { background: #63BA3C; }
  .ticon.none { background: var(--card); color: var(--subtle); border: 1px dashed #8590A2; }

  .prio { border-radius: 4px; font-size: 12px; font-weight: 700; padding: 1px 7px;
          color: var(--subtle); }
  .prio.p0 { color: var(--red); background: var(--red-bg); }
  .prio.p1 { color: var(--orange); background: var(--or-bg); }
  .prio.p2 { color: var(--yellow); background: var(--yel-bg); }
</style>
</head>
<body>
<header>
  <h1>Grove triage board</h1>
  <div class="sub" id="sub"></div>
  <div class="spacer"></div>
  <div class="seg" id="seg">
    <button class="active" data-filter="all">All</button>
    <button data-filter="pr">PRs</button>
    <button data-filter="issue">Issues</button>
  </div>
  <button class="gear" id="theme" title="Toggle dark/light mode">🌙</button>
</header>
<div class="board" id="board"></div>

<script>window.DASH = __PAYLOAD__;</script>
<script>
(function () {
  'use strict';
  var D = window.DASH;
  var filter = 'all';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function relTime(iso) {
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    var days = Math.floor(s / 86400);
    if (days >= 365) return Math.floor(days / 365) + 'y ago';
    if (days >= 60) return Math.floor(days / 30) + 'mo ago';
    if (days >= 1) return days + 'd ago';
    var h = Math.floor(s / 3600);
    if (h >= 1) return h + 'h ago';
    if (s < 45) return 'just now';
    return Math.max(1, Math.floor(s / 60)) + 'm ago';
  }

  function badges(it) {
    var out = [];
    if (it.is_draft) out.push(['draft', 'mut']);
    if (it.review_decision === 'APPROVED') out.push(['approved', 'ok']);
    if (it.review_decision === 'CHANGES_REQUESTED') out.push(['changes requested', 'warn']);
    if (it.ci_state === 'FAILURE' || it.ci_state === 'ERROR') out.push(['CI failing', 'bad']);
    if (it.ci_state === 'PENDING') out.push(['CI pending', 'mut']);
    if (it.unresolved_threads) out.push([it.unresolved_threads + ' unresolved', 'warn']);
    if (!out.length) return '';
    return '<div class="badges">' + out.map(function (b) {
      return '<span class="badge ' + b[1] + '">' + esc(b[0]) + '</span>';
    }).join('') + '</div>';
  }

  function typeIcon(it) {
    if (it.type === 'pr') return '<span class="ticon pr" title="Pull request">⇄</span>';
    var t = (it.issue_type || '').toLowerCase();
    var sym = {bug: '!', task: '✓', feature: '✦'}[t] || '+';
    return '<span class="ticon ' + (t || 'none') + '" title="Type: ' +
           esc(it.issue_type || 'not set') + '">' + sym + '</span>';
  }

  function prioBadge(it) {
    if (it.type === 'pr' || !it.priority) return '';
    return '<span class="prio ' + esc(it.priority.toLowerCase()) + '" title="Priority">' +
           esc(it.priority) + '</span>';
  }

  function cardHTML(it) {
    return '<div class="card">' +
      '<a class="title" href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.title) + '</a>' +
      badges(it) +
      '<div class="meta"><b>' + esc(it.last_activity_by) + '</b> ' + esc(it.last_activity_desc) +
        ' · <span title="' + esc(it.last_activity_at) + '">' + relTime(it.last_activity_at) + '</span></div>' +
      '<div class="foot">' +
        '<span class="left">' + typeIcon(it) +
          '<a class="key" href="' + esc(it.url) + '" target="_blank" rel="noopener">#' + it.number + '</a></span>' +
        '<span class="right">' + prioBadge(it) +
          '<img class="av" loading="lazy" src="https://github.com/' + encodeURIComponent(it.author) +
          '.png?size=40" title="' + esc(it.author) + '" alt="" ' +
          'onerror="this.style.display=\\'none\\'"></span>' +
      '</div></div>';
  }

  function render() {
    document.getElementById('board').innerHTML = D.sections.map(function (sec) {
      var items = D.items.filter(function (i) {
        return i.section === sec.key && (filter === 'all' || i.type === filter);
      }).sort(function (a, b) { return b.last_activity_at.localeCompare(a.last_activity_at); });
      return '<div class="col">' +
        '<div class="colhead" title="' + esc(sec.desc) + '"><span>' + esc(sec.title) +
        '</span><span class="cnt ' + sec.key + '">' + items.length + '</span></div>' +
        '<div class="cards">' + (items.map(cardHTML).join('') ||
          '<div class="empty">Nothing here 🎉</div>') + '</div></div>';
    }).join('');
  }

  // ---- filters ----
  var segButtons = document.querySelectorAll('#seg button');
  segButtons.forEach(function (b) {
    b.addEventListener('click', function () {
      filter = b.dataset.filter;
      segButtons.forEach(function (x) { x.classList.toggle('active', x === b); });
      try {
        history.replaceState(null, '', filter === 'all' ? location.pathname : '#' + filter);
      } catch (e) {}
      render();
    });
  });
  var initial = location.hash.replace('#', '');
  if (initial === 'pr' || initial === 'issue') {
    filter = initial;
    segButtons.forEach(function (x) { x.classList.toggle('active', x.dataset.filter === initial); });
  }

  // ---- theme ----
  var themeBtn = document.getElementById('theme');
  function applyTheme(t) {
    document.body.classList.toggle('dark', t === 'dark');
    themeBtn.textContent = t === 'dark' ? '☀️' : '🌙';
  }
  var theme = localStorage.getItem('grove_dash_theme') ||
    (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark' : 'light');
  applyTheme(theme);
  themeBtn.addEventListener('click', function () {
    theme = (theme === 'dark') ? 'light' : 'dark';
    localStorage.setItem('grove_dash_theme', theme);
    applyTheme(theme);
  });

  // ---- header sub ----
  var gen = new Date(D.generated_at);
  document.getElementById('sub').innerHTML =
    '<a href="https://github.com/' + D.repo.owner + '/' + D.repo.name + '" target="_blank" rel="noopener">' +
    D.repo.owner + '/' + D.repo.name + '</a> · updated ' + relTime(D.generated_at) +
    ' <span title="' + gen.toISOString() + '">(' + gen.toUTCString().slice(5, 22) + ' UTC)</span>' +
    ' · auto-refreshes every ~15 min · <a href="data.json">data.json</a>';

  render();
})();
</script>
</body>
</html>
"""


def render_html(items, cfg, now, project_meta, triaged_when_set):
    stale_desc = (f"No meaningful activity for {cfg['stale_days']}+ days "
                  "(in either direction).")
    payload = {
        "generated_at": now.isoformat(),
        "repo": cfg["repo"],
        "stale_days": cfg["stale_days"],
        "sections": [{"key": k, "title": t,
                      "desc": (stale_desc if k == "stale" else d)}
                     for k, t, d in SECTIONS],
        "items": items,
    }
    blob = json.dumps(payload).replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__PAYLOAD__", blob)




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
    project_session = None
    full_query = ISSUE_QUERY_TEMPLATE % PROJECT_FIELDS_FRAGMENT
    for tok, label in ((os.environ.get("PROJECTS_TOKEN"), "PROJECTS_TOKEN"),
                       (token, "default token")):
        if not tok or issues is not None:
            continue
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {tok}"
        try:
            issues = fetch_all(s, full_query, variables, "issues")
            project_session = s
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

    # Project metadata for client-side priority editing: the most common
    # project across issues, plus its Priority single-select field/options.
    project_meta = None
    main_project_id = None
    if project_session is not None:
        pcount = Counter(pi["project_id"] for it in items
                         for pi in it["project_items"] if pi["project_id"])
        if pcount:
            main_project_id = pcount.most_common(1)[0][0]
            try:
                node = gql(project_session, PROJECT_META_QUERY,
                           {"id": main_project_id})["node"]
                fld = node.get("field")
                if fld and fld.get("options"):
                    project_meta = {"id": node["id"], "title": node["title"],
                                    "priority_field": {"id": fld["id"],
                                                       "options": fld["options"]}}
            except RuntimeError as e:
                print(f"WARNING: could not read project Priority field: {e}")
    if project_meta is None:
        print("NOTE: priority editing disabled — no project field metadata "
              "available to this build.")

    for it in items:
        pitems = it.pop("project_items")
        it["project_item_id"] = next(
            (p["id"] for p in pitems if p["project_id"] == main_project_id), None)

    if not any(it["priority"] or it["severity"] for it in items):
        print("NOTE: no Priority/Severity project field values visible on any "
              "issue. If the project does use them, the token cannot see the "
              "project items — set a PROJECTS_TOKEN secret with read:project.")

    dist = Path(__file__).with_name("dist")
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(
        render_html(items, cfg, now, project_meta, triaged_when_set))
    (dist / "data.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "repo": cfg["repo"],
         "project": project_meta, "items": items}, indent=2))
    counts = Counter(it["section"] for it in items)
    print(f"Wrote dist/index.html and dist/data.json — sections: {dict(counts)}")


if __name__ == "__main__":
    main()
