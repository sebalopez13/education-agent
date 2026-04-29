"""
Education News Agent
====================
Corre 2 veces por semana (lunes y viernes).
Lee RSS feeds de fuentes curadas, scorea cada artículo con Claude,
y envía un digest por Gmail.

Setup:
  pip install feedparser anthropic requests python-dotenv

Variables de entorno (.env):
  ANTHROPIC_API_KEY=sk-...
  GMAIL_USER=tu@gmail.com
  GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  (App Password de Google)
  RECIPIENT_EMAIL=tu@gmail.com
"""

import feedparser
import anthropic
import smtplib
import json
import os
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN DE FUENTES
# ─────────────────────────────────────────────

RSS_FEEDS = {
    # Tier 1 - Must haves
    "Edutopia":              "https://www.edutopia.org/feeds/posts",
    "EdSurge":               "https://www.edsurge.com/feeds/posts",
    "MindShift (KQED)":      "https://www.kqed.org/mindshift/feed",
    "The 74":                "https://www.the74million.org/feed/",
    "TeachThought":          "https://www.teachthought.com/feed/",
    "K-12 Dive":             "https://www.k12dive.com/feeds/news/",

    # Tier 2 - Think tanks
    "Education Next":        "https://www.educationnext.org/feed/",
    "Hechinger Report":      "https://hechingerreport.org/feed/",
    "Cult of Pedagogy":      "https://www.cultofpedagogy.com/feed/",

    # Tier 3 - NYC específico
    "XQ Institute":          "https://xqsuperschool.org/feed/",
}

# ─────────────────────────────────────────────
# KEYWORDS PARA PRE-FILTRO (ahorra tokens de API)
# ─────────────────────────────────────────────

MUST_KEYWORDS = [
    "project-based learning", "PBL",
    "montessori", "reggio emilia", "waldorf",
    "21st century", "future-ready", "future of learning",
    "AI in education", "AI in classroom", "artificial intelligence",
    "student-centered", "inquiry-based", "personalized learning",
    "innovative school", "learning by doing",
    "critical thinking", "creativity in education",
    "NYC school", "new york school", "new york education",
    "alternative school", "micro-school",
    "interdisciplinary", "portfolio assessment",
    "EdTech", "education technology",
    "skills for the future", "competencies",
]

BLOCK_KEYWORDS = [
    "standardized test", "test prep", "SAT", "ACT",
    "college ranking", "sports", "athletics",
    "cafeteria", "school bus", "transportation funding",
    "real estate", "weather",
]

# ─────────────────────────────────────────────
# PASO 1: LEER RSS FEEDS
# ─────────────────────────────────────────────

def fetch_articles(feeds: dict, days_back: int = 3) -> list[dict]:
    """Lee todos los feeds y filtra artículos de los últimos N días."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    articles = []

    for source, url in feeds.items():
        print(f"  📡 Leyendo {source}...")
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                # Intentar parsear la fecha
                pub_date = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                # Si no tiene fecha o es muy viejo, saltar
                if pub_date and pub_date < cutoff:
                    continue

                # Extraer texto disponible
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")

                # Limpiar HTML básico del summary
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:800]

                articles.append({
                    "source": source,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "date": pub_date.strftime("%Y-%m-%d") if pub_date else "fecha desconocida",
                })
        except Exception as e:
            print(f"    ⚠️  Error leyendo {source}: {e}")

    print(f"\n  ✅ {len(articles)} artículos encontrados en total")
    return articles


# ─────────────────────────────────────────────
# PASO 2: PRE-FILTRO POR KEYWORDS (sin LLM)
# ─────────────────────────────────────────────

def pre_filter(articles: list[dict]) -> list[dict]:
    """
    Descarta artículos con block keywords.
    Prioriza artículos con must keywords.
    Devuelve máximo 30 candidatos para no gastar demasiados tokens.
    """
    blocked = []
    must = []
    rest = []

    for a in articles:
        text = (a["title"] + " " + a["summary"]).lower()

        if any(kw.lower() in text for kw in BLOCK_KEYWORDS):
            blocked.append(a)
            continue

        if any(kw.lower() in text for kw in MUST_KEYWORDS):
            must.append(a)
        else:
            rest.append(a)

    print(f"  🚫 {len(blocked)} artículos bloqueados")
    print(f"  🎯 {len(must)} artículos con keywords clave")
    print(f"  📄 {len(rest)} artículos restantes")

    # Priorizar must, completar con rest hasta 30
    candidates = (must + rest)[:30]
    return candidates


# ─────────────────────────────────────────────
# PASO 3: SCORING CON CLAUDE
# ─────────────────────────────────────────────

def score_articles(articles: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """
    Claude scorea cada artículo del 1 al 10 según relevancia.
    Solo los de score >= 6 pasan al digest.
    """
    print(f"\n  🤖 Scoring {len(articles)} artículos con Claude...")
    scored = []

    for a in articles:
        prompt = f"""Eres el editor de un newsletter sobre educación del siglo 21.
Tu lector es un profesional curioso que quiere entender cómo preparar a las próximas generaciones, 
con foco en pedagogía innovadora (PBL, Montessori, aprendizaje activo), EdTech, 
inteligencia artificial en educación, y escuelas de vanguardia en NYC y USA.

