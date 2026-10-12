import {afterEach,describe,expect,it,vi} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ProviderTasks from './components/ProviderTasks';
import ProviderRuntimeStatusView from './components/ProviderRuntimeStatusView';
import {LanguageProvider} from './i18n';
import {WorkspaceRequestScope} from './workspaceScope';
import {observedMetric,proposalStatusLabel} from './providerWorkflow';
afterEach(()=>vi.unstubAllGlobals());
function locale(value:'zh'|'en'){vi.stubGlobal('localStorage',{getItem:()=>value,setItem:()=>{}});}
function entry(){const scope=new WorkspaceRequestScope();return renderToStaticMarkup(createElement(LanguageProvider,null,createElement(ProviderTasks,{workspaceScope:scope,catalog:[],researchId:'',research:null,onResearchChange:()=>{},onRefreshResearch:()=>{},onOpenJob:()=>{},onPreviousResearch:()=>{},onNextResearch:()=>{},canPreviousResearch:false,canNextResearch:false})));}
describe('critical provider reading copy keeps locale without altering machine records',()=>{
 it('shows the English entry and runtime loading status in English',()=>{locale('en');const output=entry();expect(output).toContain('AI draft generation');expect(output).toContain('Submit one draft generation');expect(output).toContain('without human approval');expect(output).toContain('Loading generation executor status');expect(output).not.toMatch(/[\u4e00-\u9fff]/);});
 it('retains Chinese entry and source status labels',()=>{locale('zh');expect(entry()).toContain('提交一次草案生成');expect(proposalStatusLabel('ready')).toBe('草案可交接实验');expect(observedMetric(null)).toBe('缺失');});
 it('translates status/absent metrics while retaining exact values and unknown codes',()=>{locale('en');expect(proposalStatusLabel('ready')).toBe('Draft ready for experiment handoff');expect(proposalStatusLabel('unknown_future_status')).toBe('unknown_future_status');expect(observedMetric(null)).toBe('Missing');expect(observedMetric(0)).toBe('0');expect(observedMetric(-.123456789)).toBe('-0.123456789');});
 it('retains all host permission and model-unknown limitations in Chinese and English',()=>{for(const value of ['zh','en'] as const){locale(value);const result=entry();expect(result).toContain(value==='en'?'The browser cannot configure models':'浏览器不能设置模型');expect(result).toContain(value==='en'?'Refreshing does not reset the budget':'刷新不重置预算');expect(renderToStaticMarkup(createElement(LanguageProvider,null,createElement(ProviderRuntimeStatusView,{workspaceScope:new WorkspaceRequestScope(),refresh:0})))).toContain(value==='en'?'Loading generation executor status':'正在读取生成执行器状态');}});
});
