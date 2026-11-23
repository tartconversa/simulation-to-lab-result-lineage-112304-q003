# 仿真与实测结果的谱系连接和差异解释

数据目录由本地合成科研记录组成，不包含真实实验室、研究人员或实验结果。使用 Python 3.11 至 3.14 和标准库，无需外部服务。

```sh
python3 -m unittest discover -s tests -v
```

Windows PowerShell 可运行 `python -m unittest discover -s tests -v`。字段定义见 `docs/资料契约.md`。当前基线提供输入资料和结构校验，不包含科研分析或数据湖业务能力。
