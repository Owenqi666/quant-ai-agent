import {describe, expect, it} from "vitest";
import {authorNumber, authorPanelFileError, authorState, MAX_AUTHOR_PANEL_BYTES, parseAuthorPanel} from "./authorPanels";
const input = () => ({schema_version: 1, kind: "author_perturbed_monthly_panel", adapter_version: "gjs-v2-monthly-panel-v1", source: {}, selection: {}, months: Array.from({length: 13}, (_, i) => `month-${i}`), rows: [{returns: [0, null, 24]}]});
describe("author panel input and output boundaries", () => {
  it("rejects MAT, empty and oversized files before a browser reads their contents", () => {
    expect(authorPanelFileError(null)).toContain("请选择");
    expect(authorPanelFileError({name: "IntnlData.mat", size: 358892265})).toContain("MAT");
    expect(authorPanelFileError({name: "input.json", size: 0})).toContain("非空");
    expect(authorPanelFileError({name: "input.json", size: MAX_AUTHOR_PANEL_BYTES + 1})).toContain("768 KiB");
    expect(authorPanelFileError({name: "input.json", size: MAX_AUTHOR_PANEL_BYTES})).toBe("");
  });
  it("keeps zero, source outliers, null and unknown properties unchanged for server validation", () => {
    const value = {...input(), future_server_rejection: "preserved"};
    expect(parseAuthorPanel(JSON.stringify(value))).toEqual(value);
    expect(authorNumber(0)).toBe("0"); expect(authorNumber(null)).toBe("不可用");
    expect(authorNumber(24)).toBe("24"); expect(authorState("nan")).toContain("缺失");
  });
  it("refuses wrong artifact, row/month bounds and nonfinite numbers before JSON can silently turn them to null", () => {
    for (const value of [null, [], {report: input()}, {...input(), rows: []}, {...input(), rows: Array(513).fill({})}, {...input(), months: []}]) {
      expect(() => parseAuthorPanel(JSON.stringify(value))).toThrow("作者月度面板");
    }
    expect(() => parseAuthorPanel("{broken}")).toThrow("无法读取");
    expect(() => parseAuthorPanel(JSON.stringify(input()).replace('[0,null,24]', '[1e999,null,24]'))).toThrow("无法表示的数值");
    expect(() => parseAuthorPanel(" ".repeat(MAX_AUTHOR_PANEL_BYTES + 1))).toThrow("768 KiB");
  });
});
