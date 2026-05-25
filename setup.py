"""One-shot setup: installs deps, creates .env, initializes DB."""

import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).parent


def run(cmd: list[str], **kwargs):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: command failed with code {result.returncode}")
        sys.exit(1)


def main():
    print("\n=== Polymarket Edge Monitor — Setup ===\n")

    # 1. Create virtualenv
    venv_path = ROOT / ".venv"
    if not venv_path.exists():
        print("Creating virtual environment...")
        run([sys.executable, "-m", "venv", str(venv_path)])
    else:
        print("Virtual environment already exists.")

    # 2. Determine pip path
    if sys.platform == "win32":
        pip = str(venv_path / "Scripts" / "pip.exe")
        python = str(venv_path / "Scripts" / "python.exe")
    else:
        pip = str(venv_path / "bin" / "pip")
        python = str(venv_path / "bin" / "python")

    # 3. Install dependencies
    print("\nInstalling dependencies...")
    run([pip, "install", "--upgrade", "pip", "-q"])
    run([pip, "install", "-r", str(ROOT / "requirements.txt"), "-q"])

    # 4. Copy .env if missing
    env_file = ROOT / ".env"
    env_example = ROOT / ".env.example"
    if not env_file.exists():
        shutil.copy(env_example, env_file)
        print(f"\n.env created from .env.example — edit it to add your API keys.")
    else:
        print("\n.env already exists — skipping.")

    # 5. Initialize DB
    print("\nInitializing database...")
    run([python, str(ROOT / "main.py"), "--init"])

    # 6. Done
    print("\n=== Setup Complete ===")
    print(f"\nActivate the environment:")
    if sys.platform == "win32":
        print(f"  .venv\\Scripts\\activate")
    else:
        print(f"  source .venv/bin/activate")
    print(f"\nThen run:")
    print(f"  python main.py              # start full 24/7 monitor")
    print(f"  python main.py --signals    # view current edge signals")
    print(f"  python main.py --arb        # view arbitrage opportunities")
    print(f"  python main.py --whales     # view whale activity")
    print(f"  streamlit run dashboard/app.py  # launch web dashboard")
    print()


if __name__ == "__main__":
    main()
