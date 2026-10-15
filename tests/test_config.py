import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_env_file_is_read_from_working_directory(tmp_path):
    # The Windows installer runs the code from Program Files with each user's .env in the cwd.
    (tmp_path / ".env").write_text("FOXPROFILE_HUB_URL=http://192.0.2.10:8000\n")
    env = {k: v for k, v in os.environ.items() if k != "FOXPROFILE_HUB_URL"}
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", "from src.core.config import HUB_URL; print(HUB_URL)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "http://192.0.2.10:8000"
