"""Portfolio packaging helper added in September 2026; not original internship code."""
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from renderer.dashboard_renderer import run

if "--recalculate" in sys.argv:
    env = {**os.environ, "PP_CLIENT_SCOPE": "FULL_XML"}
    subprocess.run([sys.executable, str(ROOT / "bin/pp-prototype-cross-platform"),
                    str(ROOT / "examples/DEMO-portfolio.xml"), str(ROOT / "output/demo")], env=env, check=True)
    report, series = ROOT / "output/demo/report.json", ROOT / "output/demo/performance_series.csv"
else:
    report, series = ROOT / "examples/DEMO-report.json", ROOT / "examples/DEMO-performance_series.csv"
target = ROOT / "output/DEMO-dashboard.html"
status = run(["--report", str(report), "--series", str(series), "--output", str(target)])
if status:
    raise SystemExit(status)
html = target.read_text(encoding="utf-8")
banner = '<div style="padding:14px;background:#fff4cc;color:#302400;text-align:center;font:16px sans-serif">DEMO — entirely synthetic cash flows. No client data. Not investment performance.</div>'
target.write_text(re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, html, count=1), encoding="utf-8")
print("Open", target)
