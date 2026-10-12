"""Optional authoring utility (reportlab); the demo uses the committed PDF."""
from pathlib import Path
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT


def build():
    root = Path(__file__).parent
    text = (root / 'teaching_note.md').read_text()
    pages = text.split('## Page 1: formula and source meaning\n')[1].split('## Page 2: explicit engineering protocol\n')
    styles = getSampleStyleSheet()
    styles['BodyText'].fontSize = 11; styles['BodyText'].leading = 17; styles['BodyText'].spaceAfter = 12
    styles['Title'].fontSize = 23; styles['Title'].leading = 28; styles['Title'].alignment = TA_LEFT
    styles['Title'].textColor = styles['Heading2'].textColor = HexColor('#123C51')
    story = []
    for i, page in enumerate(pages):
        if i:
            story.append(PageBreak())
        story.extend([Paragraph('A public teaching signal', styles['Title']),
            Paragraph('Close minus open | original project note | version 1', styles['BodyText']), Spacer(1, 14),
            Paragraph('Formula and source meaning' if i == 0 else 'Explicit engineering protocol', styles['Heading2'])])
        if i == 0:
            story.append(Paragraph('Original project teaching note. No external paper text or real market observations. This is a traceable software exercise, not an investment strategy.', styles['BodyText']))
        for para in page.strip().split('\n\n'):
            story.append(Paragraph(' '.join(para.splitlines()), styles['BodyText']))
    def footer(canvas, doc):
        canvas.setFillColor(HexColor('#536875')); canvas.setFont('Helvetica', 9)
        canvas.drawString(48, 35, 'Synthetic teaching fixture | science unknown | human review pending')
        canvas.drawRightString(547, 35, str(doc.page))
    SimpleDocTemplate(str(root / 'teaching_note.pdf'), pagesize=(595, 842), rightMargin=48,
        leftMargin=48, topMargin=48, bottomMargin=58, invariant=True,
        title='A public teaching signal: close minus open', author='Paper-to-Alpha project').build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == '__main__':
    build()
