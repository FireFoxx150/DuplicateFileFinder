"""Scheduled scans: a systemd user timer that runs dupefinder-cli on a folder and notifies you."""
import os

SERVICE = "dupefinder-scan.service"
TIMER = "dupefinder-scan.timer"


def unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user")


def _quote(arg):
    # systemd unit syntax: double-quote, escape backslashes and quotes, and double any % sign
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_text(folder, hours, cli_path, report_path):
    service = (
        "[Unit]\n"
        f"Description=Scan {folder} for duplicate files\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_quote(cli_path)} {_quote(folder)} --json --notify --html {_quote(report_path)}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=Run the duplicate scan of {folder} every {hours} hours\n\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        f"OnUnitActiveSec={hours}h\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def write_units(folder, hours, cli_path, report_path, out_dir=None):
    """Write the service and timer files. Returns the two paths. Does not start anything."""
    out_dir = out_dir or unit_dir()
    os.makedirs(out_dir, exist_ok=True)
    service, timer = unit_text(folder, hours, cli_path, report_path)
    paths = []
    for name, text in ((SERVICE, service), (TIMER, timer)):
        path = os.path.join(out_dir, name)
        with open(path, "w") as f:
            f.write(text)
        paths.append(path)
    return paths
