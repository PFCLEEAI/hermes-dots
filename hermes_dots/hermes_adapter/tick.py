"""No-agent cron script entry point (no model, Slack or Gmail network calls)."""

from .runtime import DotsRuntime, json_result


def main():
    runtime = DotsRuntime()
    try:
        print(json_result(runtime.tick()))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
