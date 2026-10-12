import katex from 'katex';
import {isBoundedDerivedLatex} from './formulaPresentation';
const MATHML_NAMESPACE = 'http://www.w3.org/1998/Math/MathML';
const SAFE_TAGS = new Set(['math','semantics','annotation','mrow','mi','mn','mo','mfrac','msup','msub','msubsup','munder','mover','munderover','mstyle','mspace','mtable','mtr','mtd','mtext','mpadded','mphantom','menclose','msqrt','mroot']);
const SAFE_ATTRIBUTES = new Set(['xmlns','display','mathvariant','encoding','stretchy','symmetric','fence','separator','lspace','rspace','rowspacing','columnspacing','columnalign','rowalign','linethickness','width','height','depth','accent','accentunder','form','largeop','movablelimits','minsize','maxsize','scriptlevel','displaystyle']);
export function renderTrustedMath(latex: string, target: HTMLElement): void {
  if (!isBoundedDerivedLatex(latex)) throw Error('UNSUPPORTED_DERIVED_LATEX');
  const detached = document.createElement('span');
  katex.render(latex, detached, {output:'mathml',displayMode:true,throwOnError:true,trust:false,strict:'error',maxExpand:100,maxSize:10,macros:{}});
  for (const element of detached.querySelectorAll('*')) {
    if (element.namespaceURI === MATHML_NAMESPACE) {
      if (!SAFE_TAGS.has(element.localName) || [...element.attributes].some(attribute => !SAFE_ATTRIBUTES.has(attribute.name))) throw Error('UNSAFE_MATHML_OUTPUT');
    } else if (element.namespaceURI !== 'http://www.w3.org/1999/xhtml' || element.localName !== 'span' || [...element.attributes].some(attribute => attribute.name !== 'class') || element.getAttribute('class') !== 'katex') throw Error('UNSAFE_MATHML_OUTPUT');
  }
  if (detached.querySelectorAll('math').length !== 1) throw Error('MATHML_UNAVAILABLE');
  target.replaceChildren(...detached.childNodes);
}
