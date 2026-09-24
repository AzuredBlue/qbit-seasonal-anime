import sys
import uvicorn


def main():
    port = 8085
    for i, arg in enumerate(sys.argv[1:]):
        if arg in ("-p", "--port") and i + 1 < len(sys.argv) - 1:
            try:
                port = int(sys.argv[i + 2])
            except ValueError:
                pass
        elif arg.isdigit():
            port = int(arg)

    print(f"Starting qbit-seasonal-anime on http://0.0.0.0:{port}")
    uvicorn.run(
        "qbit_seasonal_anime.server.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
