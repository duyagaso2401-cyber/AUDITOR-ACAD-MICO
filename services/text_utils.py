"""
Utilidades de PLN sin dependencias pesadas.

Se implementan a mano (en lugar de spaCy/NLTK) para que el despliegue en Render
arranque rápido y quepa en el plan gratuito. Todas las funciones son puras.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:['’-][A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*")

# Abreviaturas frecuentes que NO terminan oración.
_ABBREVIATIONS = {
    "sr", "sra", "dr", "dra", "lic", "ing", "prof", "etc", "pp", "p", "vol", "ed", "eds",
    "et al", "al", "fig", "núm", "no", "cap", "ej", "vs", "cf", "e.g", "i.e", "mr", "mrs",
    "ms", "st", "jr", "inc", "ltd", "approx", "aprox", "art", "pág", "págs",
}
_SENT_END_RE = re.compile(r"[.!?…]+[\"'”»)\]]*\s+")

STOPWORDS_ES = set("""
a al algo algunas algunos ante antes como con contra cual cuales cuando de del desde donde
durante e el ella ellas ellos en entre era eran es esa esas ese eso esos esta estas este esto
estos fue fueron ha han hasta hay la las le les lo los mas más me mi mis mucho muy ni no nos
o os otra otras otro otros para pero poco por porque que qué quien se ser si sí sin sino sobre
son su sus también tan tanto te tiene tienen todo todos tu tus un una uno unos y ya yo
""".split())

STOPWORDS_EN = set("""
a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had
has have having he her here hers herself him himself his how i if in into is it its itself just
me more most my myself no nor not now of off on once only or other our ours out over own same
she should so some such than that the their theirs them then there these they this those through
to too under until up very was we were what when where which while who whom why will with would
you your yours
""".split())

# Muletillas / fórmulas típicas de texto generado por LLM (ES + EN).
AI_MARKERS = [
    # español
    "en conclusión", "en resumen", "cabe destacar", "cabe resaltar", "es importante destacar",
    "es importante señalar", "es fundamental", "es crucial", "juega un papel crucial",
    "juega un papel fundamental", "desempeña un papel", "en el ámbito de", "en el contexto actual",
    "en la actualidad", "hoy en día", "en este sentido", "por otro lado", "asimismo", "además",
    "no obstante", "sin lugar a dudas", "en última instancia", "a lo largo de", "un amplio abanico",
    "una amplia gama", "en definitiva", "vale la pena mencionar", "es esencial", "de manera integral",
    "de manera significativa", "en el panorama", "profundizar en", "adentrarse en", "fomentar",
    "potenciar", "sinergia", "robusto", "holístico", "multifacético", "paradigma", "crucial",
    "en un mundo cada vez más", "en la era digital", "transformador", "innovador",
    # inglés
    "in conclusion", "in summary", "it is important to note", "it is worth noting",
    "plays a crucial role", "plays a pivotal role", "in today's world", "in the realm of",
    "delve into", "delves into", "furthermore", "moreover", "additionally", "tapestry",
    "a wide range of", "ever-evolving", "ever-changing", "navigate the", "landscape",
    "underscore", "underscores", "leverage", "seamless", "robust", "holistic", "multifaceted",
    "paradigm", "pivotal", "foster", "in essence", "ultimately", "notably", "comprehensive",
]

TRANSITIONS_ES = {
    "además": ["también", "a esto se suma que", "de igual modo"],
    "asimismo": ["del mismo modo", "igualmente", "a su vez"],
    "por otro lado": ["en contraste", "desde otra perspectiva", "a diferencia de lo anterior"],
    "en conclusión": ["en síntesis", "a modo de cierre", "de lo expuesto se desprende que"],
    "en resumen": ["en pocas palabras", "en suma"],
    "cabe destacar que": ["conviene subrayar que", "resulta notable que"],
    "es importante destacar que": ["merece atención que", "conviene precisar que"],
    "es importante señalar que": ["debe precisarse que", "conviene advertir que"],
    "no obstante": ["sin embargo", "aun así", "con todo"],
    "en la actualidad": ["hoy", "en el presente", "actualmente"],
    "hoy en día": ["hoy", "en el presente"],
    "juega un papel crucial": ["resulta determinante", "tiene un peso decisivo"],
    "juega un papel fundamental": ["resulta clave", "ocupa un lugar central"],
    "sin lugar a dudas": ["claramente", "de forma evidente"],
    "en este sentido": ["así", "desde esta óptica", "en esa línea"],
}
TRANSITIONS_EN = {
    "furthermore": ["also", "beyond this", "in addition"],
    "moreover": ["what is more", "besides", "equally"],
    "additionally": ["also", "on top of this"],
    "in conclusion": ["to sum up", "overall", "taken together"],
    "it is important to note that": ["notably,", "note that"],
    "it is worth noting that": ["interestingly,", "one detail stands out:"],
    "plays a crucial role": ["matters greatly", "is central"],
    "delve into": ["examine", "explore", "look closely at"],
    "ultimately": ["in the end", "finally"],
}


@dataclass
class Sentence:
    index: int
    text: str
    start: int
    end: int
    paragraph: int

    @property
    def words(self) -> list[str]:
        return tokenize(self.text)

    def to_dict(self) -> dict:
        return {"index": self.index, "text": self.text, "start": self.start,
                "end": self.end, "paragraph": self.paragraph}


def normalize(text: str) -> str:
    """Normaliza Unicode, comillas y espacios conservando saltos de párrafo."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def tokenize(text: str, lower: bool = True) -> list[str]:
    words = WORD_RE.findall(text)
    return [w.lower() for w in words] if lower else words


