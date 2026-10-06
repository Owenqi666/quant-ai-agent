# 第二份受控合成数据

`market.csv` / `metadata.json` 用于数据导入、版本绑定、固定数值参考与恢复测试。

- 独立 seed：20261001；96 个合成工作日、7 个 ALT 资产，共 672 行。
- `research_config.json` 将前、中、后 32 日分别指定为 train、validation、test，`min_assets=5`。最终 test 区间保持保留。
- `generate.py` 复用已有明确 LCG 工具，重新生成不同日期、资产和数值；元信息记录两份生成器源文件摘要。
- 数值仅验证软件行为，不代表独立经济假设、真实市场表现或投资收益。

生成到新的检查目录，并逐字节比较三个数据文件：

```sh
.venv/bin/python tests/fixtures/datasets/generate.py --out /tmp/paper-alpha-second-fixture
```

不需要从网络下载数据，不接入 AI。
