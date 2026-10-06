# v0.9 数据与研究材料接入前检查

本轮交付离线声明和文件完整性检查。它不解析行情值，不运行因子或研究任务，不访问外网，也不改变现有执行器的 synthetic-only 限制和最终 test 隔离锁。随仓库提供的是空白模板；它们必须返回 `blocked`。测试中的完整表单由程序生成，不能作为实际行情、授权、人工标注或独立研究成果。

## 状态与证据边界

| 状态 | 条件 | 能说明什么 |
|---|---|---|
| `blocked` | 必填声明、文件、摘要、时间、已知开发重用或安全边界不满足 | 检查未完成，查看 `reasons`；不能执行 |
| `contract_complete` | 受控数据夹具的声明完整，或研究材料声明与冻结检查完成 | 当前声明结构和所引用文件通过这些有限检查；不能认证数据质量或科学独立性 |
| `ready_for_adapter_review` | `real_market`、`human` 来源及全部数据契约和文件检查通过 | 可进入来源专属适配器的人工评审；不是行情已经接入或验证 |

所有报告固定 `execution.enabled=false`、`synthetic_only_lock=true`、`final_test_unlocked=false`。`claims` 中的行情质量、授权核验、人类身份核验、科学独立性批准、行情时间保留验证和无泄漏核验全部为 `false`。填写 `human` 或 `declared_authorized` 只是声明；该工具没有身份、许可真实性或可信时间戳服务。

## 使用方式

在独立输入目录中保存填好的模板及它引用的文件。`--root` 必须是无符号链接的实际目录；例如 macOS `/tmp` 是链接时应使用实际解析后的目录。清单和全部 artifact 的 `path` 都相对于该目录。

```sh
.venv/bin/python scripts/check_research_readiness.py \
  --root /absolute/readiness-inputs \
  --manifest data.json \
  --out /absolute/readiness-reports/data-check-01
```

输出父目录需已存在，输出目录必须在输入根目录之外且尚不存在。成功或失败产物都不覆盖；下一次检查用新目录。`report.json` 保存完整结果，stdout 仅输出状态、报告路径、摘要和失败原因数量。契约完成返回退出码 0；`blocked`、不安全输入或输出目录冲突返回 2。文件系统拒绝写入时可能留下未完成目录，该目录也不能重用。

研究材料填写和人工核对后，先取得绑定摘要：

```sh
.venv/bin/python scripts/check_research_readiness.py \
  --root /absolute/readiness-inputs --manifest study.json --freeze-digest
```

此命令只打印 SHA-256，不修改表单、不冻结任务、不作批准。将结果人工写入 `freeze.input_digest`，声明 `frozen_at`、尚未执行等信息，然后检查：

```sh
.venv/bin/python scripts/check_research_readiness.py \
  --root /absolute/readiness-inputs --manifest study.json \
  --as-of 2026-10-01T12:00:00Z \
  --out /absolute/readiness-reports/study-check-01
```

`--as-of` 是显式、带时区的检查参考时间，用于可复现比较；缺失、非法或早于 `frozen_at` 时研究检查阻断。它不证明实际运行时钟，也无法证明材料此前从未执行。冻结摘要由整个清单去掉 `freeze` 对象后的规范 JSON 生成，绑定材料、声明、共享输入角色和各文件期望摘要；报告另保存原始清单摘要，包括整个 freeze 对象。材料变化必须重新冻结并保留旧记录；不能通过改时间恢复原来的研究独立性。

空白模板检查可直接运行，预期退出码为 2，不应被当成完整研究样本：

```sh
.venv/bin/python scripts/check_research_readiness.py \
  --root evaluation_suites/v09 --manifest readiness_real_data_template.json \
  --out artifacts/readiness-data-blank-01

.venv/bin/python scripts/check_research_readiness.py \
  --root evaluation_suites/v09 --manifest readiness_heldout_template.json \
  --as-of 2026-10-01T12:00:00Z --out artifacts/readiness-study-blank-01
```

## 数据清单

从 `evaluation_suites/v09/readiness_real_data_template.json` 复制。顶层严格要求 `schema_version=1`、`kind=real_data`、`id`、`data_kind`、`declaration_source`、`source`、`authorization`、`snapshots`、`field_mapping`、`semantics`。所有对象拒绝未知字段；空值和明确的 unknown/TBD 等占位声明不能通过。

- `data_kind` 为 `real_market` 或 `controlled_fixture`。`declaration_source` 为 `human` 或 `automation`；真实行情声明必须来自 `human`。
- `source` 包括提供方 `provider`、数据集 `dataset`、版本 `version`、来源引用 `reference`。引用只保存为文字，不下载或认证。
- `authorization` 包括 `status=declared_authorized`、依据 `basis`、许可用途 `permitted_use`、`evidence` 文件。必须由使用者提供实际依据；工具只核对声明存在和文件字节。
- `snapshots` 每项包含唯一 `id`、`role`、`artifact`。必须至少有 `market_data`、`calendar`、`universe` 三种角色；可补 `corporate_actions`。分开的角色需有独立文件路径。
- `field_mapping` 每项包含唯一目标 `target`、来源字段 `source`、单位 `unit`、可用时间规则 `availability_time_rule`、修订规则 `revision_policy`、指向 `market_data` 快照的 `snapshot_id`。规则需写明哪些信息在何时能够取得；检查器不会解释自然语言规则或判断其正确性。
- `semantics` 要求时区 `timezone`、时间/资产键 `timestamp_key` / `asset_key`、交易日历引用与规则 `calendar_snapshot_id` / `calendar_rule`、复权及其可用时点 `adjustment_rule` / `adjustment_as_of_rule`、历史股票池引用与规则 `universe_snapshot_id` / `historical_universe_rule`，以及缺值 `missing_value_rule`、停牌 `suspension_rule`、退市 `delisting_rule`。日历和股票池引用须指向对应角色快照。