def detect_language(text: str) -> str:
    words = tokenize(text[:5000])
    es = sum(w in STOPWORDS_ES for w in words)
    en = sum(w in STOPWORDS_EN for w in words)
    return "es" if es >= en else "en"


def _is_abbreviation(chunk: str) -> bool:
    tail = re.findall(r"([A-Za-zÁÉÍÓÚáéíóúñÑ.]+)\.$", chunk.strip())
    if not tail:
        return False
    token = tail[0].lower().rstrip(".")
    return token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha())


def split_sentences(text: str) -> list[Sentence]:
    """
    Segmenta en oraciones conservando offsets exactos sobre ``text``.
    Los offsets permiten subrayar el texto original en el dashboard y
    reemplazar oraciones al reescribir sin perder el formato de párrafos.
    """
    sentences: list[Sentence] = []
    paragraph = 0
    pos = 0
    for block in re.split(r"(\n+)", text):
        if not block:
            continue
        if block.startswith("\n"):
            pos += len(block)
            paragraph += 1
            continue
        cursor = 0
        for m in _SENT_END_RE.finditer(block):
            candidate = block[cursor:m.end()]
            if _is_abbreviation(block[cursor:m.start() + 1]):
                continue
            # Evita cortar decimales o numeraciones como "3. " seguidos de minúscula
            nxt = block[m.end():m.end() + 1]
            if nxt and nxt.islower():
                continue
            _append(sentences, candidate, pos + cursor, paragraph)
            cursor = m.end()
        if cursor < len(block):
            _append(sentences, block[cursor:], pos + cursor, paragraph)
        pos += len(block)
    return sentences


def _append(out: list[Sentence], raw: str, offset: int, paragraph: int) -> None:
    lead = len(raw) - len(raw.lstrip())
    stripped = raw.strip()
    if not stripped:
        return
    start = offset + lead
    out.append(Sentence(len(out), stripped, start, start + len(stripped), paragraph))


def word_ngrams(words: list[str], n: int = 3) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def containment(a_words: list[str], b_words: list[str], n: int = 3) -> float:
    """Proporción de n-gramas de A presentes en B (medida asimétrica de copia)."""
    a = word_ngrams([strip_accents(w) for w in a_words], n)
    if not a:
        return 0.0
    b = word_ngrams([strip_accents(w) for w in b_words], n)
    return len(a & b) / len(a)


def cosine_tf(a_words: list[str], b_words: list[str], stop: set[str] | None = None) -> float:
    stop = stop or set()
    ca = Counter(strip_accents(w) for w in a_words if w not in stop)
    cb = Counter(strip_accents(w) for w in b_words if w not in stop)
    if not ca or not cb:
        return 0.0
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_markers(text: str) -> list[str]:
    low = text.lower()
    return [m for m in AI_MARKERS if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", low)]


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
