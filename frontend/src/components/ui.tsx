import { useI18n } from "../i18n";
import { cloneElement, isValidElement, useId } from "react";
import type { ReactElement, ReactNode } from "react";
import { activeStatuses, statusName } from "../domain";

export function Badge({ status }: { status: string }) {
  useI18n();
  return (
    <span className={`badge status-${status}`}>
      {activeStatuses.has(status) && <i />}
      {statusName(status)}
    </span>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  );
}
export function JsonDetails({
  value,
  label,
}: {
  value: unknown;
  label?: string;
}) {
  const {t} = useI18n();
  return (
    <details className="json-details">
      <summary>{label ?? t("查看完整记录", "View complete record")}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {isValidElement(children)
        ? cloneElement(
            children as ReactElement<{
              id?: string;
              "aria-describedby"?: string;
            }>,
            { id, "aria-describedby": hint ? `${id}-hint` : undefined },
          )
        : children}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
