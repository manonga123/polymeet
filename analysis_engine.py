"""
analysis_engine.py  –  Module F-07/F-08 : Analyse IA & Résumé Intelligent
──────────────────────────────────────────────────────────────────────────
100% offline — aucune API externe requise.
Utilise des règles linguistiques, regex et analyse de fréquence.

F-07 Analyse Sémantique :
  • Identification des sujets principaux (TF-IDF simplifié)
  • Détection des décisions prises
  • Extraction des tâches/actions avec responsable et délai
  • Identification des questions sans réponse
  • Analyse du sentiment (positif / neutre / tendu)

F-08 Résumé Intelligent :
  • Résumé narratif (3-5 paragraphes)
  • Résumé exécutif ultra-court (5 lignes max)
  • Points clés hiérarchisés
"""

import re
import math
import datetime
from collections import Counter
from typing import Optional

# ─── Marqueurs de décisions ──────────────────────────────────────────────────
DECISION_MARKERS = [
    r"on a décidé", r"nous avons décidé", r"il a été décidé",
    r"on va", r"nous allons", r"il faut", r"on doit", r"nous devons",
    r"c'est décidé", r"convenu que", r"accord sur", r"validé",
    r"approuvé", r"retenu", r"choisi de", r"opté pour",
    r"we decided", r"it was decided", r"we will", r"we agreed",
    r"decision to", r"agreed to", r"approved", r"resolved to",
    r"nifanaraka", r"voafaritra", r"hataontsika",
]

# ─── Marqueurs de tâches ─────────────────────────────────────────────────────
TASK_MARKERS = [
    r"il faut", r"on doit", r"nous devons", r"à faire",
    r"préparer", r"envoyer", r"créer", r"rédiger", r"contacter",
    r"vérifier", r"analyser", r"organiser", r"planifier",
    r"développer", r"finir", r"terminer", r"livrer",
    r"responsable", r"assigné à", r"délai", r"avant le",
    r"need to", r"must", r"action item", r"todo", r"assigned to",
    r"deadline", r"by monday", r"by friday",
]

# ─── Marqueurs de délais ─────────────────────────────────────────────────────
DEADLINE_PATTERNS = [
    r"avant (?:le )?(\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)",
    r"pour (?:le )?(\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)",
    r"d'ici (?:le )?(.{3,20})",
    r"(?:lundi|mardi|mercredi|jeudi|vendredi)",
    r"(?:monday|tuesday|wednesday|thursday|friday)",
    r"(?:cette|la) semaine",
    r"(?:next week|this week)",
]

# ─── Mots de sentiment ───────────────────────────────────────────────────────
POSITIVE_WORDS = {
    "excellent", "parfait", "bien", "bon", "super", "génial", "bravo",
    "accord", "validé", "approuvé", "succès", "réussi", "avancé",
    "progrès", "positif", "satisfait", "content", "heureux",
    "great", "good", "perfect", "agreed", "success", "happy",
    "satisfied", "positive", "approved", "done", "completed",
}

NEGATIVE_WORDS = {
    "problème", "difficile", "impossible", "retard", "bloqué", "échec",
    "mauvais", "insuffisant", "refusé", "rejeté", "annulé", "urgent",
    "critique", "risque", "préoccupant", "conflit",
    "problem", "difficult", "impossible", "delay", "blocked", "failure",
    "bad", "refused", "rejected", "cancelled", "urgent", "critical",
}

# ─── Stopwords ───────────────────────────────────────────────────────────────
STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
    "mais", "donc", "car", "si", "que", "qui", "quoi", "comment",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "on",
    "me", "te", "se", "lui", "leur", "y", "en", "à", "au", "aux",
    "par", "pour", "avec", "sans", "sur", "sous", "dans", "entre",
    "est", "sont", "être", "avoir", "fait", "faire", "dit", "dire",
    "the", "a", "an", "is", "are", "was", "were", "be", "have", "has",
    "do", "does", "will", "would", "could", "should", "can", "of",
    "in", "to", "for", "on", "at", "by", "from", "with", "and", "or",
    "but", "if", "not", "this", "that", "it", "we", "they", "you",
    "notre", "votre", "mon", "ton", "son", "ma", "ta", "sa", "ce",
    "cette", "ces", "dont", "plus", "très", "bien", "aussi", "alors",
    "même", "tout", "tous", "c'est", "cet", "où",
}


