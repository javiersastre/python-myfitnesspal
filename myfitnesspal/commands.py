import argparse
import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from dateutil.parser import parse as dateparse
from rich import print
from rich.console import Console

from . import Client
from .types import CommandDefinition

COMMANDS: Dict[str, CommandDefinition] = {}

logger = logging.getLogger(__name__)


def get_command_list():
    command_lines = []
    for name, info in COMMANDS.items():
        if info["is_alias"]:
            continue
        message = "{}: {}".format(name, info["description"])
        if info["aliases"]:
            message = message + "; aliases: {}".format(", ".join(info["aliases"]))
        command_lines.append(message)
    prolog = "available commands:\n"
    return prolog + "\n".join(["  " + cmd for cmd in command_lines])


def command(desc, name=None, aliases=None):
    if aliases is None:
        aliases = []

    def decorator(fn):
        main_name = name if name else fn.__name__
        command_details: CommandDefinition = {
            "function": fn,
            "description": desc,
            "is_alias": False,
            "aliases": [],
        }

        COMMANDS[main_name] = command_details
        for alias in aliases:
            COMMANDS[alias] = command_details.copy()
            COMMANDS[alias]["is_alias"] = True
            COMMANDS[main_name]["aliases"].append(alias)
        return fn

    return decorator


@command(
    "Display MyFitnessPal data for a given date.",
)
def day(super_args, *extra, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now().strftime("%Y-%m-%d"),
        type=lambda datestr: dateparse(datestr).date(),
        help="The date for which to display information.",
    )
    args = parser.parse_args(extra)

    client = Client(log_requests_to=super_args.log_requests_to)
    day = client.get_date(args.date)

    date_str = args.date.strftime("%Y-%m-%d")
    print(f"[blue]{date_str}[/blue]")
    for meal in day.meals:
        print(f"[bold]{meal.name.title()}[/bold]")
        for entry in meal.entries:
            print(f"* {entry.name}")
            print(
                f"  [italic bright_black]{entry.nutrition_information}"
                f"[/italic bright_black]"
            )
        print("")

    print("[bold]Totals[/bold]")
    for key, value in day.totals.items():
        print(
            "{key}: {value}".format(
                key=key.title(),
                value=value,
            )
        )
    print(f"Water: {day.water}")
    if day.notes:
        print(f"[italic]{day.notes}[/italic]")


def _parse_entry_name(entry) -> dict:
    """Split a raw MFP entry name into brand, product, quantity and unit.

    Assumptions (per MFP naming convention):
      - Brand never contains a hyphen  →  split on first ' - '
      - Amount/unit never contains a comma  →  split on last ', '
    Keeping 'name' verbatim in the output means nothing is lost if either
    assumption is violated for a particular entry.
    """
    brand = product = quantity = unit = None
    name = entry.name

    last_comma = name.rfind(", ")
    if last_comma != -1:
        food_part = name[:last_comma].strip()
        amount_tokens = name[last_comma + 2:].strip().split(None, 1)
        if len(amount_tokens) == 2:
            quantity, unit = amount_tokens
        elif len(amount_tokens) == 1:
            unit = amount_tokens[0]
    else:
        food_part = name.strip()

    parts = food_part.split(" - ", maxsplit=1)
    if len(parts) == 2:
        brand, product = parts[0].strip(), parts[1].strip()
    else:
        product = food_part.strip()

    return {"brand": brand, "product": product, "quantity": quantity, "unit": unit}


@command(
    "Download MyFitnessPal data for a date or date range and save to a JSON file.",
)
def days(super_args, *extra, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "dates",
        nargs="+",
        type=lambda s: dateparse(s).date(),
        metavar="DATE",
        help=(
            "One date (single day) or two dates (start and end of range, inclusive). "
            "Accepted formats: YYYY-MM-DD, MM/DD/YYYY, etc."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory where the output JSON file is written (default: current directory).",
    )
    args = parser.parse_args(extra)

    if len(args.dates) > 2:
        parser.error("Provide one date (single day) or two dates (start and end of range).")

    date_from = args.dates[0]
    date_to = args.dates[1] if len(args.dates) == 2 else date_from

    date_start = min(date_from, date_to)
    date_end = max(date_from, date_to)

    all_dates = []
    d = date_from
    step = timedelta(days=1) if date_from <= date_to else timedelta(days=-1)
    while d != date_to + step:
        all_dates.append(d)
        d += step

    console = Console(stderr=True)
    client = Client(log_requests_to=super_args.log_requests_to)
    results = {}

    for i, date in enumerate(all_dates):
        date_str = date.strftime("%Y-%m-%d")
        console.print(f"[blue]Fetching {date_str} ({i + 1}/{len(all_dates)})...[/blue]")

        for attempt in range(3):
            try:
                day_data = client.get_date(date)
                results[date_str] = {
                    "date": date_str,
                    "meals": [
                        {
                            "name": meal.name,
                            "entries": [
                                {
                                    "name": entry.name,
                                    **_parse_entry_name(entry),
                                    "nutrition": dict(entry.nutrition_information),
                                }
                                for entry in meal.entries
                            ],
                        }
                        for meal in day_data.meals
                    ],
                    "totals": dict(day_data.totals),
                    "water": day_data.water,
                    "notes": day_data.notes or None,
                }
                console.print(f"[green]✓ {date_str}[/green]")
                break
            except Exception as e:
                if attempt < 2:
                    backoff = 60 * (2 ** attempt)
                    console.print(
                        f"[yellow]Error fetching {date_str} "
                        f"(attempt {attempt + 1}/3): {e}. "
                        f"Retrying in {backoff}s...[/yellow]"
                    )
                    time.sleep(backoff)
                else:
                    console.print(f"[red]Failed to fetch {date_str} after 3 attempts: {e}[/red]")
                    results[date_str] = None

        if i < len(all_dates) - 1:
            # Every 5th request take a longer break (20-45s), otherwise 3-9s.
            # Both ranges use uniform random to avoid a detectable fixed rhythm.
            if (i + 1) % 5 == 0:
                delay = random.uniform(20, 45)
                console.print(f"[dim]Taking a short break ({delay:.1f}s)...[/dim]")
            else:
                delay = random.uniform(3, 9)
            time.sleep(delay)

    filename = (
        f"myfitnesspal_{date_start.strftime('%Y-%m-%d')}"
        f"_{date_end.strftime('%Y-%m-%d')}.json"
    )
    output_path = args.output_dir / filename
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    console.print(f"[bold green]Saved → {output_path.resolve()}[/bold green]")
