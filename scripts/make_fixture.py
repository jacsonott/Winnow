#!/usr/bin/env python3
"""Generate a synthetic event-log CSV for perf testing.

    python make_fixture.py --rows 1200000 --out sample.csv
"""
import argparse, csv, datetime, random

PROCS = ["svchost.exe", "powershell.exe", "cmd.exe", "explorer.exe",
         "rundll32.exe", "lsass.exe", "chrome.exe", "wmiprvse.exe"]
USERS = ["ACME\\jsmith", "ACME\\admin", "NT AUTHORITY\\SYSTEM", "ACME\\bkupsvc"]
HOSTS = ["WKSTN-014", "WKSTN-002", "SRV-DC01", "SRV-FS02"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_200_000)
    ap.add_argument("--out", default="sample.csv")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    random.seed(a.seed)
    t0 = datetime.datetime(2026, 3, 14, 8, 0, 0)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "EventId", "Channel", "Computer", "User",
                    "Process", "CommandLine", "SourceIp", "Bytes", "Details"])
        for i in range(a.rows):
            t = t0 + datetime.timedelta(seconds=i * 0.37)
            p = random.choice(PROCS)
            w.writerow([
                t.strftime("%Y-%m-%d %H:%M:%S"),
                random.choice([4624, 4625, 4688, 1, 7045, 4104]),
                random.choice(["Security", "Sysmon", "System"]),
                random.choice(HOSTS), random.choice(USERS), p,
                f"C:\\Windows\\System32\\{p} -k netsvcs -id {i}",
                f"10.0.{random.randint(0, 5)}.{random.randint(1, 254)}",
                random.randint(0, 900_000),
                # ~0.1% needles to search and tag against
                "Routine" if random.random() > 0.001
                else "encoded command detected base64 IEX",
            ])
    print(f"{a.rows:,} rows -> {a.out}")


if __name__ == "__main__":
    main()
