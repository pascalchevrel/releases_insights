#!/usr/bin/env python3
"""
Posts the state of the open Firefox release regressions to Slack.

Both messages are built from the same bug set, the REO tab of
https://bugdash.moz.tools/, and neither needs Bugzilla credentials; every query
is over public data. Which one is posted depends on the flag:

no flag   The cycle summary, runs twice a week by
          .github/workflows/reo-regression-slack.yml. For Release, Beta and
          Nightly it reports two bug lists:

          - "new regressions" carry the regression keyword and are affected in
            version N while N-1 is unaffected or unknown, so they regressed
            during this cycle
          - "carry over regressions" are the same query negated: N-1 has a real
            status, so the bug was already there

          Those two partition every open regression affecting N. Each count is
          broken down by severity, with New Regressions also broken down by
          owning team. Beta and Nightly get a working day countdown to the end
          of their cycle.

--daily   The action required message, run every weekday by
          .github/workflows/reo-regression-daily-slack.yml. The same
          regressions with the new/carry over split dropped and the three
          channels merged into one deduplicated list, reporting only the bugs
          stuck long enough to need a nudge: high severity with nobody on them
          (UNASSIGNED_EXEMPT_PRODUCTS exempt), no severity decision, or an unanswered
          needinfo. It ends with
          bugdash's Burndown list per version, Beta and Release only, cut down
          to the fixes nobody has asked to uplift. Each line is broken down by
          owning team.

Every count in either message links to a Bugzilla list of exactly the bugs
counted.

Requires the SLACK_WEBHOOK_URL environment variable (set from the
REO_SLACK_WEBHOOK repo secret in the workflow).

Env vars for testing:
  DRY_RUN=1           print the message instead of posting it. Accepts
                      1/true/yes/on to enable and 0/false/no/off or empty to
                      disable; anything else is an error rather than a guess.
"""
import argparse
import datetime
import functools
import os
import re
import sys
import urllib.parse

from lib.env import env_flag
from lib.fetch import fetch_json
from lib.schedule import (
    channel_versions,
    fetch_schedule,
    parse_date,
    today as utc_today,
)
from lib.slack import post_to_slack

RELEASE_PAGE_URL = "https://whattrainisitnow.com/release/?version={}"

WELLNESS_API_URL = "https://whattrainisitnow.com/api/wellness/days/"

BZ_REST_URL = "https://bugzilla.mozilla.org/rest/bug"
BZ_BUGLIST_URL = "https://bugzilla.mozilla.org/buglist.cgi"
BZ_PRODUCT_URL = (
    "https://bugzilla.mozilla.org/rest/product?type=accessible"
    "&include_fields=name,components.name,components.team_name"
)

# Every Bugzilla classification except Graveyard, which holds the ~100 retired
# products. Same list bugdash's REO queries use.
CLASSIFICATIONS = [
    "Client Software",
    "Components",
    "Developer Infrastructure",
    "Other",
    "Server Software",
]

# The severities we call out: the most serious ones, and the one that means no
# triage decision has been made yet. Bugs are filtered on these locally, so the
# values have to be exactly what Bugzilla reports in a bug's severity field,
# which is case sensitive and not always what the same value looks like in a
# search: "N/A" comes back from the API where a query matches it as "n/a". Only
# "--" counts as missing here; N/A is a decision, not the absence of one.
HIGH_SEVERITIES = ("S1", "S2")
MISSING_SEVERITIES = ("--",)

# Bugzilla's placeholder assignee: the bug sits in a component, but nobody has
# taken it on. Components with a real default assignee are left out of this, so
# a bug parked on a triage owner counts as assigned.
NOBODY = "nobody@mozilla.org"

# Products where an unassigned high severity bug is not something to nag about,
# so they are left out of the "S2+ unassigned" bucket alone. Web Compatibility
# bugs S2 definition does not follow the regression severity definition
#
# Matched on the product name exactly as Bugzilla reports it, and only against
# this one bucket: a Web Compatibility bug with no severity or an unanswered
# needinfo is still stuck in the way those buckets mean.
UNASSIGNED_EXEMPT_PRODUCTS = ("Web Compatibility",)

