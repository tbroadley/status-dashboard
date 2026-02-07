import argparse
import asyncio
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

_ = os.environ.setdefault("TODOIST_API_TOKEN", "fake-token")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

_ = sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import fake_data  # noqa: E402
from status_dashboard.app import StatusDashboard  # noqa: E402


def export_text(app: StatusDashboard) -> str:
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    screen_render = app.screen._compositor.render_update(  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType]
        full=True,
        screen_stack=app._background_screens,  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType]
        simplify=True,
    )
    console.print(screen_render)
    return console.export_text(clear=True, styles=False)


def save_screenshot(text: str, scenario: str, index: int, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{scenario}_{index:03d}.txt"
    _ = path.write_text(text)
    return path


PATCHES = [
    patch(
        "status_dashboard.clients.github.get_my_prs",
        return_value=fake_data.fake_prs(),
    ),
    patch(
        "status_dashboard.clients.github.get_review_requests",
        return_value=fake_data.fake_review_requests(),
    ),
    patch(
        "status_dashboard.clients.github.get_notifications",
        return_value=fake_data.fake_notifications(),
    ),
    patch(
        "status_dashboard.clients.todoist.get_tasks_for_date",
        return_value=fake_data.fake_todoist_tasks(),
    ),
    patch(
        "status_dashboard.clients.linear.get_project_issues",
        return_value=fake_data.fake_linear_issues(),
    ),
    patch(
        "status_dashboard.db.goals.get_goals_for_week",
        return_value=fake_data.fake_goals(),
    ),
    patch("status_dashboard.db.goals.get_week_metrics", return_value=None),
    patch("status_dashboard.app.StatusDashboard._check_for_updates"),
]


async def run_scenario(
    scenario: str,
    keys: list[str] | None,
    size: tuple[int, int],
    output_dir: Path,
) -> list[Path]:
    saved: list[Path] = []

    for p in PATCHES:
        _ = p.start()

    try:
        app = StatusDashboard()
        async with app.run_test(size=size) as pilot:  # pyright: ignore[reportUnknownVariableType]
            await pilot.pause()
            await pilot.pause()

            if scenario == "default" and not keys:
                text = export_text(app)
                saved.append(save_screenshot(text, scenario, 1, output_dir))

            elif scenario == "navigation":
                text = export_text(app)
                saved.append(save_screenshot(text, scenario, 1, output_dir))

                for i, key in enumerate(["j", "j", "j", "k", "Tab", "j", "j"], start=2):
                    await pilot.press(key)
                    await pilot.pause()
                    text = export_text(app)
                    saved.append(save_screenshot(text, scenario, i, output_dir))

            elif scenario == "modal":
                text = export_text(app)
                saved.append(save_screenshot(text, scenario, 1, output_dir))

                for key in ["Tab", "Tab", "Tab", "Tab"]:
                    await pilot.press(key)
                    await pilot.pause()
                await pilot.press("c")
                await pilot.pause()
                text = export_text(app)
                saved.append(save_screenshot(text, scenario, 2, output_dir))

            elif keys:
                text = export_text(app)
                saved.append(save_screenshot(text, scenario, 1, output_dir))

                for i, key in enumerate(keys, start=2):
                    await pilot.press(key)
                    await pilot.pause()
                    text = export_text(app)
                    saved.append(save_screenshot(text, scenario, i, output_dir))
    finally:
        for p in PATCHES:
            _ = p.stop()

    return saved


def parse_size(s: str) -> tuple[int, int]:
    parts = s.lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Size must be WxH, got: {s}")
    return int(parts[0]), int(parts[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="TUI screenshot harness")
    _ = parser.add_argument("--keys", type=str, help="Space-separated keys to send")
    _ = parser.add_argument(
        "--scenario",
        choices=["default", "navigation", "modal"],
        default="default",
        help="Predefined scenario to run",
    )
    _ = parser.add_argument(
        "--size", type=parse_size, default=(120, 40), help="Terminal size as WxH"
    )
    _ = parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "screenshots",
        help="Output directory for screenshots",
    )
    args = parser.parse_args()

    keys: list[str] | None = args.keys.split() if args.keys else None  # pyright: ignore[reportAny]
    scenario: str = "custom" if keys and args.scenario == "default" else args.scenario  # pyright: ignore[reportAny]

    saved = asyncio.run(
        run_scenario(scenario, keys, args.size, args.output_dir)  # pyright: ignore[reportAny]
    )

    for path in saved:
        print(f"Saved: {path}")


if __name__ == "__main__":
    main()
