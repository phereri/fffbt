"""Launch the SELF-RECOVERING registration ladder on the Pixel 6 Pro.

Flow per attempt (RegistrationRunner): NoopRotator (ChangeDevice already done by
the operator) -> restore golden clean backup -> fingerprint snapshot -> MobileRun
agent registers. The ladder (RecipeLadder) advances to the next SMS recipe on a
recoverable failure (phone_verification_failed / no SMS) and stops on success,
a fatal reason (rate_limited / device_unreachable / account_suspended), or when
recipes are exhausted. On success the runner saves the bundle (app backup +
fingerprint.json + credentials.json).

Loads .env from the repo root (or C:\\fffbt\\.env as fallback) and forces
OPENAI_API_KEY = GOOGLE_API_KEY (agent's OpenAI-compat profiles target Google's
Gemini endpoint; the ShopAIKey is dead).
"""
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for env_path in [HERE / ".env", Path(r"C:\fffbt\.env")]:
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("="); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break
g = os.environ.get("GOOGLE_API_KEY", "")
if g: os.environ["OPENAI_API_KEY"] = g
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from src.registration.cli import main  # noqa: E402

DEFAULT_ARGS = [
    "register-loop",
    "--device-serial", "192.168.4.169:5555",  # LAN IP — survives ProxyConnector VPN
    "--clean-backup", r"clean_backups\com.instagram.android\clean_install",
    "--csv", "accounts.csv",
    "--artifacts-dir", r"artifacts\registration",
    "--backup-root", "app_backups",
    # default built-in ladder: 5sim austria(proven) -> luxembourg -> croatia -> czech -> smspool usa
]
if __name__ == "__main__":
    argv = sys.argv[1:] or DEFAULT_ARGS
    print("ARGS:", argv)
    print("LLM key (OPENAI_API_KEY):", os.environ.get("OPENAI_API_KEY","")[:6], "...")
    raise SystemExit(main(argv))