# How long a bug has to have been stuck before the daily message nags about it.
# Long enough that a bug filed or touched during yesterday's working day is left
# alone, short enough that nothing sits unnoticed for a second day.
#
# It ages bugs from a fixed point in the past rather than over a window, so a
# quiet weekend doesn't hide anything: a bug that went stale on Friday is still
# in Monday's message, and stays there until someone acts on it.
STUCK_HOURS = 24

# The uplift approval flag per channel. A fix only reaches Beta or Release by
# being uplifted, so a burndown bug carrying neither is a fix that will not ship
# in the version it is marked as affecting.
#
# The flag is matched by name alone, so any state of it counts as asked: pending
# (?), granted (+) and denied (-) alike. Matching only a pending request would
# put a bug back on the list the moment its uplift was approved, since the flag
# stops being pending then and the fix has yet to land, and would keep a denied
# one on the list for good.
#
# Nightly is where fixes land, so it needs no uplift and gets no burndown line.
# The order here is the order the lines appear in.
#
# A channel added here also needs a version from channel_versions(), which covers
# Release, Beta and Nightly only. One without a version is skipped with a note on
# stderr rather than reported.
UPLIFT_FLAGS = {
    "beta": "approval-mozilla-beta",
    "release": "approval-mozilla-release",
}

HEADING = "REO release regression status:"

# The daily title leads with what makes it different from the twice weekly status
# summary, rather than trailing it. Slack cuts a long title off in notification
# previews and the eye reads from the left, so a title starting "REO release
# regression" like the other one would be indistinguishable at a glance. It is
# also the fallback text of the message, which is what those previews show.
#
# Slack allows 150 characters in a header block, which this is nowhere near.
DAILY_HEADING = "Action needed: REO release regressions"

# Sits under the heading in a context block: small, grey, and read as a label on
# the message rather than as part of it. Says the message is a recurring one, so
# a reader who has not seen it before knows it is not an incident.
DAILY_CADENCE = "Daily update"

# Follows the daily heading. Says the one thing every line below has in common,
# so the bullets don't each have to explain themselves, and points each team at
# the sub-bullets, which is where the message asks anything of anyone.
#
# What the buckets share is that none of them is waiting on the work: each is
# waiting on an action, which is what makes the message
# worth sending daily and what separates it from the twice weekly summary of how
# the cycle is going.
#
# The age is given here as a round number and again on each bullet, where it is
# also said what the age is counted from, as that differs per bucket.
DAILY_INTRO = (
    "These release regressions are waiting on activity and fall into the urgent "
    "category. "
    f"They have been pending for longer than {STUCK_HOURS} hours. "
    "Please take a look where one of your teams is listed."
)

# Shown instead of dropping a channel entirely, so a silent channel reads as
# good news rather than as the script having failed.
NOTHING_TO_REPORT = "•  No open release regressions"

# The same, for a day where every daily bucket came out empty.
NOTHING_STUCK = "•  Nothing needs attention"

# For a component with no team_name, or one missing from the mapping entirely.
# Every component had a team when this was written, so this is only a guard
# against silently dropping bugs out of the per-team line.
UNKNOWN_TEAM = "Unknown team"

# A Slack section block holds at most 3000 characters.
SECTION_LIMIT = 3000

# Above this a snapshot URL is dropped in favour of the (fixed length) query
# URL, so that one very long bug list can't push a section over SECTION_LIMIT.
MAX_SNAPSHOT_URL = 2000

# Stands in for a milestone key, as the last beta is numbered differently from
# one version to the next (beta_10 for 154, beta_5 under the 2 week cadence).
LAST_BETA = "last_beta"

