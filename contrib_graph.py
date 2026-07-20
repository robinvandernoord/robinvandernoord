#!/usr/bin/python3
"""
PoC: terminal (TUI) graphs of GitHub contributions using plotext.

Reuses the GraphQL/REST client already defined in github_stats.py.

Usage:
    ACCESS_TOKEN=ghp_xxx GITHUB_ACTOR=your-username python3 contrib_graph.py
    python3 contrib_graph.py --user your-username --token ghp_xxx --days 180 --loc
"""

import argparse
import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

import aiohttp
import plotext as plt

from github_stats import Queries, Stats

CALENDAR_QUERY = """
query {{
  viewer {{
    contributionsCollection(from: "{from_date}", to: "{to_date}") {{
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      contributionCalendar {{
        weeks {{
          contributionDays {{
            date
            contributionCount
          }}
        }}
      }}
    }}
  }}
}}
"""


async def fetch_collection(queries: Queries, days: int) -> Dict[str, Any]:
    """
    :param days: size of the trailing window to fetch (GitHub caps a single
        contributionsCollection query at 1 year)
    :return: contributionsCollection payload for the window
    """
    to_date = datetime.utcnow()
    from_date = to_date - timedelta(days=days)
    query = CALENDAR_QUERY.format(
        from_date=from_date.strftime("%Y-%m-%dT00:00:00Z"),
        to_date=to_date.strftime("%Y-%m-%dT00:00:00Z"),
    )
    result = await queries.query(query)
    return result.get("data", {}).get("viewer", {}).get("contributionsCollection", {})


def flatten_days(collection: Dict[str, Any]) -> List[Dict[str, Any]]:
    weeks = collection.get("contributionCalendar", {}).get("weeks", [])
    return [day for week in weeks for day in week.get("contributionDays", [])]


def plot_daily_contributions(days_data: List[Dict[str, Any]]) -> None:
    dates = [d["date"] for d in days_data]
    counts = [d["contributionCount"] for d in days_data]

    plt.clear_figure()
    plt.date_form("Y-m-d")
    plt.plot(dates, counts, marker="braille")
    plt.title("Daily contributions")
    plt.xlabel("date")
    plt.ylabel("contributions")
    plt.plotsize(None, 20)
    plt.show()


def plot_contribution_types(collection: Dict[str, Any]) -> None:
    labels = ["commits", "PRs", "PR reviews", "issues"]
    values = [
        collection.get("totalCommitContributions", 0),
        collection.get("totalPullRequestContributions", 0),
        collection.get("totalPullRequestReviewContributions", 0),
        collection.get("totalIssueContributions", 0),
    ]

    plt.clear_figure()
    plt.bar(labels, values)
    plt.title("Contributions by type (window)")
    plt.plotsize(None, 15)
    plt.show()


def plot_loc(additions: int, deletions: int) -> None:
    plt.clear_figure()
    plt.bar(["added", "deleted"], [additions, deletions])
    plt.title("Lines of code changed (all-time, all repos)")
    plt.plotsize(None, 15)
    plt.show()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.getenv("GITHUB_ACTOR"))
    parser.add_argument("--token", default=os.getenv("ACCESS_TOKEN"))
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="trailing window in days for the daily-contributions graph (max 365)",
    )
    parser.add_argument(
        "--loc",
        action="store_true",
        help="also graph lines-of-code added/deleted (slow: iterates every repo)",
    )
    args = parser.parse_args()

    if not args.user or not args.token:
        raise SystemExit(
            "Set --user/--token, or the GITHUB_ACTOR/ACCESS_TOKEN environment variables"
        )

    async with aiohttp.ClientSession() as session:
        stats = Stats(args.user, args.token, session)
        collection = await fetch_collection(stats.queries, args.days)

        plot_daily_contributions(flatten_days(collection))
        plot_contribution_types(collection)

        if args.loc:
            additions, deletions = await stats.lines_changed
            plot_loc(additions, deletions)


if __name__ == "__main__":
    asyncio.run(main())
