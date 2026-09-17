#!/usr/bin/env python3
"""Coordinate-free macOS GUI adapter for Portfolio Performance CSV import."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any, Callable, Iterable

from import_watcher import RuntimeConfig, load_config


PREFLIGHT_APPLESCRIPT = r'''
on run argv
    set bundleId to item 1 of argv
    tell application "System Events"
        set accessibilityEnabled to UI elements enabled
        set matchingProcesses to every application process whose bundle identifier is bundleId
        set appRunning to ((count of matchingProcesses) > 0)
    end tell
    return (accessibilityEnabled as text) & "|" & (appRunning as text)
end run
'''


GUI_APPLESCRIPT = r'''
on splitLabels(labelText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to "||"
    set labelItems to text items of labelText
    set AppleScript's text item delimiters to previousDelimiters
    return labelItems
end splitLabels

on waitForProcess(bundleId, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then return true
        end tell
        if (current date) > deadline then error "Timed out waiting for Portfolio Performance process"
        delay 0.25
    end repeat
end waitForProcess

on waitForWindow(bundleId, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    if (count of windows) > 0 then return true
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for Portfolio Performance window"
        delay 0.25
    end repeat
end waitForWindow

on windowCount(bundleId)
    tell application "System Events"
        set matchingProcesses to every application process whose bundle identifier is bundleId
        if (count of matchingProcesses) = 0 then return 0
        set targetProcess to item 1 of matchingProcesses
        tell targetProcess
            return count of windows
        end tell
    end tell
end windowCount

on waitForChooser(bundleId, originalWindowCount, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    if (count of windows) > originalWindowCount then return true
                    if (count of windows) > 0 then
                        try
                            if (count of sheets of front window) > 0 then return true
                        end try
                    end if
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for file chooser"
        delay 0.25
    end repeat
end waitForChooser

on accessibilityIdentifier(targetElement)
    tell application "System Events"
        try
            return value of attribute "AXIdentifier" of targetElement as text
        on error
            return ""
        end try
    end tell
end accessibilityIdentifier

on clickNamedMenuItem(bundleId, menuBarItemName, menuItemName, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    try
                        set frontmost to true
                        set targetMenuBarItem to menu bar item menuBarItemName of menu bar 1
                        click targetMenuBarItem
                        delay 0.2
                        set targetMenuItem to menu item menuItemName of menu 1 of targetMenuBarItem
                        if enabled of targetMenuItem then
                            click targetMenuItem
                            return true
                        end if
                    end try
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for menu item: " & menuBarItemName & " → " & menuItemName
        delay 0.25
    end repeat
end clickNamedMenuItem

on clickCSVImportMenu(bundleId, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    try
                        set frontmost to true
                        set fileMenuBarItem to menu bar item "File" of menu bar 1
                        click fileMenuBarItem
                        delay 0.2
                        set importMenuItem to menu item "Import" of menu 1 of fileMenuBarItem
                        click importMenuItem
                        delay 0.2
                        set csvMenuItems to every menu item of menu 1 of importMenuItem whose name starts with "CSV files (comma-separated values)"
                        if (count of csvMenuItems) > 0 then
                            set csvMenuItem to item 1 of csvMenuItems
                            if enabled of csvMenuItem then
                                click csvMenuItem
                                return true
                            end if
                        end if
                    end try
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for File → Import → CSV files"
        delay 0.25
    end repeat
end clickCSVImportMenu

on showGoToFolder(bundleId)
    tell application "System Events"
        set matchingProcesses to every application process whose bundle identifier is bundleId
        if (count of matchingProcesses) = 0 then error "Portfolio Performance process is not running"
        set targetProcess to item 1 of matchingProcesses
        set frontmost of targetProcess to true
        delay 0.2
        try
            key down command
            key down shift
            key code 5
        on error errorMessage number errorNumber
            try
                key up shift
            end try
            try
                key up command
            end try
            error errorMessage number errorNumber
        end try
        key up shift
        key up command
    end tell
end showGoToFolder

on pressReturn(bundleId)
    tell application "System Events"
        set matchingProcesses to every application process whose bundle identifier is bundleId
        if (count of matchingProcesses) = 0 then error "Portfolio Performance process is not running"
        set targetProcess to item 1 of matchingProcesses
        set frontmost of targetProcess to true
        key code 36
    end tell
end pressReturn

on setGoToPath(bundleId, pathValue, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    repeat with targetWindow in every window
                        repeat with targetSheet in every sheet of targetWindow
                            repeat with candidateField in every text field of targetSheet
                                if my accessibilityIdentifier(candidateField) is "PathTextField" then
                                    set value of candidateField to pathValue
                                    return true
                                end if
                            end repeat
                        end repeat
                        set candidateElements to entire contents of targetWindow
                        repeat with candidateField in candidateElements
                            try
                                if role of candidateField is "AXTextField" then
                                    if my accessibilityIdentifier(candidateField) is "PathTextField" then
                                        set value of candidateField to pathValue
                                        return true
                                    end if
                                end if
                            end try
                        end repeat
                    end repeat
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for Go to Folder path field"
        delay 0.25
    end repeat
end setGoToPath

on clickButtonByIdentifier(bundleId, buttonIdentifier, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    repeat with targetWindow in every window
                        set candidateElements to entire contents of targetWindow
                        repeat with candidateElement in candidateElements
                            try
                                if role of candidateElement is "AXButton" then
                                    if my accessibilityIdentifier(candidateElement) is buttonIdentifier then
                                        if enabled of candidateElement then
                                            click candidateElement
                                            return true
                                        end if
                                    end if
                                end if
                            end try
                        end repeat
                    end repeat
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for enabled button id: " & buttonIdentifier
        delay 0.25
    end repeat
end clickButtonByIdentifier

on fileNameFromPath(pathValue)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to "/"
    set pathItems to text items of pathValue
    set fileName to last item of pathItems
    set AppleScript's text item delimiters to previousDelimiters
    return fileName
end fileNameFromPath

on documentTabExists(bundleId, documentName)
    tell application "System Events"
        set matchingProcesses to every application process whose bundle identifier is bundleId
        if (count of matchingProcesses) = 0 then return false
        set targetProcess to item 1 of matchingProcesses
        tell targetProcess
            if (count of windows) = 0 then return false
            set candidateElements to entire contents of front window
            repeat with candidateElement in candidateElements
                try
                    if role of candidateElement is "AXTabGroup" then
                        if name of candidateElement as text is documentName then return true
                    end if
                end try
            end repeat
        end tell
    end tell
    return false
end documentTabExists

on waitForDocumentTab(bundleId, documentName, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    if (count of windows) > 0 then
                        set candidateElements to entire contents of front window
                        repeat with candidateElement in candidateElements
                            try
                                if role of candidateElement is "AXTabGroup" then
                                    if name of candidateElement as text is documentName then return true
                                end if
                            end try
                        end repeat
                    end if
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for portfolio document tab: " & documentName
        delay 0.25
    end repeat
end waitForDocumentTab

on clickNamedButton(bundleId, buttonLabels, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) > 0 then
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    if (count of windows) > 0 then
                        set candidateElements to entire contents of front window
                        repeat with candidateElement in candidateElements
                            try
                                if role of candidateElement is "AXButton" then
                                    set buttonTitle to name of candidateElement as text
                                    repeat with buttonLabel in buttonLabels
                                        set expectedTitle to buttonLabel as text
                                        if expectedTitle is not "" then
                                            if buttonTitle contains expectedTitle then
                                                if enabled of candidateElement then
                                                    click candidateElement
                                                    return buttonTitle
                                                end if
                                            end if
                                        end if
                                    end repeat
                                end if
                            end try
                        end repeat
                    end if
                end tell
            end if
        end tell
        if (current date) > deadline then error "Timed out waiting for enabled button: " & (buttonLabels as text)
        delay 0.25
    end repeat
end clickNamedButton

on waitForProcessExit(bundleId, timeoutSeconds)
    set deadline to (current date) + timeoutSeconds
    repeat
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            if (count of matchingProcesses) = 0 then return true
        end tell
        if (current date) > deadline then error "Timed out waiting for Portfolio Performance to quit"
        delay 0.25
    end repeat
end waitForProcessExit

on run argv
    set xmlPath to item 1 of argv
    set csvPath to item 2 of argv
    set bundleId to item 3 of argv
    set csvMenuKey to item 4 of argv
    set nextLabels to my splitLabels(item 5 of argv)
    set finishLabels to my splitLabels(item 6 of argv)
    set launchTimeout to (item 7 of argv) as integer
    set dialogTimeout to (item 8 of argv) as integer
    set saveTimeout to (item 9 of argv) as integer
    set currentStage to "wait_process"
    set completedStages to {}

    try
        my waitForProcess(bundleId, launchTimeout)
        my waitForWindow(bundleId, launchTimeout)

        set currentStage to "open_xml"
        set documentName to my fileNameFromPath(xmlPath)
        if not my documentTabExists(bundleId, documentName) then
            tell application "System Events"
                set matchingProcesses to every application process whose bundle identifier is bundleId
                set targetProcess to item 1 of matchingProcesses
                tell targetProcess
                    set frontmost to true
                    set originalWindowCount to count of windows
                end tell
            end tell
            my clickNamedMenuItem(bundleId, "File", "Open...", dialogTimeout)
            my waitForChooser(bundleId, originalWindowCount, dialogTimeout)
            my showGoToFolder(bundleId)
            my setGoToPath(bundleId, xmlPath, dialogTimeout)
            my pressReturn(bundleId)
            my clickButtonByIdentifier(bundleId, "OKButton", dialogTimeout)
        end if
        my waitForDocumentTab(bundleId, documentName, launchTimeout)
        set end of completedStages to "open_xml"

        set currentStage to "start_import"
        tell application "System Events"
            set matchingProcesses to every application process whose bundle identifier is bundleId
            set targetProcess to item 1 of matchingProcesses
            tell targetProcess
                set frontmost to true
                set originalWindowCount to count of windows
            end tell
        end tell
        my clickCSVImportMenu(bundleId, dialogTimeout)
        set end of completedStages to "start_import"

        set currentStage to "choose_csv"
        my waitForChooser(bundleId, originalWindowCount, dialogTimeout)
        my showGoToFolder(bundleId)
        my setGoToPath(bundleId, csvPath, dialogTimeout)
        my pressReturn(bundleId)
        my clickButtonByIdentifier(bundleId, "OKButton", dialogTimeout)
        set end of completedStages to "choose_csv"

        set currentStage to "next"
        my clickNamedButton(bundleId, nextLabels, dialogTimeout)
        set end of completedStages to "next"

        set currentStage to "finish"
        my clickNamedButton(bundleId, finishLabels, dialogTimeout)
        set end of completedStages to "finish"

        set currentStage to "save"
        delay 0.5
        my clickNamedMenuItem(bundleId, "File", "Save", saveTimeout)
        delay 1
        set end of completedStages to "save"

        set currentStage to "quit"
        my clickNamedMenuItem(bundleId, "Portfolio Performance", "Quit Portfolio Performance", saveTimeout)
        my waitForProcessExit(bundleId, saveTimeout)
        set end of completedStages to "quit"

        set previousDelimiters to AppleScript's text item delimiters
        set AppleScript's text item delimiters to ">"
        set stageText to completedStages as text
        set AppleScript's text item delimiters to previousDelimiters
        return "OK|" & stageText
    on error errorMessage number errorNumber
        return "ERROR|" & currentStage & "|" & (errorNumber as text) & "|" & errorMessage
    end try
end run
'''


class GUIImportError(RuntimeError):
    """A failure with a named GUI automation stage."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")


