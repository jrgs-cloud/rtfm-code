import json
import sys
from pathlib import Path

import click

CONFIG_FILENAME = "rtfm.json"
_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "config.template.json"


@click.command("init")
@click.option(
    "--languages",
    multiple=True,
    type=str,
    help="Languages to enable (repeat flag): --languages python --languages typescript.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing config.",
)
@click.option(
    "--output",
    default=CONFIG_FILENAME,
    show_default=True,
    type=click.Path(),
    help="Output path for the config file.",
)
def init(languages: tuple[str, ...], force: bool, output: str) -> None:
    """Bootstrap a new project for rtfm usage."""
    output_path = Path(output)

    if output_path.exists() and not force:
        click.echo(
            f"Config already exists at {output_path}. Use --force to overwrite.",
            err=True,
        )
        sys.exit(1)

    config = json.loads(_TEMPLATE_PATH.read_text())

    if languages:
        config["languages"] = list(languages)

    output_path.write_text(json.dumps(config, indent=2) + "\n")
    click.echo(f"Created {output_path}")

    dirs_created: set[Path] = set()
    for rel_path in config.get("paths", {}).values():
        p = Path(rel_path)
        target_dir = p if rel_path.endswith("/") else p.parent
        if str(target_dir) != "." and target_dir not in dirs_created:
            target_dir.mkdir(parents=True, exist_ok=True)
            dirs_created.add(target_dir)
            click.echo(f"Created directory {target_dir}")