# The milestone that ends each channel's cycle, and the cycle's name. Both the
# countdown ("End of Beta ...") and the finished line ("Beta cycle finished") are
# built from that one name, so they can't drift apart. Release has no equivalent
# deadline, so it gets no countdown.
CYCLE_ENDS = {
    "beta": ("Beta", LAST_BETA),
    "nightly": ("Nightly", "merge_day"),
}

# Custom emoji in the Mozilla workspace, one per channel. A name that doesn't exist
# there renders as the literal :name: rather than failing, so these have to stay
# in step with the workspace.
CHANNEL_EMOJI = {
    "release": ":firefox-browser:",
    "beta": ":beta-browser:",
    "nightly": ":nightly-browser:",
}

# Bugzilla searches do real work, so they get longer than the small JSON APIs.
BZ_TIMEOUT_SECONDS = 60

# What each message needs back from a bug search. The cycle summary only splits
# its counts by severity and team; the daily message also has to age each bug,
# and the three timestamps it can age one from all live on the bug itself, so
# asking for them keeps it to the same one request per version.
SUMMARY_FIELDS = "id,severity,product,component"
DAILY_FIELDS = f"{SUMMARY_FIELDS},assigned_to,creation_time,last_change_time,flags"
BURNDOWN_FIELDS = f"{SUMMARY_FIELDS},cf_last_resolved"

# Slack renders this back as >. Sending the character itself would work where it
# is used now, but it ends a link's label at the first > and opens a blockquote at
# the start of a line, so a label or bullet reworded around it would break in ways
# that are easy to miss. The entity is never wrong.
GREATER_THAN = "&gt;"

# Slack has no nested lists in message text, so indent sub-bullets by hand.
# Non-breaking spaces, as Slack collapses runs of regular ones.
SUB_BULLET = "    ◦ "


@functools.cache
def wellness_days() -> frozenset[datetime.date]:
    """Fetch the days off that don't count as working days."""
    return frozenset(
        datetime.date.fromisoformat(day) for day in fetch_json(WELLNESS_API_URL)
    )


def work_days_until(end: datetime.date) -> int:
    """
    Count working days between today and end, end excluded.

    Mirrors ReleaseInsights\\Duration::workDays() so this agrees with the
    countdowns on the release pages: weekends, wellness days and the current
    day are all left out.
    """
    today = utc_today()
    days = (end - today).days
    if days <= 0:
        return 0

    # Counting from tomorrow is what leaves the current day out.
    return sum(
        1
        for offset in range(1, days)
        if (day := today + datetime.timedelta(days=offset)).weekday() < 5  # Mon-Fri
        and day not in wellness_days()
    )


def regressions_query(version: int, carry_over: bool | None = None) -> dict:
    """
    Build the open regressions query for a version.

    Bugs with all of the following:
    - regression keyword
    - open (unresolved)
    - status-firefox{version} is affected
    Bugs with any of the following are ignored:
    - tracking-firefox{version} is -
    - stalled or intermittent-failure keywords
    - within the Testing product

    carry_over adds a condition on the previous version, splitting that set in
    two. False keeps the bugs where status-firefox{version - 1} is one of
    unaffected, ? or ---, so they regressed during this cycle; True negates it,
    leaving the ones that were already there. The two therefore partition every
    open regression affecting the version, and the default of None asks for that
    whole set instead of one side of it.

    Field numbering is Bugzilla's boolean charts: f/o/v are the field, operator
    and value for a numbered condition, OP and CP open and close a group, j sets
    how a group joins (OR here, AND otherwise) and n negates. The gap at f7 comes
    from bugdash and is harmless, as Bugzilla ignores unused numbers.
    """
    query = {
        "classification": CLASSIFICATIONS,
        "keywords": "regression",
        "keywords_type": "allwords",
        "resolution": "---",
        "f1": f"cf_status_firefox{version}",
        "o1": "equals",
        "v1": "affected",
        "f8": f"cf_tracking_firefox{version}",
        "o8": "notequals",
        "v8": "-",
        "f9": "product",
        "o9": "notequals",
        "v9": "Testing",
        "f10": "keywords",
        "o10": "nowordssubstr",
        "v10": "stalled,intermittent-failure",
    }

    if carry_over is None:
        return query

    # Conditions are matched up by their number, so leaving these out above and
    # adding them here changes nothing but the order they appear in the URL.
    previous = version - 1
    query |= {
        "f2": "OP",
        "j2": "OR",
        "f3": f"cf_status_firefox{previous}",
        "o3": "equals",
        "v3": "unaffected",
        "f4": f"cf_status_firefox{previous}",
        "o4": "equals",
        "v4": "?",
        "f5": f"cf_status_firefox{previous}",
        "o5": "equals",
        "v5": "---",
        "f6": "CP",
    }

    if carry_over:
        # n2 attaches to the OP at f2, so it negates the whole f3-f5 group rather
        # than just the first condition in it.
        query["n2"] = "1"

    return query


