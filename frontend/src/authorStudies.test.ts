import {describe, expect, it} from "vitest";
import type {StudyDetail, StudyReview} from "./generated/api-contract";
import {MAX_AUTHOR_SCAN_BYTES, parseStudyScan, studyFileError, studyPanelIds, studyReviewBody, studyStatusName} from "./authorStudies";
const scan = () => ({schema_version: 1, kind: "author_eligibility_scan", semantics_version: "gjs-author-eligibility-v1", source: {}, plan: {}, plan_digest: "a".repeat(64), months: [{month: "1993-03", patterns: [0, 1, 0, 0, 0, 0, 0, 0]}]});
describe("author study input and review boundaries", () => {
  it("blocks wrong artifacts, oversized files and nonfinite values without rewriting an input", () => {
    expect(studyFileError(null)).toContain("请选择");
    expect(studyFileError({name: "data.mat", size: 100})).toContain("原 MAT");
    expect(studyFileError({name: "input.json", size: MAX_AUTHOR_SCAN_BYTES + 1})).toContain("256 KiB");
    expect(studyFileError({name: "input.json", size: 1})).toBe("");
    const value = {...scan(), unknown_field: "retained for server rejection"};
    expect(parseStudyScan(JSON.stringify(value))).toEqual(value);
    expect(() => parseStudyScan(JSON.stringify({...scan(), months: []}))).toThrow("资格扫描");
    expect(() => parseStudyScan(JSON.stringify({...scan(), months: Array(181).fill({})}))).toThrow("资格扫描");
    expect(() => parseStudyScan(JSON.stringify(scan()).replace('[0,1,0,0,0,0,0,0]', '[1e999,1,0,0,0,0,0,0]'))).toThrow("无法表示");
    expect(() => parseStudyScan('{"plan":{}}')).toThrow("资格扫描");
  });
  it("requires bounded unique full panel IDs while empty linkage remains optional", () => {
    const id = "author_panel_" + "a".repeat(64), next = "author_panel_" + "b".repeat(64);
    expect(studyPanelIds("")).toEqual([]); expect(studyPanelIds(` ${id},\n${next}`)).toEqual([id, next]);
    expect(() => studyPanelIds(`${id}\n${id}`)).toThrow("不重复");
    expect(() => studyPanelIds("a local MAT path")).toThrow("完整");
    expect(() => studyPanelIds(Array.from({length: 9}, (_, i) => "author_panel_" + String(i).repeat(64)).join("\n"))).toThrow("8 个");
  });
  it("binds a review to visible exact results and never promotes screened output to executable", () => {
    const detail = {digest: "original-digest", verification_scope: "aggregate_consistency_only", raw_source_reverified: false, result: {execution_ready: false}} as StudyDetail;
    const body = studyReviewBody(detail, "data_insufficient", "Observed missing histories.", "automation");
    expect(body).toEqual({study_digest: "original-digest", decision: "data_insufficient", note: "Observed missing histories.", actor: "automation"});
    expect(() => studyReviewBody(null, "accepted_with_limits", "reason", "human")).toThrow("先完整读取");
    expect(() => studyReviewBody({...detail, result: {...detail.result, execution_ready: true}} as unknown as StudyDetail, "accepted_with_limits", "reason", "human")).toThrow("先完整读取");
    expect(() => studyReviewBody(detail, "accepted_with_limits", " ", "human")).toThrow("填写");
    expect(() => studyReviewBody(detail, "accepted_with_limits", "reason", "robot" as StudyReview["actor"])).toThrow("声明来源");
    expect(() => studyReviewBody(detail, "toString" as StudyReview["decision"], "reason", "human")).toThrow("请选择");
    expect(studyStatusName("screen_passed")).toBe("筛查完成：达到声明的数量门槛");
    expect(studyStatusName("screen_blocked")).toBe("筛查完成：未达到门槛");
  });
});
