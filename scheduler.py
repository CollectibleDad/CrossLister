"""
Sets up Windows Task Scheduler to run CrossLister daily at 9:00 AM.
Run this script once to install the scheduled task.
Requires administrator privileges.
"""
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

TASK_NAME = "CrossLister Daily"
SCRIPT_PATH = Path(__file__).parent / "main.py"
PYTHON_EXE = sys.executable
RUN_TIME = "09:00"


def create_scheduled_task() -> bool:
    """Creates the Windows scheduled task. Must be run as Administrator."""
    command = (
        f'"{PYTHON_EXE}" "{SCRIPT_PATH}"'
    )

    args = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", command,
        "/SC", "DAILY",
        "/ST", RUN_TIME,
        "/RL", "HIGHEST",
        "/F",
    ]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Scheduled task created: '%s' runs daily at %s", TASK_NAME, RUN_TIME)
            print(f"[OK] Task '{TASK_NAME}' scheduled for {RUN_TIME} daily.")
            return True
        else:
            logger.error("schtasks failed: %s", result.stderr)
            print(f"[ERROR] Failed to create task: {result.stderr}")
            if "access is denied" in result.stderr.lower():
                print("[HINT] Right-click this script and choose 'Run as administrator'.")
            return False
    except FileNotFoundError:
        logger.error("schtasks.exe not found — are you on Windows?")
        print("[ERROR] schtasks.exe not found. This feature requires Windows.")
        return False
    except subprocess.TimeoutExpired:
        logger.error("schtasks timed out")
        return False


def delete_scheduled_task() -> bool:
    """Removes the scheduled task."""
    args = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"[OK] Task '{TASK_NAME}' removed.")
            return True
        else:
            print(f"[ERROR] {result.stderr}")
            return False
    except Exception as e:
        logger.error("Delete task error: %s", e)
        return False


def check_task_exists() -> bool:
    """Returns True if the scheduled task already exists."""
    args = ["schtasks", "/Query", "/TN", TASK_NAME]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False


def run_task_now() -> bool:
    """Manually triggers the scheduled task immediately."""
    args = ["schtasks", "/Run", "/TN", TASK_NAME]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            print(f"[OK] Task '{TASK_NAME}' triggered manually.")
            return True
        else:
            print(f"[ERROR] {result.stderr}")
            return False
    except Exception as e:
        logger.error("Run task error: %s", e)
        return False


def show_task_status():
    """Prints the current status of the scheduled task."""
    args = ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"Task '{TASK_NAME}' not found.")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="CrossLister Task Scheduler")
    parser.add_argument("action", nargs="?", default="install",
                        choices=["install", "remove", "status", "run"],
                        help="Action to perform (default: install)")
    args = parser.parse_args()

    if args.action == "install":
        if check_task_exists():
            print(f"[INFO] Task '{TASK_NAME}' already exists.")
            show_task_status()
        else:
            create_scheduled_task()
    elif args.action == "remove":
        delete_scheduled_task()
    elif args.action == "status":
        show_task_status()
    elif args.action == "run":
        run_task_now()