def burndown_query(version: int, uplift_flag: str) -> dict:
    """
    Build the burndown query for a version, less the bugs already asking to uplift.

    Bugs with all of the following:
    - resolved as fixed
    - status-firefox{version} is affected or fix-optional
    - any of:
      - crash, regression, leak, topcrash, assertion or dataloss keywords
      - in a security group
      - tracking-firefox{version} is +, ? or blocking
    Bugs with any of the following are ignored:
    - within the Testing product
    - an uplift request against the channel, in any state

    All but the last of those is bugdash's Burndown list, kept in step with
    app/buglists/burndown.mjs there. Its numbering gaps at f5, f8 and f10 are
    copied along with the rest, as Bugzilla ignores unused numbers.

    The uplift request is a flag on an attachment, and the only way a bug search
    will report those is to send back every attachment with it, so it is left to
    Bugzilla rather than filtered here. flagtypes.name matches the flags on a
    bug's attachments as well as those on the bug itself, on name and state
    together, so matching the bare name catches the request whatever became of
    it. n11 negates that, leaving the fixes nobody has asked to uplift.
    """
    return {
        "classification": CLASSIFICATIONS,
        "resolution": "FIXED",
        "f1": f"cf_status_firefox{version}",
        "o1": "anywords",
        "v1": "affected optional",
        "j2": "OR",
        "f2": "OP",
        "f3": "keywords",
        "o3": "anywords",
        "v3": "crash regression leak topcrash assertion dataloss",
        "f4": "bug_group",
        "o4": "substring",
        "v4": "sec",
        "f6": f"cf_tracking_firefox{version}",
        "o6": "anywordssubstr",
        "v6": "+ ? blocking",
        "f7": "CP",
        "f9": "product",
        "o9": "notequals",
        "v9": "Testing",
        "f11": "flagtypes.name",
        "o11": "substring",
        "v11": uplift_flag,
        "n11": "1",
    }


def with_severities(query: dict, severities: tuple[str, ...]) -> dict:
    """
    Narrow a query to some severities, for a link that stays live.

    The counts themselves are filtered locally, so this is only needed to build a
    URL when a bug list is too long to link by id. Slot 11 is free because the
    REO queries stop at f10.
    """
    return {
        **query,
        "f11": "bug_severity",
        "o11": "anyexact",
        "v11": ", ".join(severities),
    }


@functools.cache
def component_teams() -> dict[tuple[str, str], str]:
    """
    Map every (product, component) to the team that owns it.

    team_name is a Bugzilla field on components, the same one bugdash's Teams
    filter uses. One request covers every product, around 120KB for 2000-odd
    components, which is why it's cached for the life of the run.
    """
    products = fetch_json(BZ_PRODUCT_URL, BZ_TIMEOUT_SECONDS)["products"]

    return {
        (product["name"], component["name"]): component.get("team_name") or UNKNOWN_TEAM
        for product in products
        for component in product.get("components", [])
    }