每个 `artifact` 严格为 `{ "path": "relative/file", "sha256": "64位小写十六进制" }`。这只证明检查时读到的字节符合声明。字段是否真的存在、价格单位、复权处理、可用时点、交易日历、历史成员、退市收益等都留给后续来源专属适配和数据质量测试。64 MiB 单文件限制适用于接入前小型快照；大型来源应先确定分片与适配计划，不能直接在本检查中扩大无界读取。

## 研究材料清单与开发重用

从 `evaluation_suites/v09/readiness_heldout_template.json` 复制。顶层严格要求 `schema_version=1`、`kind=heldout_study`、`id`、`material_kind`、`paper`、`task`、`manual_expected`、`additional_inputs`、`development_exposure`、`freeze`。

- `material_kind` 为实际待审材料声明 `research_material` 或明确受控夹具 `controlled_fixture`。
- `paper` 包含 `id`、`title`、`source_reference` 和论文文件 `artifact`；`task` 包含唯一任务 `id` 和 `artifact`。
- `manual_expected` 包含 `source=human`、`declarant`、`label_status=completed`、预期文件 `artifact`。工具不生成人工答案，不解析或认证答案内容。
- `development_exposure` 要求 `source=human`、声明人 `declarant`，并明确 `paper_used=false`、`task_used=false`、`expected_used=false`。缺失或未知不能替代否认声明。
- `freeze` 要求 `status=frozen`、带时区的 `frozen_at`、正确的 `input_digest`、`execution_state=not_started`、`execution_started_at=null`。
- `additional_inputs` 可为空数组；模板中的占位行应删除或完整填写。每项必须有唯一 `id`、`role` 和 `artifact`。`role=heldout_material` 表示同样应避开开发重用的额外研究材料；`role=shared_context` 表示明确共用的上下文，例如已有合成行情。

检查器固定读取仓库 v04/v05/v06 开发清单和 Alpha101 论文身份，生成附带源文件摘要的 `development_registry`；调用者不能传入空登记替换它。论文 ID（包括常见 arXiv 版本/URL 写法）、规范化标题、已知任务 ID 和文件摘要用于发现开发重用。重命名 Alpha101 的文件、标题或任务不能消除原文件摘要冲突。登记文件缺失或损坏也阻断。

论文、核心任务、人工预期和 `heldout_material` 的已知开发字节重用会阻断。核心三种材料不能通过添加 `role=shared_context` 绕过检查，因为这些对象不接受该字段。`shared_context` 的已知 ID/摘要重用保存在 `shared_development_context`，不会单凭共享行情阻断新的研究材料声明；每个文件回执也保存用途。将同样字节重新声明为 `heldout_material` 仍会阻断。

这里区分“新论文/研究任务保留”与“市场数据的时间保留”。用新论文研究同一合成行情，可以完成研究材料契约，但没有产生新的行情保留区间，也没有证明无泄漏。登记表只能发现已知开发输入的明确重用，不能识别所有语义改写、未知开发历史或不实声明。对语义改写、科学独立性和真实人工预期的判断仍需人工评审；`contract_complete` 不是 held-out 研究已经获批。

## 文件边界与报告复现

根目录及逐层输入路径通过目录文件描述符、`O_NOFOLLOW` 打开；拒绝符号链接、硬链接、目录/FIFO 等非独立常规文件。叶节点采用 `O_NONBLOCK`，避免 FIFO 阻塞。路径必须相对、最长 512 字符和 16 层，不允许绝对路径、空组件、`.`、`..`、反斜杠、冒号或 NUL。使用前后核对 inode/大小/修改时间，读完重新从根打开名称；摘要改变或检查期间检测到替换/修改会阻断。它不是锁定整个文件系统的快照，报告描述的是检查期间读到的文件。

清单最大 1 MiB，单个 artifact 最大 64 MiB，输入累计最大 256 MiB，最多 32 个 artifact；快照/额外研究输入分别最多 16 个，字段映射最多 128 项。空文件、重复路径、重复 ID、重复目标字段、重复 JSON key 和非有限数值拒绝。文件仅流式计算 SHA-256，只有清单/可信开发登记作为 JSON 解析；不解析行情或执行其中内容。

报告包含版本 `research-readiness-v1`、检查模块源码 SHA、原始清单 SHA、绑定契约 SHA、实际文件 SHA/大小/引用位置、开发登记来源 SHA、明确参考时间、状态、逐项失败原因和固定限制。`report_sha256` 是报告去掉自身摘要字段后的规范 JSON SHA-256。规范 JSON 按键排序、UTF-8、紧凑分隔、末尾换行；相同输入字节、代码、登记和参考时间产生相同报告。输出目录名称不参与摘要。安装/发布包需保留上述开发登记文件，不能单独复制脚本后静默跳过登记。

## 定向验证

```sh
.venv/bin/python -m unittest tests.test_research_readiness -v
```

测试只使用临时受控声明和已公开在仓库的合成开发材料，覆盖空模板、授权/时间/规则缺失、摘要漂移、冻结改变、Alpha101 重命名、共享行情角色、路径穿越/链接/FIFO/大小界限、检查期间变更、严格 JSON，以及失败产物保留和输出不覆盖。成功分支证明检查器契约行为，不能推导真实数据已验证、人工预期已存在或研究效果提高。
