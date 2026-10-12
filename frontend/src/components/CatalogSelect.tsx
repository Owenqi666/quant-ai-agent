import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { describeError, shortId } from "../domain";
import { catalogPath } from "../feedback";
import type { CatalogPage, CatalogResource } from "../feedback";
import { Field } from "./ui";

/** Bounded, snapshot-paged references; large histories never require copying UUIDs. */
export default function CatalogSelect<T extends { id: string }>({ label, resource, researchId, value, onChange, describe, seed = [], filter, refreshKey = "" }: {
  label: string; resource: CatalogResource; researchId: string; value: string; onChange: (id: string, item?: T) => void;
  describe: (item: T) => string; seed?: T[]; filter?: (item: T) => boolean; refreshKey?: string;
}) {
  const [rows, setRows] = useState<T[]>([]);
  const [page, setPage] = useState<CatalogPage<T> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const epoch = useRef(0);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const generation = ++epoch.current;
    const abort = new AbortController();
    setRows([]); setPage(null); setError(""); setLoading(true);
    void api<CatalogPage<T>>(catalogPath(resource, { research_id: researchId }), { signal: abort.signal }).then((next) => {
      if (generation === epoch.current && !abort.signal.aborted) { setRows(next.items); setPage(next); }
    }).catch((e) => { if (!abort.signal.aborted) setError(describeError(e)); }).finally(() => { if (generation === epoch.current) setLoading(false); });
    return () => { abort.abort(); epoch.current += 1; };
  }, [researchId, resource, refreshKey, reload]);
  const all = [...new Map([...rows, ...seed].map((row) => [row.id, row])).values()];
  const options = all.filter((row) => row.id === value || !filter || filter(row));
  async function more() {
    if (!page?.has_more || loading) return;
    const generation = epoch.current;
    setLoading(true); setError("");
    try {
      const next = await api<CatalogPage<T>>(catalogPath(resource, { research_id: researchId }, page.next_cursor, page.high_watermark));
      if (generation !== epoch.current) return;
      setRows((old) => [...old, ...next.items]); setPage(next);
    } catch (e) { if (generation === epoch.current) setError(describeError(e)); }
    finally { if (generation === epoch.current) setLoading(false); }
  }
  return <div className="reference-select">
    <Field label={label}><select value={value} onChange={(e) => onChange(e.target.value, all.find((row) => row.id === e.target.value))}>
      <option value="">请选择（不自动关联）</option>
      {value && !options.some((row) => row.id === value) && <option value={value}>当前关联 {shortId(value)}（尚未加载或属于其他研究，请核对）</option>}
      {options.map((row) => <option key={row.id} value={row.id}>{describe(row)} · {shortId(row.id)}</option>)}
    </select></Field>
    <div className="button-row"><button type="button" className="button small" disabled={loading} onClick={() => setReload((n) => n + 1)}>刷新{label}选项</button>{page?.has_more && <button type="button" className="button small" disabled={loading} onClick={() => void more()}>加载更多{label}</button>}</div>
    {error && <p className="info-note warning" role="alert">{error}</p>}
  </div>;
}