Evaluá este artículo del 1 al 10 según qué tan relevante e interesante sería para ese lector.
Criterios:
- 9-10: Imprescindible. Idea nueva, caso concreto inspirador, o tendencia importante.
- 7-8: Vale la pena. Útil, relevante, bien enfocado.
- 5-6: Marginal. Relacionado pero poco específico o muy general.
- 1-4: Descartar. Ruido, muy técnico/burocrático, o fuera de foco.

Artículo:
Fuente: {a['source']}
Título: {a['title']}
Resumen: {a['summary'][:500]}

Respondé SOLO con JSON válido, sin texto adicional:
{{"score": 8, "reason": "Describe por qué en máximo 15 palabras"}}"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            # Limpiar posibles backticks
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            a["score"] = result.get("score", 0)
            a["reason"] = result.get("reason", "")
            scored.append(a)
            print(f"    [{a['score']}/10] {a['title'][:60]}...")
        except Exception as e:
            print(f"    ⚠️  Error scoring '{a['title'][:40]}': {e}")
            a["score"] = 0
            a["reason"] = ""
            scored.append(a)

    # Ordenar por score y filtrar >= 6
    scored.sort(key=lambda x: x["score"], reverse=True)
    selected = [a for a in scored if a["score"] >= 6]
    print(f"\n  ✅ {len(selected)} artículos seleccionados (score >= 6)")
    return selected


# ─────────────────────────────────────────────
# PASO 4: GENERAR DIGEST CON CLAUDE
# ─────────────────────────────────────────────

def generate_digest(articles: list[dict], client: anthropic.Anthropic) -> str:
    """Claude genera el digest final en HTML limpio."""

    today = datetime.now().strftime("%A %d %B %Y")
    day_name = datetime.now().strftime("%A")

    # Separar top pick, nyc y resto
    nyc_articles = [a for a in articles if "NYC" in a["source"] or
                    "new york" in (a["title"] + a["summary"]).lower()]
    top = articles[0] if articles else None
    rest = articles[1:4]  # máximo 3 más

    articles_text = "\n\n".join([
        f"[SCORE {a['score']}] {a['source']} | {a['title']}\n{a['summary'][:300]}\nURL: {a['link']}"
        for a in articles[:6]
    ])

    prompt = f"""Generá un digest de educación en HTML limpio (sin <!DOCTYPE>, sin <html>, solo el body content).
Fecha: {today}

Usá esta estructura exacta:

<h2>📚 Education Digest — {today}</h2>

<h3>⭐ Top Pick</h3>
[El artículo más interesante. Título como link. 2-3 oraciones de resumen. Una oración de por qué importa.]

<h3>📰 También vale la pena</h3>
[2-3 artículos. Título como link. 1 oración cada uno.]

{"<h3>🗽 NYC esta semana</h3>[Si hay artículos de NYC, 1-2 con una línea cada uno.]" if nyc_articles else ""}

{"<h3>💡 Idea de la semana</h3>[Solo si es lunes: un concepto educativo relevante para reflexionar, en 2-3 oraciones.]" if day_name == "Monday" else ""}

<hr>
<p style="color:#999;font-size:12px;">Education Digest · 3 veces por semana · Powered by Claude</p>

Artículos disponibles:
{articles_text}

Importante:
- Escribí en español
- Sé breve y directo. Nada de relleno.
- Cada título debe ser un link HTML: <a href="URL">Título</a>
- El tono es de un amigo inteligente que te cuenta lo más interesante"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


# ─────────────────────────────────────────────
# PASO 5: ENVIAR POR GMAIL
# ─────────────────────────────────────────────

def send_email(html_content: str, subject: str):
    """Envía el digest por Gmail usando App Password."""
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("RECIPIENT_EMAIL", gmail_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient

    # Versión HTML
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, recipient, msg.as_string())

    print(f"  📧 Email enviado a {recipient}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("🚀 Education News Agent arrancando...\n")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # 1. Leer feeds
    print("📡 PASO 1: Leyendo RSS feeds...")
    articles = fetch_articles(RSS_FEEDS, days_back=3)

    if not articles:
        print("⚠️  No se encontraron artículos. Terminando.")
        return

    # 2. Pre-filtro
    print("\n🔍 PASO 2: Pre-filtrando por keywords...")
    candidates = pre_filter(articles)

    if not candidates:
        print("⚠️  No quedaron candidatos después del filtro. Terminando.")
        return

    # 3. Scoring con Claude
    print("\n🤖 PASO 3: Scoring con Claude...")
    selected = score_articles(candidates, client)

    if not selected:
        print("⚠️  Ningún artículo superó el score mínimo. Terminando.")
        return

    # 4. Generar digest
    print("\n✍️  PASO 4: Generando digest...")
    digest_html = generate_digest(selected, client)

    # 5. Enviar email
    print("\n📧 PASO 5: Enviando email...")
    today_str = datetime.now().strftime("%d %b")
    subject = f"📚 Education Digest — {today_str}"

    send_email(digest_html, subject)

    print("\n✅ ¡Listo! Digest enviado correctamente.")
    print(f"   {len(selected)} artículos seleccionados de {len(articles)} totales.")


if __name__ == "__main__":
    main()
