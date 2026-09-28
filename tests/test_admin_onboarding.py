import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ADMIN_EMAIL = "technology@thermolio.com"


def test_onboarding_help_identifies_default_admin_email():
    result = subprocess.run(
        [sys.executable, ROOT / "scripts" / "onboard_pilot_user.py", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Defaults to" in result.stdout
    assert EXPECTED_ADMIN_EMAIL in result.stdout