def team_of(bug: dict) -> str:
    """The team owning a bug's component."""
    return component_teams().get((bug["product"], bug["component"]), UNKNOWN_TEAM)


def fetch_bugs(query: dict, fields: str = SUMMARY_FIELDS) -> list[dict]:
    """
    Return the requested fields of every bug matching a query.

    Fetching the bugs rather than asking for count_only is what lets the severity
    and team breakdowns be derived from one request, and lets each count link to
    the exact bugs behind it. limit=0 lifts Bugzilla's default page size.
    """
    params = {
        **query,
        "include_fields": fields,
        "limit": "0",
    }
    url = f"{BZ_REST_URL}?{urllib.parse.urlencode(params, doseq=True)}"
    return fetch_json(url, BZ_TIMEOUT_SECONDS)["bugs"]


def query_url(query: dict) -> str:
    """A Bugzilla URL that re-runs a query, so its results change over time."""
    return f"{BZ_BUGLIST_URL}?{urllib.parse.urlencode(query, doseq=True)}"


def snapshot_url(bugs: list[dict]) -> str:
    """
    A Bugzilla URL listing exactly these bugs, as bugdash's bug lists do.

    Linking the bug ids rather than the query means the list still matches the
    count in the message when it is read days later. order=bug_list keeps
    Bugzilla showing them in the order given rather than re-sorting.
    """
    ids = ",".join(str(bug["id"]) for bug in bugs)
    return f"{BZ_BUGLIST_URL}?bug_id={ids}&order=bug_list"


def bug_link(
    bugs: list[dict], label_template: str, fallback_query: dict | None = None
) -> str:
    """
    Format a non-empty bug list as a Slack link labelled with its count.

    label_template is formatted with the count, e.g. "{} New Regressions".
    If the snapshot URL comes out too long it is replaced by fallback_query, or
    left unlinked when there is no query for just these bugs. Team lines pass no
    fallback, as reproducing a team as a query means listing all its components.

    Callers are expected to skip empty lists: an empty bug_id would link to a
    broken list, and a count of zero is left out of the message anyway.
    """
    label = label_template.format(len(bugs))
    url = snapshot_url(bugs)

    if len(url) > MAX_SNAPSHOT_URL:
        if fallback_query is None:
            return label
        url = query_url(fallback_query)

    return f"<{url}|{label}>"


def milestone_date(schedule: dict, milestone: str) -> datetime.date:
    """
    The date of a milestone, resolving LAST_BETA to the highest numbered beta.

    The number of betas differs per version, so the last one has to be found
    rather than named. Sorting on the number matters: as strings, beta_9 would
    come after beta_10.
    """
    if milestone == LAST_BETA:
        betas = [key for key in schedule if re.fullmatch(r"beta_\d+", key)]
        milestone = max(betas, key=lambda key: int(key.removeprefix("beta_")))

    return parse_date(schedule[milestone])


def cycle_countdown(version: int, channel: str) -> str:
    """
    A countdown to the end of this version's time on the channel.

    Beta ends with the last beta build; Nightly ends on merge day, when the
    version moves to Beta. Release has no such deadline.

    The version numbers roll over on merge day, so the day of and the days after
    that deadline each only show up briefly, but they read badly as a countdown
    ("in 0 working days") and so get their own wording.
    """
    if channel not in CYCLE_ENDS:
        return ""

    cycle, milestone = CYCLE_ENDS[channel]
    label = f"End of {cycle}"
    end = milestone_date(fetch_schedule(str(version)), milestone)
    today = utc_today()

    if end < today:
        return f"{cycle} cycle finished"

    if end == today:
        return f"{label} today"

    if end == today + datetime.timedelta(days=1):
        return f"{label} {end:%Y-%m-%d} — tomorrow"

    days = work_days_until(end)
    working_days = "1 working day" if days == 1 else f"{days} working days"

    return f"{label} {end:%Y-%m-%d} in {working_days}"


