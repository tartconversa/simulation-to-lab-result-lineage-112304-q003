# 仿真与实测结果的谱系连接和差异解释

数据目录由本地合成科研记录组成，不包含真实实验室、研究人员或实验结果。
使用 Python 3.11 至 3.14 和标准库，无需外部服务。

本仓库在基线资料校验之上提供完整数据应用：持续接入仿真批次与实验实测、
建立版本化验证关系（验证 / 否定 / 尚不可比较）、维护双负责人签收与修订台账，
并支持按历史时点重建报告。**仿真输出与实测记录是两类观测，不会被混为同一种数据。**

## 快速开始

```sh
# 运行全部测试（31 个用例，标准库 unittest）
python3 -m unittest discover -s tests -v

# 生成受控合成夹具并走完整流程（src 布局需设置 PYTHONPATH）
PYTHONPATH=src python3 -m simulation_to_lab_result_lineage make-fixture --out /tmp/demo/v1
PYTHONPATH=src python3 -m simulation_to_lab_result_lineage make-fixture --out /tmp/demo/v2 --update
PYTHONPATH=src python3 -m simulation_to_lab_result_lineage ingest --data-dir /tmp/demo/v1 --db /tmp/demo/app.sqlite
PYTHONPATH=src python3 -m simulation_to_lab_result_lineage build-facts --db /tmp/demo/app.sqlite
PYTHONPATH=src python3 -m simulation_to_lab_result_lineage report --db /tmp/demo/app.sqlite
```

Windows PowerShell 将 `PYTHONPATH=src` 改为先执行 `$env:PYTHONPATH="src"`，
或直接 `pip install -e .` 后省略该变量。

## 目录

- `src/simulation_to_lab_result_lineage/`
  - `schema.py`：SQLite 表结构（原始表仅追加、事实表版本化）
  - `ingest.py`：CSV 接入、内容哈希去重、问题行隔离台账
  - `domain.py`：可比性与验证/否定判定纯函数
  - `store.py`：事实版本、待复核、样品更正、双签收、历史报告重建
  - `synthetic.py`：覆盖全部结论分支的受控合成数据生成器
  - `cli.py` / `__main__.py`：命令行入口（支持 `--json`）
- `tests/`：领域规则、接入、生命周期、CLI 端到端测试
- `docs/资料契约.md`：上游 CSV 字段契约；`docs/应用说明.md`：完整中文说明
- `data/`：基线合成资料（90 仿真 / 75 实验 / 120 测量 / 60 链接）

## 核心规则摘要

- 仅当同指标、同单位、实验批次 `complete` 且双侧有测量时才可比；
  容差 `z·√(u_sim²+u_lab²)` 内为验证，否则为否定，默认 z=2。
- 单位不一致、缺测、失败/中止实验、无共同指标一律记为尚不可比较并附原因码。
- 新增实测或样品更正使受影响结论进入待复核；重算追加新版本，旧版本与旧报告
  可按时间点完整重建，原始行永不覆盖。
