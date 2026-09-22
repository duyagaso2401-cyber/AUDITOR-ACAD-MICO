"""
Perfiles institucionales.

Cada perfil define:
  * norma de citación y guía de estilo que se inyecta en los prompts de Gemini;
  * formato de página que se aplica al exportar el .docx;
  * umbrales de aceptación (IA y similitud) que usa el dashboard para el semáforo.

Para añadir una institución basta con agregar una entrada a ``INSTITUTIONS``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class PageFormat:
    font_name: str = "Times New Roman"
    font_size: float = 12
    line_spacing: float = 2.0          # múltiplo de interlineado
    margin_top_cm: float = 2.54
    margin_bottom_cm: float = 2.54
    margin_left_cm: float = 2.54
    margin_right_cm: float = 2.54
    first_line_indent_cm: float = 1.27
    space_after_pt: float = 0
    justify: bool = False


@dataclass(frozen=True)
class Institution:
    id: str
    name: str
    group: str
    citation_style: str
    language: str
    style_guide: str                   # reglas que recibe el motor de reescritura
    page: PageFormat
    ai_threshold: int = 30             # % máximo aceptable de probabilidad IA
    similarity_threshold: int = 20     # % máximo aceptable de similitud
    notes: list[str] = field(default_factory=list)

    def to_public(self) -> dict:
        data = asdict(self)
        data.pop("style_guide", None)
        return data


INSTITUTIONS: dict[str, Institution] = {
    "upel": Institution(
        id="upel",
        name="UPEL – Universidad Pedagógica Experimental Libertador (Venezuela)",
        group="Latinoamérica",
        citation_style="Manual UPEL de Trabajos de Grado (adaptación APA)",
        language="es",
        style_guide=(
            "Redacción académica en español de Venezuela conforme al Manual de Trabajos de Grado "
            "de Especialización, Maestría y Tesis Doctorales de la UPEL. Usa tercera persona o forma "
            "impersonal (se analizó, se evidenció). Evita la primera persona del singular. Párrafos "
            "con idea central clara, oraciones de longitud variada. Citas autor-fecha (Apellido, año) "
            "y citas textuales de menos de 40 palabras entre comillas; de 40 o más en bloque. No uses "
            "anglicismos innecesarios. Conserva la terminología pedagógica y epistemológica."
        ),
        page=PageFormat(
            font_name="Times New Roman", font_size=12, line_spacing=1.5,
            margin_top_cm=3, margin_bottom_cm=3, margin_left_cm=4, margin_right_cm=3,
            first_line_indent_cm=1.27, justify=True,
        ),
        ai_threshold=25,
        similarity_threshold=15,
        notes=["Margen izquierdo 4 cm", "Interlineado 1,5", "Sangría de 5 espacios"],
    ),
    "unicartagena_apa7": Institution(
        id="unicartagena_apa7",
        name="Universidad de Cartagena / Colombia (APA 7.ª ed.)",
        group="Latinoamérica",
        citation_style="APA 7",
        language="es",
        style_guide=(
            "Redacción académica en español de Colombia conforme a las Normas APA 7.ª edición. "
            "Lenguaje claro, conciso y sin sesgos; se permite la primera persona cuando describe "
            "acciones del autor. Citas autor-fecha: (Apellido, año) o Apellido (año); tres o más "
            "autores con 'et al.' desde la primera cita. Citas textuales de 40 o más palabras en "
            "bloque sin comillas. Evita redundancias, voz pasiva excesiva y conectores repetidos."
        ),
        page=PageFormat(
            font_name="Times New Roman", font_size=12, line_spacing=2.0,
            margin_top_cm=2.54, margin_bottom_cm=2.54, margin_left_cm=2.54, margin_right_cm=2.54,
            first_line_indent_cm=1.27, justify=False,
        ),
        ai_threshold=30,
        similarity_threshold=20,
        notes=["Márgenes 2,54 cm", "Doble espacio", "Alineación izquierda"],
    ),
    "ieee": Institution(
        id="ieee",
        name="Estándar internacional – IEEE (Ingeniería y Tecnología)",
        group="Internacional",
        citation_style="IEEE (numérico entre corchetes)",
        language="auto",
        style_guide=(
            "Technical/scientific register following IEEE Editorial Style. Precise, concise, "
            "active voice preferred. Numeric citations in square brackets [1], [2]–[4], placed "
            "inside the sentence punctuation. Define acronyms at first use. Keep equations, units "
            "(SI) and numbers unchanged. Write in the same language as the input."
        ),
        page=PageFormat(
            font_name="Times New Roman", font_size=10, line_spacing=1.0,
            margin_top_cm=1.9, margin_bottom_cm=2.54, margin_left_cm=1.65, margin_right_cm=1.65,
            first_line_indent_cm=0.35, justify=True,
        ),
        ai_threshold=30,
        similarity_threshold=20,
        notes=["Times 10 pt", "Interlineado sencillo", "Citas [n]"],
    ),
    "vancouver": Institution(
        id="vancouver",
        name="Estándar internacional – Vancouver (Ciencias de la Salud / ICMJE)",
        group="Internacional",
        citation_style="Vancouver (numérico entre paréntesis o superíndice)",
        language="auto",
        style_guide=(
            "Biomedical scientific register following ICMJE recommendations and Vancouver "
            "citation style. Numeric citations in order of appearance, e.g. (1) or (1-3). "
            "Objective, impersonal tone; avoid hedging clichés; keep clinical terminology, drug "
            "names, doses and statistics unchanged. Write in the same language as the input."
        ),
        page=PageFormat(
            font_name="Arial", font_size=11, line_spacing=1.5,
            margin_top_cm=2.5, margin_bottom_cm=2.5, margin_left_cm=2.5, margin_right_cm=2.5,
            first_line_indent_cm=0, space_after_pt=6, justify=True,
        ),
        ai_threshold=30,
        similarity_threshold=20,
        notes=["Arial 11", "Interlineado 1,5", "Citas (n)"],
    ),
}

DEFAULT_INSTITUTION = "unicartagena_apa7"


def get_institution(institution_id: str | None) -> Institution:
    """Devuelve el perfil solicitado o el perfil por defecto."""
    return INSTITUTIONS.get((institution_id or "").strip().lower(), INSTITUTIONS[DEFAULT_INSTITUTION])


def list_institutions() -> list[dict]:
    return [inst.to_public() for inst in INSTITUTIONS.values()]
