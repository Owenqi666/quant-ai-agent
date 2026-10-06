import {
  activeStatuses,
  canReview,
  describeError,
  formatDate,
  formatNumber,
  shortId,
} from "../domain";
import type { Artifact, Dataset, JsonObject, Run } from "../domain";
import { Badge, JsonDetails } from "./ui";
import type { useRunEvents } from "../useRunEvents";

export default function RunDetail({
  run,
  eventFeed,
  artifacts,
  busy,
  onAction,
  onReview,
  dataset,
}: {
  run: Run;
  eventFeed: ReturnType<typeof useRunEvents>;
  artifacts: Artifact[];
  busy: string;
  onAction: (a: "retry" | "cancel") => void;
  onReview: () => void;
  dataset?: Dataset;
}) {
  const candidates = run.state?.candidates || [];
  const eventState = eventFeed.state;
  const history = eventState.history;
  const events = history?.items ?? eventState.items;
  return (
    <div className="run-detail">
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              实验结果 <Badge status={run.status} />
            </h2>
          </div>
          <div className="button-row">
            {activeStatuses.has(run.status) && (
              <button
                className="button small danger"
                disabled={!!busy || run.status === "cancelling"}
                onClick={() => onAction("cancel")}
              >
                取消实验
              </button>
            )}
            {["failed", "interrupted"].includes(run.status) && (
              <button
                className="button small"
                disabled={!!busy}
                onClick={() => onAction("retry")}
              >
                重试实验
              </button>
            )}
            {canReview(run) && (
              <button className="button primary small" onClick={onReview}>
                审核此结果
              </button>
            )}
          </div>
        </div>
        <div className="run-meta">
          <span>
            工具调用 <strong>{run.state?.tool_calls ?? "—"}</strong>
          </span>
          <span>
            引擎耗时{" "}
            <strong>{formatNumber(run.state?.elapsed_seconds, 2)} 秒</strong>
          </span>
          <span>
            制品完整性校验{" "}
            <strong className={canReview(run) ? "text-green" : ""}>
              {canReview(run) ? "已通过" : "未确认"}
            </strong>
          </span>
          <span>
            尝试 <strong>{run.attempt_count}</strong>
          </span>
        </div>
        {!!run.error && (
          <div className="info-note warning">{describeError(run.error)}</div>
        )}
        {activeStatuses.has(run.status) && (
          <div className="progress-line">
            <span />
            <p>状态自动更新。离开或刷新页面不会删除任务。</p>
          </div>
        )}
        <div className="results-grid">
          {candidates.map((c) => (
            <article className="result-card" key={c.id}>
              <div className="candidate-top">
                <h3>{c.id}</h3>
                <Badge status={c.status} />
              </div>
              <code className="expression">
                {c.result && "expression" in c.result
                  ? String(c.result.expression)
                  : c.expression}
              </code>
              {c.result?.metrics ? (
                <>
                  <div className="metric-grid">
                    <div>
                      <span>Mean rank IC</span>
                      <strong>
                        {formatNumber(c.result.metrics.mean_rank_ic)}
                      </strong>
                    </div>
                    <div>
                      <span>Mean gross return</span>
                      <strong>
                        {formatNumber(c.result.metrics.mean_gross_return, 6)}
                      </strong>
                    </div>
                    <div>
                      <span>有效天数</span>
                      <strong>{c.result.metrics.evaluated_days ?? "—"}</strong>
                    </div>
                    <div>
                      <span>因子覆盖率</span>
                      <strong>
                        {typeof c.result.metrics.factor_coverage === "number"
                          ? `${(c.result.metrics.factor_coverage * 100).toFixed(1)}%`
                          : "—"}
                      </strong>
                    </div>
                  </div>
                  <p className="fine-print">
                    验证区间 · {dataset?.data_kind === "synthetic" ? "合成数据" : dataset?.data_kind || "类型待确认"} · {dataset?.version || "数据版本待确认"} · 毛收益未扣费用
                  </p>
                </>
              ) : (
                <p className="failure-reason">
                  {c.reason ||
                    c.result?.reason ||
                    describeError(c.error) ||
                    "尚无可评估结果。"}
                </p>
              )}
              {c.attempts && (
                <JsonDetails
                  value={c.attempts}
                  label={`执行尝试与修复记录（${c.attempts.length}）`}
                />
              )}
            </article>
          ))}
        </div>
        {!candidates.length && !activeStatuses.has(run.status) && (
          <p className="muted">
            本次尝试尚未产生候选结果，请检查事件和错误记录。
          </p>
        )}
      </section>
      <div className="detail-grid">
        <section className="panel">
          <div className="panel-heading">
            <h2>事件时间线</h2>
            <span className="count-pill">显示 {events.length} 条</span>
          </div>
          <p className="muted" role="status" aria-live="polite">
            {eventState.error
              ? "事件同步失败；已接收记录与游标仍保留。"
              : eventState.syncing || !eventState.caughtUp
                ? "正在补齐持久化事件…"
                : `已同步至事件快照 ${eventState.highWatermark}，该快照共 ${eventState.totalRecords} 条。`}
          </p>
          {eventState.error && (
            <div className="info-note warning">
              <p>{eventState.error}</p>
              <div className="button-row">
                <button className="button small" onClick={eventFeed.retry}>重试事件同步</button>
                <button className="button small" onClick={eventFeed.reset}>从头重新同步</button>
              </div>
            </div>
          )}
          {!history && (
            <>
              <p className="fine-print">
                已接收 {eventState.receivedRecords} / {eventState.totalRecords} 条；当前缓存 {eventState.items.length} 条。
                {eventState.receivedRecords > eventState.items.length &&
                  " 较早记录已移出浏览器缓存，仍保存在服务端；请加载历史查看，当前列表不是完整日志。"}
              </p>
              <button className="button small" disabled={!eventState.totalRecords} onClick={eventFeed.openHistory}>
                从第一条加载历史
              </button>
            </>
          )}
          {history && (
            <div className="info-note">
              <p>
                历史快照 {history.through} · 显示第 {history.items.length ? history.loadedThrough - history.items.length + 1 : 0}–{history.loadedThrough} 条，共 {history.totalRecords} 条。
                每页最多 200 条；翻页只替换当前历史页，实时同步仍继续。
              </p>
              {history.loading && <p>正在读取历史页…</p>}
              {history.error && <p>{history.error}</p>}
              <div className="button-row">
                {history.error
                  ? <button className="button small" disabled={history.loading} onClick={eventFeed.retryHistory}>重试历史页</button>
                  : <button className="button small" disabled={history.loading || !history.hasMore} onClick={eventFeed.nextHistory}>下一页历史</button>}
                <button className="button small" disabled={history.loading} onClick={eventFeed.openHistory}>重新从第一条查看</button>
                <button className="button small" onClick={eventFeed.closeHistory}>返回实时事件</button>
              </div>
            </div>
          )}
          <ol className="timeline">
            {events.map((event, index) => (
              <li key={String(event.id ?? event.sequence ?? index)}>
                <span className="timeline-dot" />
                <div>
                  <span className="event-kind">
                    {String(
                      event.kind === "engine_event" &&
                        event.payload &&
                        typeof event.payload === "object" &&
                        "event" in event.payload
                        ? (event.payload.event as JsonObject).kind
                        : (event.kind ??
                            event.type ??
                            event.event_type ??
                            "event"),
                    )}
                  </span>
                  <time>{formatDate(String(event.created_at ?? ""))}</time>
                  <small>尝试 {shortId(event.attempt_id ?? undefined)} · 事件 {event.sequence}</small>
                  <JsonDetails value={event} label="事件详情" />
                </div>
              </li>
            ))}
          </ol>
          {!events.length && <p className="muted">{history ? "该历史页暂无事件。" : "尚未收到持久化事件。"}</p>}
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>报告与实验产物</h2>
          </div>
          {artifacts.some((a) => a.name.endsWith("report.md")) && (
            <a
              className="button wide"
              href={`/api/runs/${run.id}/report`}
              download
            >
              下载研究报告
            </a>
          )}
          <p className="fine-print">
            下载时重新核验文件摘要。下载为路径脱敏视图；完整原始快照保留在本地工作空间。
          </p>
          <div className="artifact-list">
            {artifacts.map((a) => (
              <a
                key={a.id}
                href={`/api/runs/${run.id}/artifacts/${a.id}`}
                download
              >
                <span>
                  {a.name}
                  <small>
                    尝试 {shortId(a.attempt_id)} · {(a.size / 1024).toFixed(1)}{" "}
                    KB · SHA {shortId(a.sha256)}
                  </small>
                </span>
              </a>
            ))}
          </div>
          {!artifacts.length && (
            <p className="muted">本次运行暂无已登记产物。</p>
          )}
          <JsonDetails value={run.verification} label="查看验证结果" />
          <JsonDetails value={run.attempts} label="查看历史运行尝试" />
        </section>
      </div>
    </div>
  );
}
