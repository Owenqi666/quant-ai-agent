# Third-party material and reused components

This source publication does not include the author's private research workspace, third-party paper PDFs, or extracted full paper text. A locally installable personal research bundle does not establish public redistribution rights.

No project-wide open-source license has been selected. These notices preserve component provenance and third-party terms; they do not grant a license to unrelated project content.

## Alpha101 paper

The optional development case refers to Zura Kakushadze, *101 Formulaic Alphas*, [arXiv:1601.00991, version 3](https://arxiv.org/abs/1601.00991v3). Obtain the original from its source subject to its terms. `scripts/prepare_alpha101.py` verifies the fixed PDF locally before extracting material for the supported formula templates.

The paper and its full extracted text are excluded from Git. The public tutorial is an original synthetic software example, not a copy of that paper or its empirical dataset. Local factor results do not establish replication of the paper or equivalence with WorldQuant BRAIN.

WorldQuant competition participation is part of the author's personal research context. This project does not claim employment, endorsement, affiliation, or partnership with WorldQuant.

## Personal operator and factor code reuse

`paper_alpha/vendor/operators.py` and `paper_alpha/vendor/factors.py` were adapted from the author's earlier personal factor project. The source snapshots and adapted-file checksums are recorded in `paper_alpha/vendor/PROVENANCE.json` with public source identity. Original local files were not overwritten.

Adaptations removed the import-time random seed and script-only download/demo entry points; the factor module also uses a package-relative operator import. This code reuse is not a newly implemented full backtest engine. The workbench adds expression contracts, alignment checks, numerical diagnostics, evaluation, execution state, evidence/review links, and delivery/recovery mechanisms around those components.

## KaTeX

The frontend depends on KaTeX 0.19.0 for mathematical presentation. KaTeX is licensed under the MIT License. Its license follows, reproduced from the installed package:

```text
The MIT License (MIT)

Copyright (c) 2013-2020 Khan Academy and other contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Other Python and npm dependencies retain their respective licenses in their upstream distributions. Dependency locks identify the versions used; they are not a replacement for the licenses accompanying those packages.

## Project-generated illustration

`frontend/public/art/research-signal.webp` was created with an image generation tool for this project. It is a decorative illustration of papers becoming a ribbon; it depicts no actual research evidence, market data, or investment result. No third-party paper pages are embedded in it.
