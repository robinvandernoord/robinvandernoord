#!/usr/bin/python3

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

###############################################################################
# GitHub API Client
###############################################################################

REPO_FIELDS = """
fragment RepoFields on Repository {
  nameWithOwner
  stargazers {
    totalCount
  }
  forkCount
  languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
    edges {
      size
      node {
        name
        color
      }
    }
  }
}
"""

OWNED_REPOS_QUERY = (
    """
query ($cursor: String) {
  viewer {
    login
    name
    repositories(
      first: 100
      orderBy: {field: UPDATED_AT, direction: DESC}
      isFork: false
      after: $cursor
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        ...RepoFields
      }
    }
  }
}
"""
    + REPO_FIELDS
)

CONTRIBUTED_REPOS_QUERY = (
    """
query ($cursor: String) {
  viewer {
    repositoriesContributedTo(
      first: 100
      includeUserRepositories: false
      orderBy: {field: UPDATED_AT, direction: DESC}
      contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY, PULL_REQUEST_REVIEW]
      after: $cursor
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        ...RepoFields
      }
    }
  }
}
"""
    + REPO_FIELDS
)

CONTRIB_YEARS_QUERY = """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""


def contribs_by_year_query(years: list[int]) -> str:
    """
    :param years: years to get contributions for
    :return: query retrieving the contribution count of each year, aliased as
             `year<year>`
    """
    by_years = "\n".join(
        f"""
    year{year}: contributionsCollection(
      from: "{year}-01-01T00:00:00Z"
      to: "{year + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}"""
        for year in years
    )
    return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Queries:
    """
    Client for the GitHub GraphQL (v4) and REST (v3) APIs.
    """

    # Attempts for network errors and 5xx responses, with exponential backoff
    max_attempts = 4
    # Attempts while the REST API answers 202 because it is still computing
    # statistics; these are retried every two seconds
    max_accepted_retries = 60

    def __init__(
        self,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def _request(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        """
        Send a request, retrying network errors and server errors
        :return: HTTP status and decoded JSON body (None if there is no JSON body)
        """
        headers = {"Authorization": f"Bearer {self.access_token}"}
        for attempt in range(self.max_attempts):
            try:
                async with (
                    self.semaphore,
                    self.session.request(
                        method, url, headers=headers, **kwargs
                    ) as response,
                ):
                    if response.status < 500:
                        body = await response.read()
                        try:
                            data = json.loads(body) if body else None
                        except ValueError:
                            data = None
                        return response.status, data
                    error = f"HTTP {response.status}"
            except (TimeoutError, aiohttp.ClientError) as e:
                error = type(e).__name__
            if attempt + 1 < self.max_attempts:
                print(f"GitHub request failed ({error}). Retrying...")
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"GitHub request failed ({error})")

    async def query(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """
        Make a request to the GraphQL API
        :param query: GraphQL query to be sent to the API
        :param variables: values for the variables declared by the query
        :return: the `data` part of the GraphQL response
        """
        status, result = await self._request(
            "POST",
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables or {}},
        )
        data = result.get("data") if isinstance(result, dict) else None
        if status != 200 or data is None:
            errors = result.get("errors") if isinstance(result, dict) else None
            message = result.get("message") if isinstance(result, dict) else None
            raise RuntimeError(
                f"GraphQL query failed with HTTP {status}: {errors or message}"
            )
        return data

    async def query_rest(self, path: str, params: dict | None = None) -> Any:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :return: deserialized JSON output, or None if the request did not succeed
        """
        url = f"https://api.github.com/{path.removeprefix('/')}"
        for _ in range(self.max_accepted_retries):
            status, result = await self._request("GET", url, params=params or {})
            if status != 202:
                return result if status == 200 else None
            # Repository names are not logged since they may be private
            print("A path returned 202. Retrying...")
            await asyncio.sleep(2)
        print("There were too many 202s. Data for this repository will be incomplete.")
        return None


###############################################################################
# Statistics
###############################################################################


@dataclass
class Language:
    size: int
    occurrences: int
    color: str | None
    # Percentage of the total size of all languages
    prop: float = 0.0


@dataclass
class Stats:
    """
    Summary statistics about a user's GitHub usage
    """

    name: str
    stargazers: int
    forks: int
    total_contributions: int
    languages: dict[str, Language]
    repos: set[str]
    lines_added: int
    lines_deleted: int
    # Only covers the last 14 days, as per the GitHub API
    views: int

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_deleted

    def __str__(self) -> str:
        formatted_languages = "\n  - ".join(
            f"{k}: {v.prop:0.4f}%" for k, v in self.languages.items()
        )
        return f"""Name: {self.name}
Stargazers: {self.stargazers:,}
Forks: {self.forks:,}
All-time contributions: {self.total_contributions:,}
Repositories with contributions: {len(self.repos)}
Lines of code added: {self.lines_added:,}
Lines of code deleted: {self.lines_deleted:,}
Lines of code changed: {self.lines_changed:,}
Project page views: {self.views:,}
Languages:
  - {formatted_languages}"""


async def fetch_repo_pages(queries: Queries, query: str, connection: str) -> dict:
    """
    Follow the pagination of a repository connection of the viewer
    :param query: query taking a `$cursor` variable
    :param connection: name of the repository connection in the query
    :return: data of the viewer from the first page, with the repository nodes
             of all pages merged into it
    """
    viewer: dict = {}
    nodes: list[dict] = []
    cursor = None
    while True:
        data = await queries.query(query, {"cursor": cursor})
        viewer = viewer or data["viewer"]
        repos = data["viewer"][connection]
        nodes += repos["nodes"]
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    viewer[connection]["nodes"] = nodes
    return viewer


async def fetch_total_contributions(queries: Queries) -> int:
    """
    :return: count of user's total contributions as defined by GitHub
    """
    data = await queries.query(CONTRIB_YEARS_QUERY)
    years = data["viewer"]["contributionsCollection"]["contributionYears"]
    if not years:
        return 0
    data = await queries.query(contribs_by_year_query(years))
    return sum(
        year["contributionCalendar"]["totalContributions"]
        for year in data["viewer"].values()
    )


async def fetch_lines_changed(
    queries: Queries, repo: str, username: str
) -> tuple[int, int]:
    """
    :return: lines added and deleted by the user in a repository
    """
    additions = 0
    deletions = 0
    result = await queries.query_rest(f"/repos/{repo}/stats/contributors")
    for author_obj in result if isinstance(result, list) else []:
        # Handle malformed response from the API by skipping this entry
        if not isinstance(author_obj, dict) or not isinstance(
            author_obj.get("author") or {}, dict
        ):
            continue
        if (author_obj.get("author") or {}).get("login") != username:
            continue
        for week in author_obj.get("weeks", []):
            additions += week.get("a", 0)
            deletions += week.get("d", 0)
    return additions, deletions


async def fetch_views(queries: Queries, repo: str) -> int:
    """
    :return: page views of a repository over the last 14 days
    """
    result = await queries.query_rest(f"/repos/{repo}/traffic/views")
    if not isinstance(result, dict):
        return 0
    return sum(view.get("count", 0) for view in result.get("views", []))


async def fetch_stats(
    username: str,
    access_token: str,
    session: aiohttp.ClientSession,
    exclude_repos: set[str] | None = None,
    exclude_langs: set[str] | None = None,
    ignore_forked_repos: bool = False,
) -> Stats:
    """
    Retrieve statistics about a user's GitHub usage
    """
    queries = Queries(access_token, session)
    exclude_repos = exclude_repos or set()
    exclude_langs_lower = {x.lower() for x in exclude_langs or set()}

    async with asyncio.TaskGroup() as tg:
        owned_task = tg.create_task(
            fetch_repo_pages(queries, OWNED_REPOS_QUERY, "repositories")
        )
        contributed_task = (
            None
            if ignore_forked_repos
            else tg.create_task(
                fetch_repo_pages(
                    queries, CONTRIBUTED_REPOS_QUERY, "repositoriesContributedTo"
                )
            )
        )
        contributions_task = tg.create_task(fetch_total_contributions(queries))

    viewer = owned_task.result()
    repo_nodes = viewer["repositories"]["nodes"]
    if contributed_task is not None:
        repo_nodes += contributed_task.result()["repositoriesContributedTo"]["nodes"]

    stargazers = 0
    forks = 0
    languages: dict[str, Language] = {}
    repos: set[str] = set()
    for repo in repo_nodes:
        if repo is None:
            continue
        name = repo["nameWithOwner"]
        if name in repos or name in exclude_repos:
            continue
        repos.add(name)
        stargazers += repo["stargazers"]["totalCount"]
        forks += repo["forkCount"]

        for edge in repo["languages"]["edges"]:
            lang = edge["node"]["name"]
            if lang.lower() in exclude_langs_lower:
                continue
            if lang in languages:
                languages[lang].size += edge["size"]
                languages[lang].occurrences += 1
            else:
                languages[lang] = Language(
                    size=edge["size"], occurrences=1, color=edge["node"]["color"]
                )

    # TODO: Improve languages to scale by number of contributions to
    #       specific filetypes
    langs_total = sum(lang.size for lang in languages.values())
    for lang in languages.values():
        lang.prop = 100 * lang.size / langs_total if langs_total else 0.0

    async with asyncio.TaskGroup() as tg:
        lines_tasks = [
            tg.create_task(fetch_lines_changed(queries, repo, username))
            for repo in repos
        ]
        views_tasks = [tg.create_task(fetch_views(queries, repo)) for repo in repos]

    return Stats(
        name=viewer["name"] or viewer["login"],
        stargazers=stargazers,
        forks=forks,
        total_contributions=contributions_task.result(),
        languages=languages,
        repos=repos,
        lines_added=sum(task.result()[0] for task in lines_tasks),
        lines_deleted=sum(task.result()[1] for task in lines_tasks),
        views=sum(task.result() for task in views_tasks),
    )


###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        print(await fetch_stats(user, access_token, session))


if __name__ == "__main__":
    asyncio.run(main())
