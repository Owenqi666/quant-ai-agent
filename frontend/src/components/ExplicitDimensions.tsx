import {assessmentDimensions} from "../researchAssessment";
import type {Dimension} from "../researchAssessment";
import type {SemanticDraft} from "../semanticAnnotationDraft";
import {Field} from "./ui";

export default function ExplicitDimensions({value,onChange,prefix}:{value:SemanticDraft["dimensions"];onChange:(next:SemanticDraft["dimensions"])=>void;prefix:string}){
  return <>{Object.entries(assessmentDimensions).map(([name,label])=>{const key=name as Dimension,v=value[key];return <div key={key}>
    <Field label={`${prefix}${label}判断`}><select value={v.outcome} onChange={e=>onChange({...value,[key]:{...v,outcome:e.target.value}})}>
      <option value="">请明确选择</option><option value="passed">通过</option><option value="failed">未通过</option><option value="not_assessed">尚未评定</option><option value="not_applicable">不适用</option>
    </select></Field>
    <Field label={`${prefix}${label}理由`}><textarea value={v.reason} maxLength={2000} rows={2} onChange={e=>onChange({...value,[key]:{...v,reason:e.target.value}})}/></Field>
  </div>;})}</>;
}