class MeetingAnalysis:
    """Résultat complet d'une analyse de réunion."""

    def __init__(self):
        self.topics: list           = []
        self.decisions: list        = []
        self.tasks: list            = []
        self.questions: list        = []
        self.sentiment: str         = "neutre"
        self.sentiment_score: float = 0.0
        self.summary_short: str     = ""
        self.summary_full: str      = ""
        self.key_points: list       = []
        self.word_count: int        = 0
        self.speakers_mentioned: list = []
        self.analyzed_at: str       = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")

    def to_dict(self) -> dict:
        return {
            "topics":             self.topics,
            "decisions":          self.decisions,
            "tasks":              self.tasks,
            "questions":          self.questions,
            "sentiment":          self.sentiment,
            "sentiment_score":    self.sentiment_score,
            "summary_short":      self.summary_short,
            "summary_full":       self.summary_full,
            "key_points":         self.key_points,
            "word_count":         self.word_count,
            "speakers_mentioned": self.speakers_mentioned,
            "analyzed_at":        self.analyzed_at,
        }


class AnalysisEngine:
    """
    Moteur d'analyse sémantique 100% offline.

    Usage :
        engine   = AnalysisEngine()
        analysis = engine.analyze(transcript_text, speakers=["Alice", "Bob"])
        print(analysis.summary_short)
        print(analysis.decisions)
        report = engine.format_analysis_report(analysis)
    """

    def analyze(self, transcript: str,
                speakers: Optional[list] = None) -> MeetingAnalysis:
        result = MeetingAnalysis()

        if not transcript or not transcript.strip():
            result.summary_short = "Aucune transcription disponible."
            return result

        text      = self._clean_text(transcript)
        sentences = self._split_sentences(text)
        words     = self._tokenize(text)

        result.word_count         = len(words)
        result.topics             = self._extract_topics(words, sentences)
        result.decisions          = self._extract_decisions(sentences)
        result.tasks              = self._extract_tasks(sentences, speakers or [])
        result.questions          = self._extract_unanswered_questions(sentences)
        result.sentiment, result.sentiment_score = self._analyze_sentiment(words)
        result.speakers_mentioned = self._extract_speakers(text, speakers or [])
        result.key_points         = self._extract_key_points(result)
        result.summary_short      = self._generate_short_summary(result)
        result.summary_full       = self._generate_full_summary(result, sentences)

        return result

    # ── Sujets ───────────────────────────────────────────────────────────────

    def _extract_topics(self, words: list, sentences: list,
                        max_topics: int = 6) -> list:
        meaningful = [w.lower() for w in words
                      if len(w) > 3 and w.lower() not in STOPWORDS and w.isalpha()]
        if not meaningful:
            return []

        freq  = Counter(meaningful)
        total = len(meaningful)
        scores = {w: (c / total) * (math.log(total / (c + 1)) + 1)
                  for w, c in freq.items()}

        topics = []
        seen   = set()
        for word, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            if len(topics) >= max_topics:
                break
            if word not in seen:
                ctx = next((s for s in sentences if word in s.lower()), "")
                phrase = self._noun_phrase(word, ctx)
                if phrase and phrase not in seen:
                    topics.append(phrase)
                    seen.add(phrase)
                    seen.add(word)
        return topics

    def _noun_phrase(self, word: str, sentence: str, window: int = 3) -> str:
        words = sentence.split()
        try:
            idx   = next(i for i, w in enumerate(words) if word in w.lower())
            start = max(0, idx - 1)
            end   = min(len(words), idx + window)
            phrase = " ".join(words[start:end])
            phrase = re.sub(r'[^\w\s\-]', '', phrase).strip()
            return phrase[:50] if phrase else word
        except StopIteration:
            return word

    # ── Décisions ────────────────────────────────────────────────────────────

    def _extract_decisions(self, sentences: list) -> list:
        decisions = []
        for s in sentences:
            for marker in DECISION_MARKERS:
                if re.search(marker, s.lower()):
                    cleaned = s.strip()
                    if len(cleaned) > 10 and cleaned not in decisions:
                        decisions.append(cleaned)
                    break
        return decisions[:10]

    # ── Tâches ───────────────────────────────────────────────────────────────

    def _extract_tasks(self, sentences: list, speakers: list) -> list:
        tasks = []
        for s in sentences:
            if any(re.search(m, s.lower()) for m in TASK_MARKERS):
                if len(s.strip()) > 10:
                    task = {
                        "text":     s.strip(),
                        "person":   self._find_person(s, speakers),
                        "deadline": self._find_deadline(s),
                        "status":   "À faire",
                    }
                    if not any(t["text"] == task["text"] for t in tasks):
                        tasks.append(task)
        return tasks[:15]

    def _find_person(self, sentence: str, speakers: list) -> str:
        for sp in speakers:
            if sp and sp.lower() in sentence.lower():
                return sp
        matches = re.findall(r'\b([A-Z][a-zéèêàâîïôùûç]+)\b', sentence)
        filtered = [m for m in matches if m.lower() not in STOPWORDS and len(m) > 2]
        return filtered[0] if filtered else "—"

    def _find_deadline(self, sentence: str) -> str:
        for pattern in DEADLINE_PATTERNS:
            match = re.search(pattern, sentence, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return "—"

    # ── Questions sans réponse ────────────────────────────────────────────────

    def _extract_unanswered_questions(self, sentences: list) -> list:
        response_starters = {
            "oui", "non", "bien sûr", "effectivement", "absolument",
            "yes", "no", "of course", "exactly", "je pense",
        }
        questions = []
        for i, s in enumerate(sentences):
            if s.strip().endswith("?") and len(s) > 10:
                answered = False
                if i + 1 < len(sentences):
                    next_s = sentences[i + 1].lower().strip()
                    answered = any(next_s.startswith(r) for r in response_starters)
                if not answered:
                    questions.append(s.strip())
        return questions[:8]

    # ── Sentiment ─────────────────────────────────────────────────────────────

    def _analyze_sentiment(self, words: list) -> tuple:
        if not words:
            return "neutre", 0.0
        w_lower   = [w.lower() for w in words]
        pos_count = sum(1 for w in w_lower if w in POSITIVE_WORDS)
        neg_count = sum(1 for w in w_lower if w in NEGATIVE_WORDS)
        score     = (pos_count - neg_count) / max(len(w_lower) * 0.1, 1)
        score     = max(-1.0, min(1.0, score))
        if score > 0.2:
            label = "positif"
        elif score < -0.2:
            label = "tendu"
        else:
            label = "neutre"
        return label, round(score, 2)

    # ── Speakers ──────────────────────────────────────────────────────────────

    def _extract_speakers(self, text: str, speakers: list) -> list:
        return [sp for sp in speakers if sp and sp.lower() in text.lower()]

    # ── Points clés ──────────────────────────────────────────────────────────

    def _extract_key_points(self, a: MeetingAnalysis) -> list:
        points = []
        if a.topics:
            points.append(f"📌 Sujets : {', '.join(a.topics[:4])}")
        for d in a.decisions[:3]:
            points.append(f"✅ Décision : {d[:100]}")
        for t in a.tasks[:4]:
            p = f" ({t['person']})" if t['person'] != "—" else ""
            dl = f" — délai : {t['deadline']}" if t['deadline'] != "—" else ""
            points.append(f"📋 Action{p} : {t['text'][:80]}{dl}")
        if a.questions:
            points.append(f"❓ {len(a.questions)} question(s) sans réponse")
        emoji = {"positif": "😊", "neutre": "😐", "tendu": "😟"}
        points.append(
            f"{emoji.get(a.sentiment,'😐')} Ambiance : {a.sentiment}")
        return points

    # ── Résumé court ──────────────────────────────────────────────────────────

    def _generate_short_summary(self, a: MeetingAnalysis) -> str:
        participants = (", ".join(a.speakers_mentioned)
                        if a.speakers_mentioned else "les participants")
        lines = [
            f"Réunion du {a.analyzed_at} avec {participants}.",
            f"Sujets : {', '.join(a.topics[:3])}." if a.topics
            else "Aucun sujet principal identifié.",
            (f"{len(a.decisions)} décision(s) prise(s)."
             if a.decisions else "Aucune décision formelle."),
            (f"{len(a.tasks)} action(s) identifiée(s)."
             if a.tasks else "Aucune tâche identifiée."),
            f"Ambiance générale : {a.sentiment}.",
        ]
        return "\n".join(lines)

    # ── Résumé narratif complet ───────────────────────────────────────────────

    def _generate_full_summary(self, a: MeetingAnalysis,
                               sentences: list) -> str:
        participants = (", ".join(a.speakers_mentioned)
                        if a.speakers_mentioned else "les participants")
        topics_str   = ", ".join(a.topics[:4]) if a.topics else "divers sujets"

        p1 = (f"Cette réunion, analysée le {a.analyzed_at}, "
              f"a réuni {participants}. "
              f"Les échanges ont principalement porté sur : {topics_str}. "
              f"La transcription contient {a.word_count} mots au total.")

        paragraphs = [p1]

        if a.decisions:
            dec_str = " ; ".join(
                f"({i+1}) {d[:100]}" for i, d in enumerate(a.decisions[:3]))
            paragraphs.append(
                f"Au cours de cette réunion, {len(a.decisions)} décision(s) "
                f"ont été identifiées : {dec_str}.")

        if a.tasks:
            task_parts = []
            for t in a.tasks[:4]:
                part = t['text'][:80]
                if t['person'] != "—":
                    part += f" (responsable : {t['person']})"
                if t['deadline'] != "—":
                    part += f" — avant {t['deadline']}"
                task_parts.append(part)
            paragraphs.append(
                f"{len(a.tasks)} action(s) ont été identifiées : "
                + " ; ".join(task_parts) + ".")

        if a.questions:
            q_str = " / ".join(q[:60] for q in a.questions[:3])
            paragraphs.append(
                f"Plusieurs questions sont restées sans réponse : {q_str}. "
                f"Ces points méritent un suivi lors de la prochaine réunion.")

        desc = {"positif": "constructive et productive",
                "neutre": "neutre et factuelle",
                "tendu": "parfois tendue avec des points de friction"}
        paragraphs.append(
            f"Dans l'ensemble, l'ambiance a été "
            f"{desc.get(a.sentiment, 'neutre')} "
            f"(score : {a.sentiment_score:+.2f}). "
            f"Il est recommandé de faire un suivi des actions identifiées.")

        return "\n\n".join(paragraphs)

    # ── Utilitaires ───────────────────────────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        text = re.sub(r'\[\d{2}:\d{2}:\d{2}\]', '', text)
        text = re.sub(r'\([A-Z]{2}\)', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _split_sentences(self, text: str) -> list:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if len(s.strip()) > 5]

    def _tokenize(self, text: str) -> list:
        return re.findall(r'\b[a-zA-ZÀ-ÿ]{2,}\b', text)

    def format_analysis_report(self, analysis: MeetingAnalysis) -> str:
        """Formate l'analyse complète en texte lisible pour l'export."""
        lines = [
            "═" * 55,
            "  ANALYSE DE LA RÉUNION — PolyMeet",
            f"  Analysé le : {analysis.analyzed_at}",
            "═" * 55,
            "",
            "📌 SUJETS PRINCIPAUX",
        ]
        for i, t in enumerate(analysis.topics, 1):
            lines.append(f"  {i}. {t}")

        lines += ["", "✅ DÉCISIONS PRISES"]
        if analysis.decisions:
            for d in analysis.decisions:
                lines.append(f"  • {d[:120]}")
        else:
            lines.append("  Aucune décision formelle détectée.")

        lines += ["", "📋 TÂCHES ET ACTIONS"]
        if analysis.tasks:
            for t in analysis.tasks:
                lines.append(f"  • {t['text'][:100]}")
                if t['person'] != "—":
                    lines.append(f"    → Responsable : {t['person']}")
                if t['deadline'] != "—":
                    lines.append(f"    → Délai : {t['deadline']}")
        else:
            lines.append("  Aucune tâche détectée.")

        lines += ["", "❓ QUESTIONS SANS RÉPONSE"]
        if analysis.questions:
            for q in analysis.questions:
                lines.append(f"  • {q[:100]}")
        else:
            lines.append("  Toutes les questions ont reçu une réponse.")

        emoji = {"positif": "😊", "neutre": "😐", "tendu": "😟"}
        lines += [
            "",
            f"{emoji.get(analysis.sentiment,'😐')} SENTIMENT : "
            f"{analysis.sentiment.upper()} "
            f"(score : {analysis.sentiment_score:+.2f})",
            "",
            "─" * 55,
            "RÉSUMÉ EXÉCUTIF (5 lignes)",
            "─" * 55,
            analysis.summary_short,
            "",
            "─" * 55,
            "RÉSUMÉ COMPLET",
            "─" * 55,
            analysis.summary_full,
            "",
            "═" * 55,
        ]
        return "\n".join(lines)