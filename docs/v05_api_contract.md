# v0.5 HTTP 响应契约

Pydantic 响应模型是 HTTP 边界的单一事实来源。FastAPI 对实际响应校验，OpenAPI 生成 TypeScript 声明、路由表和浏览器运行时校验；不另建一份手写响应规范，不改变因子、指标、保存记录或计算语义。

## 修改与检查

```sh
.venv/bin/python scripts/export_api_contract.py
.venv/bin/python scripts/export_api_contract.py --check
.venv/bin/python -m unittest tests.test_api_contract -v
```

生成物为 `frontend/src/generated/api-contract.ts` 和 `openapi.json`。修改 API/模型后运行生成；`--check` 精确比较文件字节，发现漂移返回非零退出码，不覆盖文件。生成时使用临时工作区，不读取或修改用户的工作区。需要项目已有的 Python web 依赖，无额外代码生成依赖。

浏览器使用 `assertApiResponse('/api' + path, method, status, data)`，成功返回才读取数据。遇到非法状态、缺失字段、错误 nullable 类型、额外顶层内部字段或错误响应格式，抛出 `ApiContractError`。运行时路由表区分 GET 列表、GET 详情和 POST 创建的真实形状；去除查询字符串后匹配路径参数。

## 模型范围

- 论文：列表摘要和带页码原文的详情分开；PDF 为文件下载。
- 研究：列表摘要、完整研究（修订和预检）、单条新修订分开。证据、假设、出处类别、候选实现均有必需字段。
- 实验：提交/列表为 `RunSummary`，详情/取消/重试为 `RunDetail`；状态、模式、尝试、候选状态、数值 metrics 有类型约束。未启动的 `state`、`verification`、时间和错误为明确的 `null`，不能直接缺失。
- 数据：数据集列表与注册来源详情分开；导入收据、验证报告、验证尝试、注册尝试和历史事件分别建模，状态为有限枚举。
- 回流：审核、issue 及其事件、分页目录、反馈汇总有响应模型。分页目录项目为对应资源的已定义类型联合。
- 回归：新建检查必须返回完整 outcome、差异、具体 checks、reason_code、scope 和 timing。历史列表支持三个明确的封闭形状：早期 status-only、v0.3 科学校验、v0.4 及之后的完整校验。历史响应不补造 outcome，不改原文件或数据库。
- 事件、事件分页和实验产物目录有必需字段；下载仍使用已有的摘要/大小/路径校验。

扩展性仅保留在具体局部：评估配置、预算、数据 metadata、引擎补充诊断、独立校验详细证据、汇总指标输入。它们继续由已有科学/业务校验解释，HTTP 模型不重复实现这些规则。开放部分不能替代顶层必需字段或有限状态约束。

所有 JSON 业务成功响应均纳入契约。PDF、Markdown、产物文件和 summary 下载响应不经 JSON 校验器。生成器拒绝尚未支持的新 schema 约束，避免将新的限制悄悄生成成宽松检查；运行时实现当前发出的类型、枚举、常量、联合、必需字段和限制，不能视为通用 JSON Schema 实现。

## 错误边界与本地草稿范围

业务错误和请求校验错误保持 `{"detail": "..."}`。服务端响应不符合模型时记录错误字段位置，返回安全的 HTTP 500 `{"detail": "Internal response does not match API contract"}`，不把内部响应值、路径或完整验证对象回显给客户端。Host 拒绝属于既有入口中间件边界，文件/非 API 访问不承诺 JSON 业务响应格式。

`health.workspace_id` 是本机 resolved 工作区路径的 SHA-256，用于浏览器草稿命名空间；不输出裸路径，不是认证身份或跨机器稳定 ID。相同目录刷新保持一致，移动/复制到另一个目录成为新草稿范围。恢复草稿仍应核对论文、数据集、研究和 base revision 的存在与绑定，不能仅依赖该散列。

## 验证证据

`tests/test_api_contract.py` 覆盖：真实论文示例和修订、队列→计算→报告验证→人工审核→回归检查、有效/无效数据导入与显式注册、issue 和目录、稳定工作区 scope、核心响应缺字段/非法状态/额外内部字段/错误 null 类型注入、历史检查只读兼容、新 POST 禁止降级、错误不回显输入、生成可复现与漂移拒绝。前端消费层另外执行运行时负例和浏览器完整操作流程；整体验收结果以发布 artifacts 中实际记录为准。
