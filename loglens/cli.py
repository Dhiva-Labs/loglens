"""Command-line entrypoint for LogLens."""

import click

from loglens import __version__


@click.command()
@click.argument(
    "files",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
)
@click.version_option(version=__version__, prog_name="loglens")
def main(files: tuple[str, ...]) -> None:
    """Tail and analyze one or more log FILES in a terminal UI."""
    click.echo(f"loglens {__version__}: TUI starts here in an upcoming milestone")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