@dataclass(frozen=True)
class GUIImportResult:
    steps: tuple[str, ...]
    stdout: str
    stderr: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    runner: Runner,
    command: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _app_value(config: RuntimeConfig, key: str) -> Any:
    if key not in config.portfolio_performance:
        raise GUIImportError("config", f"Missing portfolio_performance.{key}")
    return config.portfolio_performance[key]


def preflight(
    config: RuntimeConfig,
    csv_path: Path,
    *,
    runner: Runner = subprocess.run,
    system_name: Callable[[], str] = platform.system,
) -> None:
    """Check all real-mode prerequisites without performing GUI actions."""

    if system_name() != "Darwin":
        raise GUIImportError("preflight", "GUI import requires macOS")
    app_path = Path(str(_app_value(config, "app_path"))).expanduser()
    if not app_path.is_dir() or app_path.suffix != ".app":
        raise GUIImportError(
            "preflight", f"Portfolio Performance app not found: {app_path}"
        )
    for label, path in (("XML", config.xml_file), ("CSV", csv_path)):
        if path.is_symlink() or not path.is_file():
            raise GUIImportError(
                "preflight", f"{label} is not a regular file: {path}"
            )

    bundle_id = str(_app_value(config, "bundle_id"))
    result = _run(
        runner,
        ["/usr/bin/osascript", "-", bundle_id],
        input_text=PREFLIGHT_APPLESCRIPT,
    )
    if result.returncode != 0:
        raise GUIImportError(
            "preflight",
            f"System Events check failed: {result.stderr.strip()}",
        )
    values = result.stdout.strip().lower().split("|")
    if len(values) != 2:
        raise GUIImportError(
            "preflight",
            f"Unexpected System Events response: {result.stdout.strip()}",
        )
    accessibility_enabled, app_running = values
    if accessibility_enabled != "true":
        raise GUIImportError(
            "preflight",
            "Accessibility is disabled for this terminal/Python host. "
            "Enable it in System Settings → Privacy & Security → Accessibility.",
        )
    if app_running == "true":
        raise GUIImportError(
            "preflight",
            "Portfolio Performance is already running. Save your work and quit "
            "the app before executing the watcher.",
        )


