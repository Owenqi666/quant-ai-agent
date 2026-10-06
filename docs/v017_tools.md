# v0.17 受限研究工具与会话

这一层供后续模型适配器读取已完成的研究产物、提交下一步建议。当前没有模型提供商、提示词执行器、自动研究循环或模型费用统计。会话绑定一个已有 `ResearchCase` 的 ID 和摘要，不创建实验，也不改变原始结果。

## 工具权限与控制面

向未来模型开放的工具表只有四项：

| 工具 | 参数 | 返回内容 |
|---|---|---|
| `read_case` | `{}` | `case_json`：完整冻结任务，包括结果来源、方法、状态、允许动作和限制 |
| `read_evidence` | `{}` | `evidence_json`：任务证据列表及各自的核验范围 |
| `read_result` | `{}` | `result_json`：结果列表，包含原结果 ID、摘要及规范化 `payload_json` |
| `propose_next_action` | `action`、`rationale`、`evidence_ids` | `proposal`：未核实的自动化建议草稿 |

三个读取字段均是服务端生成的规范化 JSON 字符串。成功时只填充对应字段，其他输出字段为空。结果中的数字来自绑定的领域服务；不会从模型请求中接收扫描计数、指标或组合收益。

建议的 `action` 必须属于该任务的 `allowed_actions`，引用必须属于该任务的证据 ID。建议固定为 `actor=automation`、`status=draft`、`semantic_fidelity=unverified`。证据 ID 合法只证明引用对象存在，不能证明自由文本忠于证据。即使建议文字描述“已通过”，它也不是审核记录或执行结果。

会话创建、列表、详情和能力说明属于用户／应用控制面，不是模型工具。创建会话时指定不可修改的预算。新幂等键可以创建另一个会话，因此未来模型适配器不能把创建会话能力交给模型，让模型自行重置预算。

工具不开放扫描导入、人工审核、阈值修改、作者源组合执行、最终测试、任意文件读取、网络请求、Python 或 SQL 执行。`request_human_review` 只保存建议，不发送消息、不创建人工批准；`revise_plan` 不改计划；`stop_data_insufficient` 不改原运行状态。

上述限制是本地工具入口的权限边界。工作台仍是本地单用户应用，人工／自动化身份属于本地声明；这一层不是公网多用户身份认证，也不阻止有本机权限的用户直接操作数据库或其他管理接口。

## HTTP 接口

| 方法与路径 | 含义 |
|---|---|
| `GET /api/research-tools/capabilities` | 查看实际工具表、禁止能力和预算政策 |
| `POST /api/research-tool-sessions` | 为已有任务创建固定预算会话 |
| `GET /api/research-tool-sessions?case_id=...&limit=20&offset=0` | 可按任务过滤的会话摘要列表 |
| `GET /api/research-tool-sessions/{session_id}` | 查看会话详情及完整调用记录 |
| `POST /api/research-tool-sessions/{session_id}/calls` | 调用一个受限工具 |

列表返回 `items`、`total`、`limit`、`offset`。列表项包含任务绑定、预算、使用量和状态，省略 `calls`；调用记录通过详情读取，避免列表重复传输大结果。

会话详情和历史调用重放是账本读取，不代表此刻重新认证了原始数据。每次实际执行一个读取工具或合法建议时，服务重新获取并验证绑定的任务及其来源；已保存的历史调用保留原内容。

## 最小调用示例

先从工作台已有研究任务详情取得真实 `id` 和 `digest`。下例尖括号内容均是待替换占位符，不是项目已有身份。创建会话会写入当前工作台；做开发验证时应使用隔离工作区启动的服务。

```bash
curl --fail-with-body http://127.0.0.1:8765/api/research-tools/capabilities

curl --fail-with-body \
  -H 'Content-Type: application/json' \
  --data-binary '{"case_id":"<CASE_ID>","case_digest":"<CASE_DIGEST>","budget":{"max_calls":8,"max_errors":2,"max_seconds":30},"idempotency_key":"manual-session-01"}' \
  http://127.0.0.1:8765/api/research-tool-sessions

curl --fail-with-body \
  -H 'Content-Type: application/json' \
  --data-binary '{"tool":"read_result","arguments":{},"idempotency_key":"read-result-01"}' \
  'http://127.0.0.1:8765/api/research-tool-sessions/<SESSION_ID>/calls'
```