def team_breakdown(bugs: list[dict]) -> str:
    """
    Count the bugs owned by each team, busiest team first.

    Every team is listed rather than just the top few, so that the line works
    as a nudge to each team that owns something.
    """
    by_team: dict[str, list[dict]] = {}
    for bug in bugs:
        by_team.setdefault(team_of(bug), []).append(bug)

    ranked = sorted(by_team.items(), key=lambda item: (-len(item[1]), item[0]))

    return ", ".join(bug_link(team_bugs, f"{{}} {team}") for team, team_bugs in ranked)


def parse_timestamp(value: str) -> datetime.datetime:
    """Parse a Bugzilla timestamp, e.g. '2026-08-16T23:40:15Z', as UTC."""
    return datetime.datetime.fromisoformat(value)


def stuck_since() -> datetime.datetime:
    """The moment a bug has to predate to count as stuck. See STUCK_HOURS."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(hours=STUCK_HOURS)


def needs_assignee(bug: dict, cutoff: datetime.datetime) -> bool:
    """
    A high severity bug nobody has taken on, aged from when it was filed.

    Bugs in UNASSIGNED_EXEMPT_PRODUCTS are left out: an unassigned bug there is
    not a bug that has been overlooked.
    """
    return (
        bug["severity"] in HIGH_SEVERITIES
        and bug["product"] not in UNASSIGNED_EXEMPT_PRODUCTS
        and bug["assigned_to"] == NOBODY
        and parse_timestamp(bug["creation_time"]) < cutoff
    )


def needs_severity(bug: dict, cutoff: datetime.datetime) -> bool:
    """
    A bug still waiting on a severity decision, aged from its last activity.

    Any change to the bug counts as activity, not just a triage one, so a bug
    with activity is left out until it goes quiet again. There are some limitations
    with this approach since the activity may be from someone outside the triage
    team asking questions or adjusting metadata.
    """
    return (
        bug["severity"] in MISSING_SEVERITIES
        and parse_timestamp(bug["last_change_time"]) < cutoff
    )


def needs_answer(bug: dict, cutoff: datetime.datetime) -> bool:
    """
    A bug with a needinfo nobody has answered, aged from when it was requested.

    A flag's creation_date is when the request now standing was made, so one that
    was answered and then asked again is aged from the second ask rather than the
    first. Several open requests on one bug still only count the bug once, and
    the oldest of them is what decides.
    """
    return any(
        flag["name"] == "needinfo"
        and flag["status"] == "?"
        and parse_timestamp(flag["creation_date"]) < cutoff
        for flag in bug.get("flags", [])
    )


# The daily message's buckets, in the order they appear in it: what makes a bug
# belong in one, the label its count goes in, and what its age is counted from.
# A bug can be in more than one, as they describe different things left undone
# rather than a state it is in.
#
# Every bucket names its own anchor because each is aged from a different
# timestamp. Left unsaid, the same "> 24 hours" on all four lines reads as one
# shared deadline, when a bug filed weeks ago and one that went quiet yesterday
# are being asked about for different reasons.
STUCK_BUCKETS = (
    (needs_assignee, "{} S2+ unassigned", "filed"),
    (needs_severity, "{} missing severity", "last change"),
    (needs_answer, "{} needinfo pending", "requested"),
)


def open_regressions(versions: dict[str, int]) -> list[dict]:
    """
    Every open release regression across the channels, each bug listed once.

    A regression affecting Nightly usually affects Beta and Release too, so the
    three queries overlap heavily: 62 hits covering 50 bugs when this was
    written. Keying on the bug id merges them, which is the point of the daily
    message — one list of what needs doing, not the same bug asked about three
    times. Where two channels disagree the last query wins, but the fields the
    buckets look at are all channel independent.
    """
    bugs: dict[int, dict] = {}
    for version in sorted(set(versions.values())):
        for bug in fetch_bugs(regressions_query(version), DAILY_FIELDS):
            bugs[bug["id"]] = bug

    return list(bugs.values())


def stuck_group(bugs: list[dict], label: str, anchor: str) -> str:
    """
    Build the bullet and team sub-bullet for one daily bucket.

    Only the count and what it counts are linked; the age that follows is left as
    plain text, so the blue runs as far as the thing being claimed and no further.
    Building that tail here is what keeps all four bullets the same shape.

    Empty buckets return an empty string and are left out of the message, so it
    stays a list of things to do rather than a scoreboard of zeros.

    Neither link gets a fallback query: the ageing is done here rather than by
    Bugzilla, so there is no query URL that reproduces either count.
    """
    if not bugs:
        return ""

    age = f", {GREATER_THAN} {STUCK_HOURS} hours since {anchor}"

    return f"• {bug_link(bugs, label)}{age}\n{SUB_BULLET}{team_breakdown(bugs)}"


def burndown_group(channel: str, version: int, cutoff: datetime.datetime) -> str:
    """
    Build the burndown bullet for one channel, aged from when each bug was fixed.

    Unlike the other daily buckets this is per version rather than merged across the
    channels: a fix reaches Beta and Release by separate uplifts, so the same bug
    can be outstanding on one and done on the other, and each has to be asked for
    against its own version.

    Nothing is subtracted for a bug fixed in the version's own cycle, as the
    query only keeps bugs the version is still marked as affected by. Once a fix
    is uplifted the status goes to fixed and the bug leaves the list.
    """
    query = burndown_query(version, UPLIFT_FLAGS[channel])
    bugs = [
        bug
        for bug in fetch_bugs(query, BURNDOWN_FIELDS)
        if parse_timestamp(bug["cf_last_resolved"]) < cutoff
    ]
    label = f"{{}} Fx{version} {channel.title()} fixed with no uplift request"

    return stuck_group(bugs, label, "resolved")


def regression_group(
    version: int, carry_over: bool, label: str, by_team: bool = False
) -> str:
    """
    Build the bullet and severity sub-bullets for one bug list.

    The list is fetched once and split by severity and team here, rather than
    asking Bugzilla for each subset, so the sub-bullets are guaranteed to be
    part of the count above them.

    Bug lists that are empty are left out entirely rather than reported as a
    zero, so a quiet channel is short instead of a wall of "0". Returns an
    empty string when there are no bugs at all.
    """
    query = regressions_query(version, carry_over)
    bugs = fetch_bugs(query)
    if not bugs:
        return ""

    lines = [f"• {bug_link(bugs, f'{{}} {label} Regressions', query)}"]

    if by_team:
        lines.append(SUB_BULLET + team_breakdown(bugs))

    severity_counts = []
    for severities, template in (
        (HIGH_SEVERITIES, "{} S2+"),
        (MISSING_SEVERITIES, "{} missing severity"),
    ):
        subset = [bug for bug in bugs if bug["severity"] in severities]
        if subset:
            severity_counts.append(
                bug_link(subset, template, with_severities(query, severities))
            )

    if severity_counts:
        lines.append(SUB_BULLET + ", ".join(severity_counts))

    return "\n".join(lines)


def to_blocks(sections: list[str]) -> list[dict]:
    """
    Wrap the sections of a message as Block Kit sections.

    Slack silently splits a message whose text runs past about 4000 characters
    into several messages, which is what happened when every count linked to a
    full query URL. Snapshot URLs brought the total well under that, but each
    section block gets its own 3000 character allowance, so keeping the sections
    means a busier cycle can't start splitting the message again.

    A section that does overflow raises rather than posting something malformed.
    The team breakdown is the part that could get there, at roughly 90 characters
    per team; capping or splitting it is the fix if that ever fires.
    """
    for section in sections:
        if len(section) > SECTION_LIMIT:
            raise RuntimeError(
                f"Slack section block is {len(section)} characters, over the "
                f"{SECTION_LIMIT} limit:\n{section[:200]}..."
            )

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": section}}
        for section in sections
    ]


def block_text(block: dict) -> str:
    """
    The text of any block, for printing a message instead of posting it.

    Section and header blocks keep their text in one place and context blocks in
    a list of elements, so a dry run has to handle both rather than assume the
    shape of the blocks it was handed.
    """
    if "elements" in block:
        return " ".join(element["text"] for element in block["elements"])

    return block["text"]["text"]


def build_daily_blocks(versions: dict[str, int]) -> list[dict]:
    """
    Build the daily action required message, one section per bucket.

    A header block titles the message and a context block labels it, then the
    standing ask and each bucket that has anything in it follow as sections.

    The title is a header rather than bold text in a section so that it renders
    at heading weight and separates the ask from the list. Header blocks take
    plain text only, which is why nothing else lives in there.
    """
    titles = [
        {"type": "header", "text": {"type": "plain_text", "text": DAILY_HEADING}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": DAILY_CADENCE}]},
    ]
    sections = [DAILY_INTRO]

    cutoff = stuck_since()
    bugs = open_regressions(versions)

    groups = [
        group
        for matches, label, anchor in STUCK_BUCKETS
        if (
            group := stuck_group(
                [bug for bug in bugs if matches(bug, cutoff)], label, anchor
            )
        )
    ]
    for channel in UPLIFT_FLAGS:
        version = versions.get(channel)
        if version is None:
            # A channel with no version to query. ESR is the case that will turn
            # up: it has an uplift flag of its own but channel_versions() does not
            # cover it, so it needs a version from somewhere else before it can be
            # reported. Skipped rather than raised, so adding a flag above can
            # never be the thing that costs the whole message, and said out loud
            # so it isn't a silent no-op either.
            print(
                f"no version for {channel}; skipping its burndown line",
                file=sys.stderr,
            )
            continue

        if group := burndown_group(channel, version, cutoff):
            groups.append(group)

    sections.extend(groups or [NOTHING_STUCK])

    return titles + to_blocks(sections)


def build_blocks(versions: dict[str, int]) -> list[dict]:
    """Build the cycle summary message as Block Kit sections, one per bug list."""
    sections = [HEADING]

    for channel in ("release", "beta", "nightly"):
        version = versions[channel]
        page = RELEASE_PAGE_URL.format(version)
        emoji = CHANNEL_EMOJI[channel]
        header = f"{emoji} *<{page}|Fx{version} {channel.title()}>*"

        countdown = cycle_countdown(version, channel)
        if countdown:
            header += f"\n{countdown}"

        groups = [
            group
            for group in (
                regression_group(version, False, "New", by_team=True),
                regression_group(version, True, "Carry Over"),
            )
            if group
        ]

        if not groups:
            sections.append(f"{header}\n{NOTHING_TO_REPORT}")
            continue

        # The header rides along with the first surviving group, so that a
        # channel with only carry over bugs isn't left with a stray heading.
        sections.append(f"{header}\n{groups[0]}")
        sections.extend(groups[1:])

    return to_blocks(sections)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post the open Firefox release regressions to Slack."
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="post the daily action required message rather than the cycle summary",
    )
    args = parser.parse_args()

    dry_run = env_flag("DRY_RUN")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url and not dry_run:
        print("SLACK_WEBHOOK_URL is not set — nothing to do.", file=sys.stderr)
        return 1

    heading, build, posted = (
        (DAILY_HEADING, build_daily_blocks, "action required")
        if args.daily
        else (HEADING, build_blocks, "regression summary")
    )

    versions = channel_versions()
    blocks = build(versions)

    if dry_run:
        print("DRY RUN: message not posted.\n")
        for block in blocks:
            print(block_text(block))
        return 0

    post_to_slack(webhook_url, heading, blocks=blocks)
    print(
        f"Posted {posted} for Firefox "
        f"{versions['release']} / {versions['beta']} / {versions['nightly']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
