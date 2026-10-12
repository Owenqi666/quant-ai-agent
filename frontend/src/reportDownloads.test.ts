import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReportSummary } from "./generated/api-contract";
const locale = vi.hoisted(() => ({ value: "zh" as "zh" | "en" }));
vi.mock("./i18n", () => ({ useI18n: () => ({ locale: locale.value, t: (zh: string, en: string) => locale.value === "zh" ? zh : en }) }));
import { ReportDownloads } from "./components/ResearchInsights";
const report: ReportSummary = { id: "report-one", research_id: "research", created_at: "2026-10-09T00:00:00Z",
  payload_digest: "a".repeat(64), selected_run_ids: ["run"], counts: {}, integrity: "verified" };

describe("frozen report download choices", () => {
  it("offers explicit LaTeX Markdown in the current locale and retains unmodified full exports", () => {
    for (const value of ["zh", "en"] as const) {
      locale.value = value;
      const output = renderToStaticMarkup(createElement(ReportDownloads, { report }));
      expect(output).toContain("format=markdown&amp;view=readable&amp;locale=" + value + "&amp;renderer_version=2");
      expect(output).toContain('href="/api/research-reports/report-one/export?format=json"');
      expect(output).toContain('href="/api/research-reports/report-one/export?format=markdown"');
      expect(output.indexOf("view=readable")).toBeLessThan(output.indexOf("?format=json"));
      expect(output).toContain(value === "zh" ? "阅读版报告" : "Reading report");
    }
  });
});
