import {useI18n} from '../i18n';

export function ResearchMark(){return <svg className="research-mark" aria-hidden="true" viewBox="0 0 40 40" fill="none"><path d="M9 5h16l7 7v21H9zM25 5v8h7"/><path d="M5 12v25h20"/><path d="M15 25c1-8 8-8 8-2s-8 9-8 2Zm8-7v8c0 3 3 3 4 1"/></svg>}

export default function ResearchArt(){
 const {t}=useI18n();
 return <section className="research-art" aria-label={t('论文与信号：项目主题插画','Paper and signal: project illustration')}>
  <div className="research-art-copy"><p>{t('从论文中的一个想法开始，让证据、公式与实验彼此相连。','Start with an idea in a paper. Keep its evidence, formula and experiment connected.')}</p><div className="art-trace" aria-hidden="true"><span>{t('原文','Paper')}</span><i/><span>{t('证据','Evidence')}</span><i/><span>Alpha</span></div></div>
  <div className="research-art-image"><img src="/art/research-signal.webp" alt="" width="1536" height="1024"/><span className="art-caption">{t('手稿与信号 · 概念插画','Manuscript & signal · Concept illustration')}</span></div>
 </section>;
}
