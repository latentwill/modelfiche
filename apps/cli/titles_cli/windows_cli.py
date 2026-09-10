from .desktop import configure_bundle


def main() -> None:
    configure_bundle()
    from .main import app
    app()


if __name__ == "__main__":
    main()
