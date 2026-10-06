# v0.6 结构化数据诊断

## 范围与权威实现

`paper_alpha.evaluation.load_market` 继续作为导入和实际计算共同使用的严格市场数据入口。输入只支持已明确标为 synthetic 的完整日频 OHLCV 网格，数据来源、交易日历、复权与真实市场字段可得性适配仍未实现。

本轮为失败增加 `DataValidationError(ValueError)`，携带稳定的 `code`、`location` 和 `samples`。导入服务直接使用异常属性，不再匹配英文错误消息推断业务错误码。表达式计算、延迟收益标签、权重、切分与评估算法没有改动。

文件、网格和执行限制保持原有边界：执行 loader 的单文件上限为 64 MiB、面板最多 2,000,000 个日期与资产单元；导入 CSV 为 16 MiB、元数据为 2 MiB、最多 100,000 个日期与资产单元，验证子进程限时 30 秒。CSV 结构扫描与 pandas 读取均最多读取声明行数加一条数据记录；文件解码及输入摘要仍需读取受限文件本身。数据不会自动补齐、去重、排序、修改值或修复摘要。

## 报告与位置契约

新增验证报告为 `schema_version: 2`、`validator.version: synthetic-dataset-import-v2`。校验器摘要包含 `market_diagnostics.py`；旧报告保留原文件、原摘要和原版本，读取兼容由 API 契约处理，不能把旧版无位置诊断补写成新版结果。对尚未注册的旧验证结果进行注册时，沿用已有校验器摘要检查，要求以新幂等键重新验证。

```json
{
  "mode": "fail_fast",
  "detected_error_count": 1,
  "all_errors_enumerated": false,
  "sample_limit": 5,
  "row_numbering": "csv_logical_record_1_based_header_included",
  "first_error_location": {
    "row": 3,
    "column": "date,asset",
    "date": "2024-01-02",
    "asset": "A",
    "related_rows": [2],
    "value": null,
    "reason": "duplicate_key"
  },
  "samples": [
    {
      "row": 3,
      "column": "date,asset",
      "date": "2024-01-02",
      "asset": "A",
      "related_rows": [2],
      "value": null,
      "reason": "duplicate_key"
    }
  ],
  "sample_note": "Only the first failed rule is reported, with at most five samples; this is not an enumeration of all invalid cells. CSV rows count logical records including the header and blank records. Missing grid entries and metadata have no CSV row."
}
```

1. `row` 是 CSV 逻辑记录号，从 1 起计，计入表头和被忽略的空记录。通常表头为 1，首条数据为 2；如果文件以空记录开头，表头位置相应后移。带引号字段跨越多条物理文本行时，仍只算一个逻辑记录，不是编辑器行号。
2. `column` 是字段名；联合键为 `date,asset`，OHLC 联合边界为 `open,high,low,close`。元数据错误使用元数据字段名。
3. 缺失的网格条目没有实际输入行，`row` 必须为 `null`；元数据错误同样没有 CSV 行号。
4. 重复键 `related_rows` 指向首次出现该键的记录。时间乱序指向上一条记录；派生收益溢出指向上一个交易日同资产的 close 记录。
5. 每次只返回首个失败规则，最多五个样本，不统计全部错误。`detected_error_count: 1` 表示已发现一个失败规则，不代表只有一个坏单元。成功时为 0，位置为 null，样本为空；成功报告也不表示枚举了所有潜在经济或市场语义问题。
6. 每个位置内的字符串最多 160 字符、引用行最多五个。值按文本保存，不把 NaN/Infinity 作为 JSON 数值返回。元数据或执行失败可以只有 `first_error_location` 而没有样本；不能把缺少位置解释为没有错误。

## 主要稳定错误码

| code | 含义 | 位置 |
|---|---|---|
| `checksum_mismatch` | 市场文件摘要与元数据不符 | 元数据字段 |
| `columns_mismatch` | CSV 或元数据列不符 | 表头记录或元数据字段 |
| `invalid_csv_encoding` | 非 UTF-8 输入 | 无伪造记录号 |
| `invalid_csv_structure` | CSV 引号、列数、NUL 或解析不一致 | 能确定的逻辑记录，否则 null |
| `missing_key` | 日期或资产键为空 | 实际行、单字段 |
| `invalid_date` | 非 ISO 日期或不存在的日期 | 实际行或元数据字段 |
| `duplicate_key` | 日期与资产联合键重复 | 重复行与首次出现行 |
| `date_order` | CSV 日期或元数据日历非递增 | CSV 当前行与上一行，或元数据字段 |
| `grid_mismatch` | 缺失或多余网格条目、元数据数量错误 | 多余键实际行；缺键 row=null |
| `numeric_type` | OHLCV 不能转为数字 | 实际单元 |
| `nonfinite_value` | OHLCV 是 NaN/Infinity 等非有限数 | 实际单元 |
| `nonpositive_value` | 价格或成交量非正 | 实际单元 |
| `ohlc_bounds` | low/high/open/close 相互边界不成立 | 实际行、联合字段 |
| `nonfinite_derived_return` | close 比值产生非有限收益 | 当前 close 与上一交易日引用 |
| `unsupported_data_kind` | 不支持真实市场语义 | data_kind 元数据字段 |
| `unsupported_field_availability` | 不支持给定可得时间规则 | field_availability 元数据字段 |
| `invalid_metadata_json` | 非严格 JSON、重复键或非有限常量 | 不输出未经裁剪的解析器消息 |
| `import_cell_limit` / `panel_cell_limit` | 超过声明网格预算 | 无伪造行 |

其他结构错误使用明确的 `invalid_metadata`、`invalid_calendar`、`invalid_universe`、`duplicate_calendar`、`duplicate_universe`、`invalid_generator`、`invalid_grid_metadata`。输入读写与一致性有 `unsafe_input_file`、`input_file_limit`、`input_file_changed`、`input_digest_mismatch`、`validator_changed`。未分类操作系统或解析异常仅暴露稳定的阶段失败码，不回传可能包含服务器绝对路径或任意输入的大段异常文本。

CSV 先由标准库扫描列宽和逻辑记录号，再交给原有 pandas 最终判断引号与数值是否合法。标准库扫描使用兼容模式，保留原 pandas 接受的闭引号后空格或 tab；它不单独改变引用字段的合法性规则。

有意新增两项结构加固：含 NUL 的 CSV 明确拒绝，避免旧 parser 截断字段；数据列数必须与表头相同，拒绝旧 pandas 会自动猜测为索引的额外无表头首列。这两类输入的接受范围有意收紧，不声明全部历史 CSV 字节均兼容。合法 OHLCV 数值、字段规则与评估算法保持不变。标准库的进程级字段长度设置在受锁保护的结构扫描期间临时提高到已受限文件长度，退出后恢复，避免无意增加默认 128 KiB 的单字段限制。

## 验证方式

```sh
.venv/bin/python -m unittest tests.test_dataset_diagnostics tests.test_evaluation tests.test_dataset_imports -q
```

覆盖真实 CSV 记录定位、重复引用、空键、缺失/多余网格、乱序、非数字/非有限/非正值、OHLC 边界、收益溢出、五样本限制、长文本裁剪、BOM/空记录/多行引用/闭引号后空格与 tab、额外无表头索引列拒绝、异常 CSV、额外记录读取上限、路径保护、文件未改动、合法面板结果及真实导入子进程与计算 loader 的一致性。API 新旧报告序列化由 API 契约测试单独验收。
