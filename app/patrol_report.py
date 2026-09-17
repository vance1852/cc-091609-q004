"""命令行入口：还原一次断网巡护的完整监管结论。

用法：
    python -m app.patrol_report fixtures/harvest_patrol.json
"""

import argparse
import json

from .scenario import Scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="可持续采集巡护还原")
    parser.add_argument("fixture", help="巡护资料 JSON 路径")
    parser.add_argument("--out", help="将还原报告写入该 JSON 文件")
    args = parser.parse_args()

    report = Scenario.from_file(args.fixture).run()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
