"""
report_engine.py  –  Module F-09/F-10/F-11 : Génération de Rapports
──────────────────────────────────────────────────────────────────────
100% offline — python-docx + reportlab (déjà installés).

F-09 Génération Word (.docx) :
  • Page de garde professionnelle avec couleurs de marque
  • Sections : Participants, Résumé exécutif, Transcription,
    Décisions, Tâches (tableau), Prochaines étapes
  • Templates par département : RH, Commercial, Technique, Direction

F-10 Export PDF :
  • Génération PDF directe via reportlab (sans passer par Word)
  • Mode protégé : PDF avec mot de passe
  • Compression optionnelle

F-11 Personnalisation :
  • 4 templates (RH, Commercial, Technique, Direction)
  • Sections incluses/exclues configurables
  • Niveau de détail : résumé / standard / complet
"""

import os
import datetime
from typing import Optional

# ── python-docx ──────────────────────────────────────────────────────────────
try:
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    print("[Report] ⚠ python-docx non installé → pip install python-docx")

# ── reportlab ────────────────────────────────────────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, PageBreak, HRFlowable)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("[Report] ⚠ reportlab non installé → pip install reportlab")

# ─── Paramètres dossier de sortie ────────────────────────────────────────────
REPORTS_DIR = "reports"
os.makedirs(REPORTS_DIR, exist_ok=True)

# ─── Couleurs par template ────────────────────────────────────────────────────
TEMPLATE_COLORS = {
    "RH":          {"primary": (0x1a, 0x6b, 0x3a), "secondary": (0xe8, 0xf5, 0xe9), "accent": (0x4c, 0xaf, 0x50)},
    "Commercial":  {"primary": (0x1a, 0x4a, 0x8a), "secondary": (0xe3, 0xf2, 0xfd), "accent": (0x21, 0x96, 0xf3)},
    "Technique":   {"primary": (0x37, 0x47, 0x4f), "secondary": (0xec, 0xef, 0xf1), "accent": (0x60, 0x7d, 0x8b)},
    "Direction":   {"primary": (0x4a, 0x14, 0x8c), "secondary": (0xf3, 0xe5, 0xf5), "accent": (0x9c, 0x27, 0xb0)},
}

# ─── Sections disponibles ─────────────────────────────────────────────────────
ALL_SECTIONS = [
    "page_garde", "participants", "resume_executif",
    "decisions", "taches", "transcription", "prochaines_etapes"
]

DEFAULT_SECTIONS = {
    "resume":    ["page_garde", "resume_executif", "decisions", "taches"],
    "standard":  ["page_garde", "participants", "resume_executif",
                  "decisions", "taches", "prochaines_etapes"],
    "complet":   ALL_SECTIONS,
}


class ReportConfig:
    """Configuration d'un rapport."""
    def __init__(self):
        self.template    = "Direction"      # RH / Commercial / Technique / Direction
        self.detail      = "standard"       # resume / standard / complet
        self.sections    = list(DEFAULT_SECTIONS["standard"])
        self.title       = "Compte-rendu de réunion"
        self.subtitle    = ""
        self.company     = "PolyMeet"
        self.department  = ""
        self.password    = ""               # PDF protégé si non vide
        self.compress    = False


