import { describe,expect,it } from "vitest";
import { finishProtocolSave,projectProtocol,protocolConfigError,protocolMonthError,protocolNumber,protocolRecoveryKey,protocolViewKey,readProtocolSave,stageProtocolSave } from "./researchProtocols";
function storage(){const rows=new Map<string,string>();return {getItem:(key:string)=>rows.get(key)??null,setItem:(key:string,value:string)=>{rows.set(key,value);},removeItem:(key:string)=>{rows.delete(key);}};}
describe("research protocol request boundaries",()=>{
  it("keeps paper unknowns, zero returns and missing outputs distinct",()=>{
    const config=projectProtocol();expect(protocolConfigError(config)).toBe("");
    expect(protocolConfigError({...config,mode:"paper"})).toContain("不可改写");
    expect(protocolConfigError({...config,mode:"paper",id_window_months:null,id_skip_months:null,missing_policy:"unresolved",min_coverage:null,max_missing_run:null})).toBe("");
    expect(protocolNumber(null)).toBe("未输出");expect(protocolNumber(0)).toBe("0");expect(protocolNumber(0,true)).toBe("0.00%");
    expect(protocolConfigError({...config,missing_policy:"available",min_coverage:NaN})).not.toBe("");
    expect(protocolConfigError({...config,mom_window_months:Infinity})).not.toBe("");
  });
  it("binds previews to canonical configuration and target month, including zero skip",()=>{
    const config=projectProtocol();expect(protocolViewKey(config,"2025-01")).toBe(protocolViewKey({...config},"2025-01"));
    expect(protocolViewKey(config,"2025-01")).not.toBe(protocolViewKey({...config,mom_skip_months:0},"2025-01"));
    expect(protocolViewKey(config,"2025-01")).not.toBe(protocolViewKey(config,"2025-02"));
    expect(protocolMonthError("1905-01")).toBe("");expect(protocolMonthError("2099-12")).toBe("");
    for(const month of ["1904-12","2100-01","2025-00","2025-13","2025-1"])expect(protocolMonthError(month)).not.toBe("");
  });
  it("replays a frozen body without adding unsupported idempotency fields and preserves other workspaces",()=>{
    const saved=storage(),key=protocolRecoveryKey("A"),other=protocolRecoveryKey("B");
    const body={title:"Rule",note:"Manual parameters",parent_id:null,config:projectProtocol()};
    const original=stageProtocolSave(saved,key,body);body.config.mom_window_months=3;
    expect(readProtocolSave(saved,key)?.body.config.mom_window_months).toBe(11);
    expect(readProtocolSave(saved,key)?.body).not.toHaveProperty("idempotency_key");
    stageProtocolSave(saved,other,body);finishProtocolSave(saved,key,original);
    expect(readProtocolSave(saved,key)).toBeNull();expect(readProtocolSave(saved,other)?.body.config.mom_window_months).toBe(3);
  });
  it("refuses malformed, foreign or replaced request records and fails before unsafe dispatch",()=>{
    const saved=storage(),key=protocolRecoveryKey("A"),body={title:"Rule",note:"",parent_id:null,config:projectProtocol()};
    const original=stageProtocolSave(saved,key,body);
    expect(()=>stageProtocolSave(saved,key,body)).toThrow("待确认");
    saved.setItem(key,JSON.stringify({...original,body:{...body,title:"newer"}}));
    expect(()=>finishProtocolSave(saved,key,original)).toThrow("另一项");
    saved.setItem(key,JSON.stringify({...original,scope:"foreign"}));expect(()=>readProtocolSave(saved,key)).toThrow("作用域");
    saved.setItem(key,"broken");expect(()=>readProtocolSave(saved,key)).toThrow();expect(saved.getItem(key)).toBe("broken");
    expect(()=>stageProtocolSave({getItem:()=>null,setItem:()=>{}},key,body)).toThrow("确认本机保存");
  });
});
