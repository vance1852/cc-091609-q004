# 野生药材采集监管

该项目记录资源调查、采集许可、地块边界和离线巡护事件。定位原值、设备精度与边界判定依据均作为巡护记录保存。

`domain/contracts.py` 定义许可、限额和采集事件，`fixtures/harvest_patrol.json` 是一次脱敏的断网巡护资料。运行 `python -m compileall domain` 可在 Python 3.11 下检查契约。