class ReportEngine:
    """
    Moteur de génération de rapports Word et PDF.

    Usage :
        engine = ReportEngine()
        config = ReportConfig()
        config.template = "Technique"
        config.detail   = "complet"

        # Depuis une MeetingAnalysis + transcription
        docx_path = engine.generate_docx(analysis, transcript_entries, config)
        pdf_path  = engine.generate_pdf(analysis, transcript_entries, config)
    """

    def __init__(self):
        self._last_docx = ""
        self._last_pdf  = ""

    # ══════════════════════════════════════════════════════════════════════════
    # F-09 — Génération Word
    # ══════════════════════════════════════════════════════════════════════════

    def generate_docx(self, analysis, transcript_entries: list,
                      config: ReportConfig = None) -> str:
        """
        Génère un rapport Word complet.
        Retourne le chemin du fichier créé.
        """
        if not DOCX_AVAILABLE:
            print("[Report] ❌ python-docx manquant")
            return ""

        if config is None:
            config = ReportConfig()

        doc    = Document()
        colors = TEMPLATE_COLORS.get(config.template, TEMPLATE_COLORS["Direction"])
        self._setup_styles(doc, colors)

        sections = config.sections if config.sections else DEFAULT_SECTIONS[config.detail]

        # ── Sections ─────────────────────────────────────────────────────────
        if "page_garde" in sections:
            self._add_cover_page(doc, analysis, config, colors)
            doc.add_page_break()

        if "participants" in sections and analysis.participants:
            self._add_participants_section(doc, analysis, colors)

        if "resume_executif" in sections:
            self._add_executive_summary(doc, analysis, colors)

        if "decisions" in sections and analysis.decisions:
            self._add_decisions_section(doc, analysis, colors)

        if "taches" in sections:
            self._add_tasks_table(doc, analysis, colors)

        if "transcription" in sections and transcript_entries:
            self._add_transcription_section(doc, transcript_entries, colors)

        if "prochaines_etapes" in sections:
            self._add_next_steps(doc, analysis, colors)

        # ── Pied de page ──────────────────────────────────────────────────────
        self._add_footer(doc, config)

        # ── Sauvegarde ───────────────────────────────────────────────────────
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"rapport_{config.template.lower()}_{ts}.docx"
        path = os.path.join(REPORTS_DIR, name)
        doc.save(path)
        self._last_docx = path
        print(f"[Report] ✅ Word généré : {path}")
        return path

    # ── Mise en page et styles ────────────────────────────────────────────────

    def _setup_styles(self, doc, clr: dict):
        """Configure les marges et styles du document."""
        from docx.shared import Cm
        for section in doc.sections:
            section.top_margin    = Cm(2.5)
            section.bottom_margin = Cm(2.5)
            section.left_margin   = Cm(3.0)
            section.right_margin  = Cm(2.5)

    def _rgb(self, clr_tuple):
        return RGBColor(*clr_tuple)

    # ── Page de garde ─────────────────────────────────────────────────────────

    def _add_cover_page(self, doc, analysis, config: ReportConfig, clr: dict):
        # Bande de couleur supérieure (simulée avec un paragraphe coloré)
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(0)
        run = p.add_run("  " * 60)
        run.font.highlight_color = None
        # Fond coloré via shading XML
        self._shade_paragraph(p, clr["primary"])

        doc.add_paragraph()  # Espace

        # Logo / Nom entreprise
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(config.company)
        run.font.size  = Pt(28)
        run.font.bold  = True
        run.font.color.rgb = self._rgb(clr["primary"])

        # Titre principal
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(config.title)
        run.font.size  = Pt(22)
        run.font.bold  = True
        run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)

        # Sous-titre / département
        if config.subtitle or config.department:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            txt = config.subtitle or config.department
            run = p.add_run(txt)
            run.font.size  = Pt(14)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

        doc.add_paragraph()

        # Ligne de séparation colorée
        self._add_colored_line(doc, clr["primary"])

        doc.add_paragraph()

        # Infos date et participants
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"Date : {analysis.analyzed_at}")
        run.font.size = Pt(12)

        if analysis.participants:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(f"Participants : {', '.join(analysis.participants)}")
            run.font.size = Pt(12)

        # Sentiment
        emoji_map = {"positif": "😊", "neutre": "😐", "tendu": "😟"}
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(
            f"Ambiance générale : {analysis.sentiment.upper()} "
            f"{emoji_map.get(analysis.sentiment, '')}")
        run.font.size  = Pt(11)
        run.font.color.rgb = self._rgb(clr["accent"])

        doc.add_paragraph()
        self._add_colored_line(doc, clr["primary"])

        # Template / niveau
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"Template : {config.template}  |  Niveau : {config.detail}")
        run.font.size  = Pt(9)
        run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    # ── Participants ──────────────────────────────────────────────────────────

    def _add_participants_section(self, doc, analysis, clr: dict):
        self._add_section_heading(doc, "👥 Participants", clr)
        for name in analysis.participants:
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(name)
            run.font.size = Pt(11)
        doc.add_paragraph()

    # ── Résumé exécutif ───────────────────────────────────────────────────────

    def _add_executive_summary(self, doc, analysis, clr: dict):
        self._add_section_heading(doc, "📋 Résumé Exécutif", clr)

        # Box colorée pour le résumé court
        table = doc.add_table(rows=1, cols=1)
        table.style = "Table Grid"
        cell = table.cell(0, 0)
        self._shade_cell(cell, clr["secondary"])
        cell.paragraphs[0].clear()
        for line in analysis.summary_short.split("\n"):
            p = cell.add_paragraph()
            run = p.add_run(line)
            run.font.size = Pt(11)

        doc.add_paragraph()

        # Résumé complet
        if analysis.summary_full:
            self._add_section_heading(doc, "📝 Résumé Détaillé", clr, level=2)
            for para in analysis.summary_full.split("\n\n"):
                p = doc.add_paragraph()
                run = p.add_run(para.strip())
                run.font.size = Pt(11)
                p.paragraph_format.space_after = Pt(6)

        # Points clés
        if analysis.key_points:
            self._add_section_heading(doc, "🎯 Points Clés", clr, level=2)
            for point in analysis.key_points:
                p = doc.add_paragraph(style="List Bullet")
                run = p.add_run(point)
                run.font.size = Pt(11)

        doc.add_paragraph()

    # ── Décisions ─────────────────────────────────────────────────────────────

    def _add_decisions_section(self, doc, analysis, clr: dict):
        self._add_section_heading(doc, "✅ Décisions Prises", clr)
        for i, decision in enumerate(analysis.decisions, 1):
            p = doc.add_paragraph()
            run = p.add_run(f"{i}. ")
            run.font.bold  = True
            run.font.color.rgb = self._rgb(clr["primary"])
            run2 = p.add_run(decision)
            run2.font.size = Pt(11)
        doc.add_paragraph()

    # ── Tableau des tâches ────────────────────────────────────────────────────

    def _add_tasks_table(self, doc, analysis, clr: dict):
        self._add_section_heading(doc, "📌 Actions à Réaliser", clr)

        if not analysis.tasks:
            p = doc.add_paragraph()
            p.add_run("Aucune action identifiée.").font.size = Pt(11)
            doc.add_paragraph()
            return

        # En-têtes du tableau
        headers = ["#", "Tâche", "Responsable", "Délai", "Statut"]
        col_widths = [Cm(1), Cm(7.5), Cm(3.5), Cm(3.5), Cm(2.5)]

        table = doc.add_table(rows=1 + len(analysis.tasks), cols=5)
        table.style = "Table Grid"

        # Ligne d'en-tête
        hdr_row = table.rows[0]
        for i, (header, width) in enumerate(zip(headers, col_widths)):
            cell = hdr_row.cells[i]
            cell.width = width
            self._shade_cell(cell, clr["primary"])
            p = cell.paragraphs[0]
            run = p.add_run(header)
            run.font.bold  = True
            run.font.size  = Pt(10)
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Lignes de données
        for row_idx, task in enumerate(analysis.tasks, 1):
            row = table.rows[row_idx]
            # Couleur alternée
            bg = clr["secondary"] if row_idx % 2 == 0 else (0xFF, 0xFF, 0xFF)

            data = [
                str(row_idx),
                task.get("text", "")[:80],
                task.get("responsible", "—") or "—",
                task.get("deadline", "—") or "—",
                "⬜ À faire",
            ]
            for col_idx, value in enumerate(data):
                cell = row.cells[col_idx]
                self._shade_cell(cell, bg)
                p = cell.paragraphs[0]
                run = p.add_run(value)
                run.font.size = Pt(10)
                if col_idx == 0:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph()

    # ── Transcription ─────────────────────────────────────────────────────────

    def _add_transcription_section(self, doc, transcript_entries: list, clr: dict):
        self._add_section_heading(doc, "📄 Transcription Complète", clr)
        for entry in transcript_entries:
            p = doc.add_paragraph()
            # Horodatage
            ts_run = p.add_run(f"[{entry.get('time', '')}] ")
            ts_run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
            ts_run.font.size = Pt(9)
            # Speaker
            speaker = entry.get("speaker_label", "")
            if speaker and speaker != "?":
                spk_run = p.add_run(f"{speaker}: ")
                spk_run.font.bold  = True
                spk_run.font.size  = Pt(10)
                spk_run.font.color.rgb = self._rgb(clr["primary"])
            # Texte
            txt_run = p.add_run(entry.get("text", ""))
            txt_run.font.size = Pt(10)
            p.paragraph_format.space_after = Pt(3)

        doc.add_paragraph()

    # ── Prochaines étapes ─────────────────────────────────────────────────────

    def _add_next_steps(self, doc, analysis, clr: dict):
        self._add_section_heading(doc, "🔜 Prochaines Étapes", clr)

        steps = []
        if analysis.tasks:
            steps.append("Réaliser les actions identifiées dans ce rapport")
        if analysis.questions:
            steps.append(f"Répondre aux {len(analysis.questions)} question(s) ouvertes")
        steps.append("Partager ce compte-rendu à tous les participants")
        steps.append("Planifier un point de suivi si nécessaire")

        for step in steps:
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(step)
            run.font.size = Pt(11)
        doc.add_paragraph()

    # ── Pied de page ─────────────────────────────────────────────────────────

    def _add_footer(self, doc, config: ReportConfig):
        for section in doc.sections:
            footer = section.footer
            p = footer.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(
                f"{config.company} — {config.title} — "
                f"Généré par PolyMeet le "
                f"{datetime.datetime.now().strftime('%d/%m/%Y')}")
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    # ── Helpers DOCX ─────────────────────────────────────────────────────────

    def _add_section_heading(self, doc, text: str, clr: dict, level: int = 1):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.font.size  = Pt(14 if level == 1 else 12)
        run.font.bold  = True
        run.font.color.rgb = self._rgb(clr["primary"])
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after  = Pt(6)
        # Ligne sous le titre
        if level == 1:
            pPr = p._p.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            r, g, b = clr["primary"]
            bottom.set(qn("w:color"), f"{r:02X}{g:02X}{b:02X}")
            pBdr.append(bottom)
            pPr.append(pBdr)

    def _add_colored_line(self, doc, clr_tuple: tuple):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(2)
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "1")
        r, g, b = clr_tuple
        bottom.set(qn("w:color"), f"{r:02X}{g:02X}{b:02X}")
        pBdr.append(bottom)
        pPr.append(pBdr)

    def _shade_paragraph(self, p, clr_tuple: tuple):
        pPr = p._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        r, g, b = clr_tuple
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), f"{r:02X}{g:02X}{b:02X}")
        pPr.append(shd)

    def _shade_cell(self, cell, clr_tuple: tuple):
        tc   = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = OxmlElement("w:shd")
        r, g, b = clr_tuple
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), f"{r:02X}{g:02X}{b:02X}")
        tcPr.append(shd)

    # ══════════════════════════════════════════════════════════════════════════
    # F-10 — Génération PDF (reportlab)
    # ══════════════════════════════════════════════════════════════════════════

    def generate_pdf(self, analysis, transcript_entries: list,
                     config: ReportConfig = None) -> str:
        """
        Génère un rapport PDF complet via reportlab.
        Retourne le chemin du fichier créé.
        """
        if not PDF_AVAILABLE:
            print("[Report] ❌ reportlab manquant")
            return ""

        if config is None:
            config = ReportConfig()

        clr_cfg = TEMPLATE_COLORS.get(config.template, TEMPLATE_COLORS["Direction"])
        primary = colors.Color(
            clr_cfg["primary"][0]/255,
            clr_cfg["primary"][1]/255,
            clr_cfg["primary"][2]/255)
        secondary = colors.Color(
            clr_cfg["secondary"][0]/255,
            clr_cfg["secondary"][1]/255,
            clr_cfg["secondary"][2]/255)
        accent = colors.Color(
            clr_cfg["accent"][0]/255,
            clr_cfg["accent"][1]/255,
            clr_cfg["accent"][2]/255)

        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"rapport_{config.template.lower()}_{ts}.pdf"
        path = os.path.join(REPORTS_DIR, name)

        # Mot de passe (F-10 mode protégé)
        encrypt = None
        if config.password:
            try:
                from reportlab.lib.pdfencrypt import StandardEncryption
                encrypt = StandardEncryption(
                    config.password, ownerPassword="polymeet_admin",
                    canPrint=1, canModify=0, canCopy=0)
            except Exception as e:
                print(f"[Report] ⚠ Chiffrement PDF non disponible : {e}")

        doc_pdf = SimpleDocTemplate(
            path,
            pagesize=A4,
            topMargin=2.5*cm, bottomMargin=2.5*cm,
            leftMargin=3.0*cm, rightMargin=2.5*cm,
            encrypt=encrypt)

        styles = getSampleStyleSheet()
        # Styles personnalisés
        title_style = ParagraphStyle(
            "PolyTitle", parent=styles["Title"],
            fontSize=24, textColor=primary,
            spaceAfter=12, alignment=TA_CENTER)
        h1_style = ParagraphStyle(
            "PolyH1", parent=styles["Heading1"],
            fontSize=14, textColor=primary,
            spaceBefore=16, spaceAfter=8,
            borderPadding=(0, 0, 4, 0))
        h2_style = ParagraphStyle(
            "PolyH2", parent=styles["Heading2"],
            fontSize=12, textColor=primary,
            spaceBefore=10, spaceAfter=6)
        body_style = ParagraphStyle(
            "PolyBody", parent=styles["Normal"],
            fontSize=10, spaceAfter=4,
            leading=14, alignment=TA_JUSTIFY)
        bullet_style = ParagraphStyle(
            "PolyBullet", parent=styles["Normal"],
            fontSize=10, spaceAfter=3,
            leftIndent=20, bulletIndent=10)
        small_style = ParagraphStyle(
            "PolySmall", parent=styles["Normal"],
            fontSize=8, textColor=colors.grey, alignment=TA_CENTER)

        story = []
        sections = config.sections or DEFAULT_SECTIONS[config.detail]

        # ── Page de garde ─────────────────────────────────────────────────
        if "page_garde" in sections:
            story.append(Spacer(1, 2*cm))
            story.append(Paragraph(config.company, title_style))
            story.append(HRFlowable(
                width="100%", thickness=3, color=primary, spaceAfter=12))
            story.append(Paragraph(config.title, title_style))
            if config.subtitle or config.department:
                sub_style = ParagraphStyle(
                    "Sub", parent=styles["Normal"],
                    fontSize=13, textColor=colors.grey, alignment=TA_CENTER)
                story.append(Paragraph(
                    config.subtitle or config.department, sub_style))
            story.append(Spacer(1, 1*cm))

            # Infos réunion
            info_data = [
                ["Date", analysis.analyzed_at],
                ["Participants", ", ".join(analysis.participants) or "—"],
                ["Template", config.template],
                ["Sentiment", f"{analysis.sentiment.upper()}"],
            ]
            info_table = Table(info_data, colWidths=[4*cm, 11*cm])
            info_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), secondary),
                ("TEXTCOLOR",  (0, 0), (0, -1), primary),
                ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE",   (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white]),
                ("GRID",       (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("PADDING",    (0, 0), (-1, -1), 6),
            ]))
            story.append(info_table)
            story.append(Spacer(1, 0.5*cm))
            story.append(HRFlowable(
                width="100%", thickness=2, color=primary, spaceAfter=8))
            story.append(Paragraph(
                f"Rapport généré automatiquement par PolyMeet — "
                f"{datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')}",
                small_style))
            story.append(PageBreak())

        # ── Participants ───────────────────────────────────────────────────
        if "participants" in sections and analysis.participants:
            story.append(Paragraph("👥 Participants", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            for name in analysis.participants:
                story.append(Paragraph(f"• {name}", bullet_style))
            story.append(Spacer(1, 0.5*cm))

        # ── Résumé exécutif ───────────────────────────────────────────────
        if "resume_executif" in sections:
            story.append(Paragraph("📋 Résumé Exécutif", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            # Box colorée
            box_data = [[Paragraph(
                analysis.summary_short.replace("\n", "<br/>"), body_style)]]
            box = Table(box_data, colWidths=[15*cm])
            box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), secondary),
                ("PADDING",    (0, 0), (-1, -1), 10),
                ("BOX",        (0, 0), (-1, -1), 1, primary),
            ]))
            story.append(box)
            story.append(Spacer(1, 0.4*cm))

            if analysis.summary_full:
                story.append(Paragraph("Résumé Détaillé", h2_style))
                for para in analysis.summary_full.split("\n\n"):
                    story.append(Paragraph(para.strip(), body_style))

            if analysis.key_points:
                story.append(Paragraph("Points Clés", h2_style))
                for pt in analysis.key_points:
                    story.append(Paragraph(f"• {pt}", bullet_style))
            story.append(Spacer(1, 0.5*cm))

        # ── Décisions ─────────────────────────────────────────────────────
        if "decisions" in sections:
            story.append(Paragraph("✅ Décisions Prises", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            if analysis.decisions:
                for i, d in enumerate(analysis.decisions, 1):
                    story.append(Paragraph(f"{i}. {d}", body_style))
            else:
                story.append(Paragraph(
                    "Aucune décision formelle identifiée.", body_style))
            story.append(Spacer(1, 0.5*cm))

        # ── Tableau des tâches ────────────────────────────────────────────
        if "taches" in sections:
            story.append(Paragraph("📌 Actions à Réaliser", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            if analysis.tasks:
                hdr = [
                    Paragraph("<b>#</b>",            body_style),
                    Paragraph("<b>Tâche</b>",         body_style),
                    Paragraph("<b>Responsable</b>",   body_style),
                    Paragraph("<b>Délai</b>",          body_style),
                    Paragraph("<b>Statut</b>",         body_style),
                ]
                rows = [hdr]
                for i, t in enumerate(analysis.tasks, 1):
                    rows.append([
                        Paragraph(str(i), body_style),
                        Paragraph(t.get("text", "")[:70], body_style),
                        Paragraph(t.get("responsible", "—") or "—", body_style),
                        Paragraph(t.get("deadline", "—") or "—", body_style),
                        Paragraph("⬜ À faire", body_style),
                    ])
                task_table = Table(rows, colWidths=[1*cm, 6.5*cm, 3*cm, 3*cm, 2*cm])
                task_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), primary),
                    ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.white, secondary]),
                    ("GRID",       (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("PADDING",    (0, 0), (-1, -1), 6),
                    ("VALIGN",     (0, 0), (-1, -1), "TOP"),
                ]))
                story.append(task_table)
            else:
                story.append(Paragraph(
                    "Aucune action identifiée.", body_style))
            story.append(Spacer(1, 0.5*cm))

        # ── Transcription ─────────────────────────────────────────────────
        if "transcription" in sections and transcript_entries:
            story.append(PageBreak())
            story.append(Paragraph("📄 Transcription Complète", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            trans_style = ParagraphStyle(
                "Trans", parent=styles["Normal"],
                fontSize=9, spaceAfter=3, leading=13)
            for entry in transcript_entries:
                ts_str  = entry.get("time", "")
                speaker = entry.get("speaker_label", "")
                text    = entry.get("text", "")
                if speaker and speaker != "?":
                    line = f"<font color='grey' size='8'>[{ts_str}]</font> <b>{speaker}:</b> {text}"
                else:
                    line = f"<font color='grey' size='8'>[{ts_str}]</font> {text}"
                story.append(Paragraph(line, trans_style))
            story.append(Spacer(1, 0.5*cm))

        # ── Prochaines étapes ─────────────────────────────────────────────
        if "prochaines_etapes" in sections:
            story.append(Paragraph("🔜 Prochaines Étapes", h1_style))
            story.append(HRFlowable(
                width="100%", thickness=1, color=primary, spaceAfter=6))
            steps = ["Réaliser les actions identifiées dans ce rapport"]
            if analysis.questions:
                steps.append(
                    f"Répondre aux {len(analysis.questions)} question(s) ouvertes")
            steps += [
                "Partager ce compte-rendu à tous les participants",
                "Planifier un point de suivi si nécessaire",
            ]
            for s in steps:
                story.append(Paragraph(f"• {s}", bullet_style))

        # ── Build PDF ─────────────────────────────────────────────────────
        doc_pdf.build(story)
        self._last_pdf = path
        print(f"[Report] ✅ PDF généré : {path}")
        return path

    # ══════════════════════════════════════════════════════════════════════════
    # Utilitaires
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def get_templates() -> list:
        return list(TEMPLATE_COLORS.keys())

    @staticmethod
    def get_detail_levels() -> list:
        return list(DEFAULT_SECTIONS.keys())

    @staticmethod
    def get_all_sections() -> list:
        return ALL_SECTIONS

    @property
    def last_docx(self) -> str:
        return self._last_docx

    @property
    def last_pdf(self) -> str:
        return self._last_pdf