# 野生药材可持续采集监管

自然保护地羌活采集的许可、边界、断网事件与申诉复核台账。定位原值、设备
精度与边界判定依据随每条记录永久留存；跨越边界或突破任一限额的采集进入
隔离，不计入合法收获。

## 领域规则

- **许可审批即冻结**：许可绑定具体的资源调查版本 `survey_version` 与地块
  地理版本 `parcel_version`（`domain/registry.py`）。事后升版调查、调整
  核心区，不改变已批许可的判定依据；一级保护物种不得签发许可。
- **离线签发、归位去重**：设备断网时自增序号；恢复同步后按
  `(device_id, sequence)` 去重（重传保留首条并留痕），其余事件按实际
  `captured_at` 归位，依采集时刻顺序计量（`HarvestLedger.ingest_patrol`）。
- **四本限额账**：许可、团队年度、地块年度、物种年度。任一超限即隔离，
  隔离质量不占用余额（`QuarantineReason`）。
- **空间存疑从禁**：以“原点 ± 设备精度圆盘”判定地块与核心保护区
  （`domain/geo.py`）。圆盘跨边界为存疑，同样隔离；`BoundaryVerdict`
  保留原点、精度、到地块/核心区边界距离与冻结的地理版本。
- **许可生命周期**：仅尚未使用（无合法收获）的许可可暂停；暂停日前的
  采集按当时状态仍有效。
- **勘误只追加**：原始事件不可变，勘误以追加链保存，余额按最新勘误量
  计量；对合法记录上调勘误若顶破限额将被拒绝。
- **申诉第三人复核**：复核人不得是申诉提出人，也不得是原承办巡护员；
  解除隔离时重新过一遍空间与四本账，放行不能冲垮年度限额。
- **只追加审计链**：每次状态变化写入 SHA-256 哈希链（`domain/audit.py`），
  事后篡改会被 `Journal.verify()` 发现。

## 目录

```
domain/contracts.py   枚举与不可变值对象（许可/物种/地块/事件/年度限额）
domain/geo.py         点在多边形、精度缓冲三态判定与判定依据
domain/registry.py    调查/地理版本注册表与许可冻结
domain/ledger.py      去重归位、限额判定、隔离、暂停、勘误、申诉、巡护还原
domain/audit.py       只追加哈希链审计日志
app/scenario.py       巡护 fixture 加载与处置动作执行
app/patrol_report.py  命令行：输出一次巡护的完整还原报告
fixtures/             巡护资料（脱敏原件存为 harvest_patrol.desensitized.json）
tests/                32 个 unittest 用例
```

## 运行

```bash
python -m compileall domain          # 契约语法检查（Python 3.11）
python -m unittest discover -s tests -t .   # 或 python -m unittest tests.test_geo tests.test_ledger tests.test_audit
python -m app.patrol_report fixtures/harvest_patrol.json
```

巡护报告包含：各许可的限额/已用/剩余余额、每条事件的处置与越界理由、
定位原点与缓冲依据、勘误链、申诉结果，以及按巡护过滤的完整状态变化
时间线和哈希链完整性结论。