`<SESSION_ID>` 使用创建响应的 `id`。保存这个身份与两次请求的幂等键；网络中断后，先读取会话详情，再用原键和原参数重放，不要立即创建新会话。

建议请求的形状如下；`<ALLOWED_ACTION>` 和 `<EVIDENCE_ID>` 必须取自对应任务，理由是待审核的自动化文本：

```json
{
  "tool": "propose_next_action",
  "arguments": {
    "action": "<ALLOWED_ACTION>",
    "rationale": "<待人工判断的建议理由>",
    "evidence_ids": ["<EVIDENCE_ID>"]
  },
  "idempotency_key": "proposal-01"
}
```

调用响应保存 `id`、顺序号、工具名、规范化参数、请求摘要、开始／结束时间、预算计费用时、状态及 `response`。HTTP 成功返回不意味着工具成功，必须检查 `response.ok`；科学状态仍以任务与结果中的状态为准。

## 预算与持久性

创建时的三项预算范围为：

| 字段 | 范围 | 计费口径 |
|---|---|---|
| `max_calls` | 1–100 | 进入执行账本的新请求次数，包括失败、拒绝和中断 |
| `max_errors` | 1–20 | 已记录的失败或中断调用次数，是整个会话累计上限 |
| `max_seconds` | 1–600 秒 | 累计执行计费用时；闲置和查看历史不收费 |

每个新调用在执行前先提交持久记录并占用一次调用额度。完成后写入结果和用时。重启、重新打开页面或同键重放都不会增加预算。会话另有 **16 MiB 的累计 UTF-8 调用记录上限**，并为后续有界请求的错误记录预留空间。输出超过可用空间会被丢弃，保存 `RESPONSE_TOO_LARGE`，关闭会话。

会话使用操作系统文件锁串行执行，不在核验原产物时一直持有数据库写锁。并发调用者收到 `SESSION_BUSY`，应保持原键等待重试；它没有占用另一次调用额度。调用计数和按顺序计算的账本摘要随记录原子更新，读取时检查，所以漏删末尾调用不会默默恢复额度。这是存储一致性检查，不是对能够同时重写数据库内容与校验值的管理员进行防篡改认证。

`data_insufficient`、`rules_unresolved` 等科学状态可以通过一次成功读取返回。它们不消耗错误额度，也不触发自动重试。会话本身不运行重试循环；调用方必须根据错误种类、允许动作和剩余预算决定下一步。

## 时间上限与执行中断

`elapsed_seconds` 是预算计费字段。正常完成时记录工具执行耗时；执行中的记录暂时预留该会话剩余的全部时间，以防进程退出后无法知道实际消耗。它不能直接作为研究人员实际操作耗时。

源产物核验是本地同步读取，目前没有独立进程的强制超时终止。服务在执行前后检查时间预算；超过剩余额度时丢弃输出，保存 `TIME_BUDGET_EXHAUSTED`。因此时间预算限制结果的接受和后续调用，不承诺操作系统会在指定秒数立即结束一个正在进行的读取。

进程异常退出可能留下 `running` 调用。读取会话详情只展示该记录及已预留额度，不自动退费。下一次调用取得会话锁后，将未完成记录持久化为 `interrupted`，返回／保存 `CALL_INTERRUPTED`，保守扣除原预留时间。即使随后使用不同键的调用因额度不足被拒，这次恢复记录也会保留。原键重放可取得恢复后的中断记录；不会重做可能已执行过的读取。

## 幂等与失败处理

幂等键最多 128 个字符，不能为空白。会话创建键在工作区内唯一；调用键在同一会话内唯一。同键同参数重放原身份或调用记录，不重新执行工具、不重复扣费；同键换参数返回 `IDEMPOTENCY_CONFLICT`。显式空值的可选参数与省略这些参数具有相同工具含义。

工具响应错误位于 `response.error`，包含 `code`、`message`、`retryable`。控制面或执行前拒绝以非成功 HTTP 状态返回；`ToolServiceError` 响应为 `detail`、`code`、`retryable`。

