#!/usr/bin/python3

import asyncio
import os
from html import escape
from pathlib import Path

import aiohttp

from github_stats import Stats, fetch_stats

TEMPLATES = Path("templates")
OUTPUT = Path("generated")


################################################################################
# Helper Functions
################################################################################


def render(template: str, replacements: dict[str, str]) -> None:
    """
    Fill in the {{ placeholders }} of a template and write it to the output
    folder under the same name
    :param template: file name of the template
    :param replacements: placeholder names mapped to their values
    """
    output = (TEMPLATES / template).read_text()
    for key, value in replacements.items():
        output = output.replace(f"{{{{ {key} }}}}", value)

    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / template).write_text(output)


################################################################################
# Individual Image Generation Functions
################################################################################


def generate_overview(s: Stats) -> None:
    """
    Generate an SVG badge with summary statistics
    :param s: Represents user's GitHub statistics
    """
    render(
        "overview.svg",
        {
            "name": escape(s.name),
            "stars": f"{s.stargazers:,}",
            "forks": f"{s.forks:,}",
            "contributions": f"{s.total_contributions:,}",
            "lines_changed": f"{s.lines_changed:,}",
            "views": f"{s.views:,}",
            "repos": f"{len(s.repos):,}",
        },
    )


def generate_languages(s: Stats) -> None:
    """
    Generate an SVG badge with summary languages used
    :param s: Represents user's GitHub statistics
    """
    progress = ""
    lang_list = ""
    sorted_languages = sorted(
        s.languages.items(), reverse=True, key=lambda t: t[1].size
    )
    delay_between = 150
    for i, (lang, data) in enumerate(sorted_languages):
        color = data.color or "#000000"
        progress += (
            f'<span style="background-color: {color};'
            f'width: {data.prop:0.3f}%;" '
            f'class="progress-item"></span>'
        )
        lang_list += f"""
<li style="animation-delay: {i * delay_between}ms;">
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color};"
viewBox="0 0 16 16" version="1.1" width="16" height="16"><path
fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{escape(lang)}</span>
<span class="percent">{data.prop:0.2f}%</span>
</li>

"""

    render("languages.svg", {"progress": progress, "lang_list": lang_list})


################################################################################
# Main Function
################################################################################


def parse_set(value: str | None) -> set[str] | None:
    """
    :return: the comma-separated items of an environment variable, if any
    """
    return {x.strip() for x in value.split(",")} if value else None


async def main() -> None:
    """
    Generate all badges
    """
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("A personal access token is required to proceed!")
    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("Environment variable GITHUB_ACTOR must be set.")
    # Convert a truthy value to a Boolean
    raw_ignore_forked_repos = (os.getenv("EXCLUDE_FORKED_REPOS") or "").strip()
    ignore_forked_repos = raw_ignore_forked_repos.lower() not in ("", "false")

    async with aiohttp.ClientSession() as session:
        s = await fetch_stats(
            user,
            access_token,
            session,
            exclude_repos=parse_set(os.getenv("EXCLUDED")),
            exclude_langs=parse_set(os.getenv("EXCLUDED_LANGS")),
            ignore_forked_repos=ignore_forked_repos,
        )
    generate_languages(s)
    generate_overview(s)


if __name__ == "__main__":
    asyncio.run(main())
