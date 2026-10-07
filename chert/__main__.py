"""Run the project-based Discord application with python -m chert."""

from dotenv import load_dotenv
from chert.paths import ROOT


def main():
    load_dotenv(ROOT / ".env")
    from chert.application import main as run

    run()


if __name__ == "__main__":
    main()
