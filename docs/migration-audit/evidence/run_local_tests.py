"""Run only against a verified local disposable DB; never print credentials."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from mazz.config import get_settings
from sqlalchemy.engine import make_url

name, logfile, *args = sys.argv[1:]
if not name.startswith("mazz_codex_") or not name.endswith("_test"):
    raise SystemExit("refused: disposable mazz_codex_*_test required")
url = make_url(get_settings().database_url)
if url.host not in {"localhost", "127.0.0.1", "::1"}:
    raise SystemExit("refused: configured database host is not local")
if url.database == name:
    raise SystemExit("refused: target is configured application database")
env = os.environ.copy()
env["TEST_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
env["PATH"] = r"C:\Program Files\PostgreSQL\17\bin" + os.pathsep + env.get("PATH", "")
print(f"verified host={url.host} port={url.port or 5432} database={name}; fixture TEST_DATABASE_URL; providers stubbed", flush=True)
with open(ROOT / logfile, "w", encoding="utf-8") as out:
    out.write(f"database={name}; host={url.host}; root={ROOT}; Python={sys.version.split()[0]}\n")
    out.flush()
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    out.write(f"\nexit={result.returncode}\n")
print(f"exit={result.returncode}; log={logfile}")
raise SystemExit(result.returncode)
