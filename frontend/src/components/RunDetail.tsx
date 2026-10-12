import { useI18n } from "../i18n";
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
  const {t} = useI18n();
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
              {t("实验结果", "Experiment results")} <Badge status={run.status} />
            </h2>
          </div>
          <div className="button-row">
            {activeStatuses.has(run.status) && (
              <button
                className="button small danger"
                disabled={!!busy || run.status === "cancelling"}
                onClick={() => onAction("cancel")}
              >
                {t("取消实验", "Cancel experiment")}
              </button>
            )}
            {["failed", "interrupted"].includes(run.status) && (
              <button
                className="button small"
                disabled={!!busy}
                onClick={() => onAction("retry")}
              >
                {t("重试实验", "Retry experiment")}
              </button>
            )}
            {canReview(run) && (
              <button className="button primary small" onClick={onReview}>
                {t("审核此结果", "Review this result")}
              </button>
            )}
          </div>
        </div>
        <div className="run-meta">
          <span>
            {t("工具调用", "Tool calls")} <strong>{run.state?.tool_calls ?? "—"}</strong>
          </span>
          <span>
            {t("引擎耗时", "Engine time")}{" "}
            <strong>{formatNumber(run.state?.elapsed_seconds, 2)} {t("秒", "s")}</strong>
          </span>
          <span>
            {t("制品完整性校验", "Artifact integrity")}{" "}
            <strong className={canReview(run) ? "text-green" : ""}>
              {canReview(run) ? t("已通过", "Verified") : t("未确认", "Not confirmed")}
            </strong>
          </span>
          <span>
            {t("尝试", "Attempts")} <strong>{run.attempt_count}</strong>
          </span>
        </div>
        {!!run.error && (
          <div className="info-note warning">{describeError(run.error)}</div>
        )}
        {activeStatuses.has(run.status) && (
          <div className="progress-line">
            <span />
            <p>{t("状态自动更新。离开或刷新页面不会删除任务。", "Status updates automatically. Leaving or refreshing does not delete the task.")}</p>
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
                      <span>{t("有效天数", "Evaluated days")}</span>
                      <strong>{c.result.metrics.evaluated_days ?? "—"}</strong>
                    </div>
                    <div>
                      <span>{t("因子覆盖率", "Factor coverage")}</span>
                      <strong>
                        {typeof c.result.metrics.factor_coverage === "number"
                          ? `${(c.result.metrics.factor_coverage * 100).toFixed(1)}%`
                          : "—"}
                      </strong>
                    </div>
                  </div>
                  <p className="fine-print">
                    {t("验证区间", "Validation split")} · {dataset?.data_kind === "synthetic" ? t("合成数据", "Synthetic data") : dataset?.data_kind || t("类型待确认", "Kind not confirmed")} · {dataset?.version || t("数据版本待确认", "Data version not confirmed")} · {t("毛收益未扣费用", "Gross returns before fees")}
                  </p>
                </>
              ) : (
                <p className="failure-reason">
                  {c.reason ||
                    c.result?.reason ||
                    describeError(c.error) ||
                    t("尚无可评估结果。", "No evaluable result yet.")}
                </p>
              )}
              {c.attempts && (
                <JsonDetails
                  value={c.attempts}
                  label={t(`执行尝试与修复记录（${c.attempts.length}）`, `Execution attempts and repairs (${c.attempts.length})`)}
                />
              )}
            </article>
          ))}
        </div>
        {!candidates.length && !activeStatuses.has(run.status) && (
          <p className="muted">
            {t("本次尝试尚未产生候选结果，请检查事件和错误记录。", "This attempt has no candidate results yet. Check the events and errors.")}
          </p>
        )}
      </section>
      <div className="detail-grid">
        <section className="panel">
          <div className="panel-heading">
            <h2>{t("事件时间线", "Event timeline")}</h2>
            <span className="count-pill">{t(`显示 ${events.length} 条`, `${events.length} events shown`)}</span>
          </div>
          <p className="muted" role="status" aria-live="polite">
            {eventState.error
              ? t("事件同步失败；已接收记录与游标仍保留。", "Event synchronization failed. Received records and the cursor are retained.")
              : eventState.syncing || !eventState.caughtUp
                ? t("正在补齐持久化事件…", "Synchronizing persisted events…")
                : t(`已同步至事件快照 ${eventState.highWatermark}，该快照共 ${eventState.totalRecords} 条。`, `Synced to event snapshot ${eventState.highWatermark}, containing ${eventState.totalRecords} records.`)}
          </p>
          {eventState.error && (
            <div className="info-note warning">
              <p>{eventState.error}</p>
              <div className="button-row">
                <button className="button small" onClick={eventFeed.retry}>{t("重试事件同步", "Retry event synchronization")}</button>
                <button className="button small" onClick={eventFeed.reset}>{t("从头重新同步", "Synchronize from the start")}</button>
              </div>
            </div>
          )}
          {!history && (
            <>
              <p className="fine-print">
                {t(`已接收 ${eventState.receivedRecords} / ${eventState.totalRecords} 条；当前缓存 ${eventState.items.length} 条。`, `Received ${eventState.receivedRecords} / ${eventState.totalRecords}; ${eventState.items.length} records cached.`)}
                {eventState.receivedRecords > eventState.items.length &&
                  t(" 较早记录已移出浏览器缓存，仍保存在服务端；请加载历史查看，当前列表不是完整日志。", " Earlier records are still on the server. Load history to view them; this list is not the complete log.")}
              </p>
              <button className="button small" disabled={!eventState.totalRecords} onClick={eventFeed.openHistory}>
                {t("从第一条加载历史", "Load history from the first record")}
              </button>
            </>
          )}
          {history && (
            <div className="info-note">
              <p>
                {t(`历史快照 ${history.through} · 显示第 ${history.items.length ? history.loadedThrough - history.items.length + 1 : 0}–${history.loadedThrough} 条，共 ${history.totalRecords} 条。每页最多 200 条；翻页只替换当前历史页，实时同步仍继续。`, `Snapshot ${history.through} · Records ${history.items.length ? history.loadedThrough - history.items.length + 1 : 0}–${history.loadedThrough} of ${history.totalRecords}. Up to 200 per page; live synchronization continues.`)}
              </p>
              {history.loading && <p>{t("正在读取历史页…", "Loading history…")}</p>}
              {history.error && <p>{history.error}</p>}
              <div className="button-row">
                {history.error
                  ? <button className="button small" disabled={history.loading} onClick={eventFeed.retryHistory}>{t("重试历史页", "Retry history page")}</button>
                  : <button className="button small" disabled={history.loading || !history.hasMore} onClick={eventFeed.nextHistory}>{t("下一页历史", "Next history page")}</button>}
                <button className="button small" disabled={history.loading} onClick={eventFeed.openHistory}>{t("重新从第一条查看", "Restart from the first record")}</button>
                <button className="button small" onClick={eventFeed.closeHistory}>{t("返回实时事件", "Return to live events")}</button>
              </div>
            </div>
          )}
          <details><summary>{t("查看事件记录", "View event records")}</summary><ol className="timeline">
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
                  <small>{t("尝试", "Attempt")} {shortId(event.attempt_id ?? undefined)} · {t("事件", "Event")} {event.sequence}</small>
                  <JsonDetails value={event} label={t("事件详情", "Event details")} />
                </div>
              </li>
            ))}
          </ol></details>
          {!events.length && <p className="muted">{history ? t("该历史页暂无事件。", "No events on this page.") : t("尚未收到持久化事件。", "No persisted events received yet.")}</p>}
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>{t("报告与实验产物", "Reports and experiment artifacts")}</h2>
          </div>
          {artifacts.some((a) => a.name.endsWith("report.md")) && (
            <a
              className="button wide"
              href={`/api/runs/${run.id}/report`}
              download
            >
              {t("下载研究报告", "Download research report")}
            </a>
          )}
          <p className="fine-print">
            {t("下载时重新核验文件摘要。下载为路径脱敏视图；完整原始快照保留在本地工作空间。", "File digests are verified on download. Downloads redact local paths; original snapshots remain in the local workspace.")}
          </p>
          <details><summary>{t("查看全部实验文件", "View all experiment files")}</summary><div className="artifact-list">
            {artifacts.map((a) => (
              <a
                key={a.id}
                href={`/api/runs/${run.id}/artifacts/${a.id}`}
                download
              >
                <span>
                  {a.name}
                  <small>
                    {t("尝试", "Attempt")} {shortId(a.attempt_id)} · {(a.size / 1024).toFixed(1)}{" "}
                    KB · SHA {shortId(a.sha256)}
                  </small>
                </span>
              </a>
            ))}
          </div></details>
          {!artifacts.length && (
            <p className="muted">{t("本次运行暂无已登记产物。", "No artifacts registered for this run.")}</p>
          )}
          <JsonDetails value={run.verification} label={t("查看验证结果", "View verification")} />
          <JsonDetails value={run.attempts} label={t("查看历史运行尝试", "View historical attempts")} />
        </section>
      </div>
    </div>
  );
}