def run_gui_import(
    config: RuntimeConfig,
    csv_path: Path,
    *,
    runner: Runner = subprocess.run,
    system_name: Callable[[], str] = platform.system,
) -> GUIImportResult:
    """Open the fixed XML and execute the bounded import wizard flow."""

    csv_path = csv_path.expanduser().resolve()
    preflight(
        config, csv_path, runner=runner, system_name=system_name
    )
    app_path = Path(str(_app_value(config, "app_path"))).expanduser()
    open_result = _run(
        runner,
        [
            "/usr/bin/open",
            "-a",
            str(app_path),
        ],
    )
    if open_result.returncode != 0:
        raise GUIImportError(
            "open_xml",
            open_result.stderr.strip() or "macOS open command failed",
        )

    arguments = [
        str(config.xml_file),
        str(csv_path),
        str(_app_value(config, "bundle_id")),
        str(_app_value(config, "csv_menu_key")),
        "||".join(_app_value(config, "next_button_labels")),
        "||".join(_app_value(config, "finish_button_labels")),
        str(int(_app_value(config, "launch_timeout_seconds"))),
        str(int(_app_value(config, "dialog_timeout_seconds"))),
        str(int(_app_value(config, "save_timeout_seconds"))),
    ]
    gui_result = _run(
        runner,
        ["/usr/bin/osascript", "-", *arguments],
        input_text=GUI_APPLESCRIPT,
    )
    if gui_result.returncode != 0:
        raise GUIImportError(
            "osascript",
            gui_result.stderr.strip() or "osascript exited unsuccessfully",
        )
    response = gui_result.stdout.strip()
    if response.startswith("ERROR|"):
        parts = response.split("|", 3)
        stage = parts[1] if len(parts) > 1 else "unknown"
        message = parts[3] if len(parts) > 3 else response
        raise GUIImportError(stage, message)
    if not response.startswith("OK|"):
        raise GUIImportError(
            "osascript", f"Unexpected GUI response: {response}"
        )
    internal_steps = tuple(
        step for step in response[3:].split(">") if step
    )
    return GUIImportResult(
        steps=("preflight", "launch_app", *internal_steps),
        stdout=gui_result.stdout,
        stderr=gui_result.stderr,
    )


def describe_plan(config: RuntimeConfig, csv_path: Path) -> tuple[str, ...]:
    return (
        f"Preflight macOS, Accessibility, and closed app for {csv_path}",
        f"Open {config.xml_file} with "
        f"{_app_value(config, 'app_path')}",
        "Press Cmd+I, then the configured CSV menu key",
        f"Enter absolute CSV path {csv_path.expanduser().resolve()}",
        "Click enabled Next and Finish by accessibility role/name",
        f"Save {config.xml_file}, quit, and wait for process exit",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Portfolio Performance macOS GUI import adapter."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
    )
    parser.add_argument("--csv", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def run(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.dry_run:
            for index, action in enumerate(
                describe_plan(config, args.csv), start=1
            ):
                sys.stdout.write(f"{index}. {action}\n")
            return 0
        if args.check:
            preflight(config, args.csv)
            sys.stdout.write("Preflight passed; no GUI actions performed.\n")
            return 0
        result = run_gui_import(config, args.csv)
        sys.stdout.write("GUI import completed: " + " → ".join(result.steps))
        sys.stdout.write("\n")
        return 0
    except (GUIImportError, OSError, ValueError) as error:
        sys.stderr.write(f"GUI import failed: {error}\n")
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
