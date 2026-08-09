import subprocess
import sys
import time

marker = sys.argv[1]
subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import pathlib, sys, time; time.sleep(0.3); pathlib.Path(sys.argv[1]).write_text('orphan')",
        marker,
    ]
)
time.sleep(2)