| 情形 | 错误码 | 处理方式 |
|---|---|---|
| 未开放的工具 | `TOOL_DENIED` | 已入账；停止调用该能力 |
| 工具参数不合法 | `ARGUMENTS_INVALID` | 按契约修正；不要用原键提交不同参数 |
| 动作不属于当前任务 | `ACTION_NOT_ALLOWED` | 读取允许动作并重新判断 |
| 引用不属于任务 | `EVIDENCE_NOT_FOUND` | 检查真实证据身份，不补造引用 |
| 来源缺失／完整性失败 | `SOURCE_UNAVAILABLE`、`SOURCE_INTEGRITY` | 工具调用记为失败并关闭会话；不自动重试 |
| 来源数据库暂时不可用 | `STORE_BUSY` | 已入账且 `retryable=true`；原键只返回原失败，实际重试需新键并继续占用预算 |
| 会话有调用正在执行 | `SESSION_BUSY` | 执行前拒绝且 `retryable=true`；用原键等待重试 |
| 时间／存储上限 | `TIME_BUDGET_EXHAUSTED`、`RESPONSE_TOO_LARGE` | 丢弃输出，保留错误；不能继续消耗该会话 |
| 执行中断恢复 | `CALL_INTERRUPTED` | 保留中断与预算；不重新执行 |
| 总预算用尽／会话已关闭 | `BUDGET_EXHAUSTED`、`SESSION_BLOCKED` | 执行前拒绝，不增加调用条目 |
| 键重复但请求不同 | `IDEMPOTENCY_CONFLICT` | 检查请求身份，不把冲突当成工具失败重试 |
| 账本不一致 | `LEDGER_INTEGRITY` | 停止并检查存储；不返回不可信历史记录 |
| 会话身份不存在 | `SESSION_NOT_FOUND` | 检查会话 ID |
| 未分类工具异常 | `INTERNAL_ERROR` | 保留失败；不标记为可自动重试 |

来源完整性失败、来源缺失和输出空间不足产生 `blocked` 会话。调用／错误／时间预算用尽产生 `exhausted`；正在执行或尚待恢复的记录显示 `running`。历史重放仍可读取已经保存的调用。

## HTTP 形状拒绝与入账后失败

HTTP 请求先经过请求体边界和严格 Pydantic 模型。额外字段、错误类型或长度越界通常直接返回 422；例如在 `arguments` 中塞入 `metrics`，或要求写入 `actor=human`，不会进入工具服务，也不会产生调用账本条目。这类通用 HTTP 校验错误可能只有 `detail`，不应假设所有 422 都具有工具错误码。

通过 HTTP 形状校验后，服务仍校验非空键、有限数值、有效 UTF-8、有界 JSON 等。执行前无法安全记录的请求会直接拒绝。合法形状但未知工具名会入账后返回 `TOOL_DENIED`；存在的工具携带不符合其语义的参数，会入账后返回相应错误。

Python 服务调用也经过服务端校验。可安全序列化但多余的参数能够先入账再被工具拒绝；因此直接 Python 测试和 HTTP 测试的拒绝位置可能不同。两者都不允许把模型给出的数字作为计算结果，但不能据此声称每一次无效网络请求都已保存为实验尝试。

## 后续模型提供商适配

接入实际模型时仍需增加以下适配，而不是直接把整个工作台 API 暴露给模型：

1. 由应用分配会话，并只注册上述四个工具。工具描述和模型参数解析须保持同一闭合契约，禁止自动创建新会话续费。
2. 保存模型、提示词、原始响应、工具选择、请求身份和每次模型调用的关联记录。模型文本继续与确定性工具产物分开标记，论文内容和工具文本不能成为越权指令。
3. 增加模型请求超时、供应商错误分类、取消／恢复处理，以及模型 token、费用和调用次数预算。当前时间／调用预算仅覆盖这一层工具会话，不包含模型或网络开销。
4. 编写有界调度规则：重放与真正重试使用不同策略，科学阻断不循环重试，缺证据和需人工决定时明确停止。若未来要强制终止长读取，需要独立进程等执行隔离方案。
5. 在固定材料上开展真实模型对照，并保留尚待人工确认的语义标签。当前脚本对照证明工程行为，不证明 LLM 效果或人工提效。

允许生成新假设或自动开展策略实验还需要更广的证据／候选契约、对应执行适配和研究系列级最终测试控制。v0.17 的提案接口没有授予这些能力。

相关说明：[研究任务契约](v017_research_contract.md)、[固定评测](v017_evaluation.md)。实现与边界测试分别位于 `paper_alpha/server/research_tools.py`、`paper_alpha/server/research_tools_schema.py` 和 `tests/test_research_tools.py`。
