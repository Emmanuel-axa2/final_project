from flask import Flask
import os
import time
import uuid
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import json
from dotenv import load_dotenv
from groq import Groq
from werkzeug.utils import secure_filename
from io import BytesIO
from prompts.build_prompt import build_cv_prompt
from prompts.update_prompt import improve_cv_prompt
from prompts.Jobmatch_prompt import match_cv_prompt
from flask import session, redirect, url_for, render_template, request
from community_db import init_db, get_db
from datetime import datetime
from flask import flash
import itertools
import re
from flask import send_file
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from weasyprint import HTML
import textwrap
from flask_session import Session
import tempfile
# ---------- CONFIG & SETUP ----------
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = tempfile.mkdtemp()
Session(app)

# ---------- GROQ CLIENT ----------
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# ---------- IN-MEMORY STORES ----------
_question_id_counter = itertools.count(1)
QUESTIONS = []
REVIEWS = []
# ---------- TEMPLATES ----------
TEMPLATES = [
    {"id": "modern",  "name": "Modern",  "tag": "Clean + ATS"},
    {"id": "classic", "name": "Classic", "tag": "Traditional"},
    {"id": "sidebar", "name": "Sidebar", "tag": "Vertical layout"},
]
# ================================================================
#  SCORING HELPERS
# ================================================================
def tfidf_match_score(cv_text: str, job_desc: str) -> float:
    def extract_keywords(text):
        words = re.findall(r'\b[a-zA-Z][a-zA-Z+#\.]{2,}\b', text)
        stopwords = {
            "and", "the", "for", "with", "you", "are", "our", "will", "this",
            "that", "have", "from", "your", "we", "their", "they", "been",
            "would", "about", "working", "work", "role", "team", "able",
            "experience", "including", "such", "also", "which", "must", "can",
            "using", "strong", "good", "key", "within", "across", "looking",
            "ensure", "provide", "support", "develop", "manage", "deliver",
            "ability", "skills", "knowledge", "understanding", "relevant",
            "required", "preferred", "demonstrated", "excellent", "effective",
            "responsible", "opportunity", "position", "candidate", "successful"
        }
        return " ".join(w.lower() for w in words if w.lower() not in stopwords)

    cv_keywords  = extract_keywords(cv_text)
    job_keywords = extract_keywords(job_desc)

    if not cv_keywords or not job_keywords:
        return 0.0

    vectorizer = TfidfVectorizer()
    vectors = vectorizer.fit_transform([cv_keywords, job_keywords])
    score = cosine_similarity(vectors[0], vectors[1])[0][0]
    return round(float(score) * 100, 1)
def keyword_overlap_score(matched: list, missing: list) -> float:
    total = len(matched) + len(missing)
    if total == 0:
        return 0.0
    return round((len(matched) / total) * 100, 1)
def hybrid_match_score(cv_text: str, job_desc: str, matched: list, missing: list) -> dict:
    """50% TF-IDF cosine + 50% keyword overlap."""
    tfidf   = tfidf_match_score(cv_text, job_desc)
    overlap = keyword_overlap_score(matched, missing)
    hybrid  = round((0.5 * tfidf) + (0.5 * overlap), 1)
    return {
        "tfidf_score":   tfidf,
        "overlap_score": overlap,
        "hybrid_score":  hybrid,
    }
# ================================================================
#  TEXT EXTRACTION
# ================================================================
def extract_text_from_pdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(file_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
def extract_text_from_docx(file_bytes: bytes) -> str:
    import docx
    doc = docx.Document(BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs).strip()
def extract_text_from_txt(file_bytes: bytes) -> str:
    try:
        return file_bytes.decode("utf-8").strip()
    except Exception:
        return file_bytes.decode("latin-1", errors="ignore").strip()
def extract_uploaded_cv(request) -> str:
    """Try file upload first, fall back to pasted text."""
    cv_text = ""
    uploaded_file = request.files.get("cv_file")
    if uploaded_file and uploaded_file.filename:
        filename = secure_filename(uploaded_file.filename.lower())
        file_bytes = uploaded_file.read()
        if filename.endswith(".pdf"):
            cv_text = extract_text_from_pdf(file_bytes)
        elif filename.endswith(".docx"):
            cv_text = extract_text_from_docx(file_bytes)
        elif filename.endswith(".txt"):
            cv_text = extract_text_from_txt(file_bytes)
    if not cv_text:
        cv_text = request.form.get("cv_text", "").strip()
    return cv_text
# ================================================================
#  CV PARSING & CONTACT EXTRACTION
# ================================================================
def extract_contact_links(text: str) -> dict:
    text = text or ""

    def first(pattern):
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else ""

    urls = re.findall(r"(https?://\S+)", text, flags=re.I)
    website = ""
    for u in urls:
        if "linkedin.com" not in u.lower() and "github.com" not in u.lower():
            website = u.strip()
            break

    return {
        "email":    first(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})"),
        "phone":    first(r"(\+?\d[\d\s().-]{8,}\d)"),
        "linkedin": first(r"(https?://(?:www\.)?linkedin\.com/\S+)"),
        "github":   first(r"(https?://(?:www\.)?github\.com/\S+)"),
        "website":  website,
    }
def parse_prompt_cv(cv_text: str) -> dict:
    lines = [l.strip() for l in (cv_text or "").splitlines() if l.strip()]

    # Remove unwanted intro lines
    while lines and (
        lines[0].lower().startswith("improved cv") or
        lines[0].lower().startswith("based on") or
        len(lines[0].split()) > 12
    ):
        lines.pop(0)

    name    = lines[0] if len(lines) > 0 else ""
    contact = lines[1] if len(lines) > 1 else ""

    VALID_HEADERS = [
        "PROFILE", "EDUCATION", "CERTIFICATIONS",
        "EXPERIENCE", "PROJECTS", "SKILLS",
        "FEEDBACK", "WEAKNESSES FIXED"
    ]

    sections = {}
    current  = None

    for line in lines[2:]:
        s = line.strip()
        header_candidate = re.sub(r":\s*$", "", s).strip().upper()
        if header_candidate in VALID_HEADERS:
            current = header_candidate
            sections[current] = []
            continue
        if current:
            sections[current].append(line)

    linkedin = ""
    github   = ""
    website  = ""
    if contact:
        m = re.search(r"(https?://(www\.)?linkedin\.com/\S+)", contact, re.I)
        if m:
            linkedin = m.group(1)
        m = re.search(r"(https?://(www\.)?github\.com/\S+)", contact, re.I)
        if m:
            github = m.group(1)
        m = re.search(r"(https?://\S+)", contact, re.I)
        if m:
            website = m.group(1)

    return {
        "name": name, "contact": contact,
        "linkedin": linkedin, "github": github, "website": website,
        "sections": sections,
    }
# ================================================================
#  CV OUTPUT PARSERS
# ================================================================
def extract_improved_cv_block(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    m_start = re.search(r"(?i)\bImproved\s*CV\s*:\s*", t)
    if not m_start:
        return t
    start_idx = m_start.end()
    m_end = re.search(r"(?i)\bWeaknesses\s*Fixed\s*:\s*|\bConfidence\s*:\s*", t[start_idx:])
    if m_end:
        return t[start_idx: start_idx + m_end.start()].strip()
    return t[start_idx:].strip()
def add_cv_spacing(cv_text: str) -> str:
    lines = cv_text.splitlines()
    spaced = []
    headers = {"PROFILE", "EDUCATION", "CERTIFICATIONS", "EXPERIENCE", "PROJECTS", "SKILLS"}
    for i, line in enumerate(lines):
        stripped = line.strip().upper().rstrip(":")
        if stripped in headers:
            if spaced and spaced[-1] != "":
                spaced.append("")  # blank line before header
            spaced.append(line)
            spaced.append("")  # blank line after header
        elif line.strip().startswith("•"):
            spaced.append(line)
        else:
            spaced.append(line)
            if i < len(lines) - 1 and lines[i+1].strip().startswith("•"):
                spaced.append("")  # blank line before bullet list
    return "\n".join(spaced)
def extract_weaknesses_fixed_block(text: str) -> list:
    if not text:
        return []
    m = re.search(
        r"(?is)\bWeaknesses\s*Fixed\b\s*:?\s*(.*?)(?:\n\s*Confidence\s*:|\Z)",
        text
    )
    if not m:
        return []
    block = m.group(1).strip()
    items = []
    for line in block.splitlines():
        s = re.sub(r"^[-•\d\)\.]+\s*", "", line.strip()).strip()
        if s:
            items.append(s)
    return items
def extract_confidence(text: str) -> str:
    m = re.search(r"(?i)\bConfidence\s*:\s*\[?(Low|Medium|High)]?\b", text or "")
    return m.group(1).title() if m else "Medium"
# ================================================================
#  PDF GENERATION
# ================================================================
def cv_text_to_pdf_bytes(title: str, cv_text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left_margin = 0.75 * inch
    top_margin  = height - 0.75 * inch
    line_height = 12

    c.setFont("Helvetica-Bold", 14)
    c.drawString(left_margin, top_margin, title)
    y = top_margin - 24
    c.setFont("Helvetica", 10)

    for raw_line in (cv_text or "").splitlines():
        line    = raw_line.rstrip()
        wrapped = textwrap.wrap(line, width=95) if line else [""]
        for w in wrapped:
            if y < 0.75 * inch:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = height - 0.75 * inch
            c.drawString(left_margin, y, w)
            y -= line_height

    c.save()
    buffer.seek(0)
    return buffer.read()
# ================================================================
#  SESSION HELPERS
# ================================================================
def get_session_id():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]
def clear_last_preview():
    for key in [
        "last_flow", "last_generated_cv", "last_improved_cv",
        "last_original_cv", "last_confidence",
    ]:
        session.pop(key, None)
# ================================================================
#  CORE AI FUNCTIONS
# ================================================================
def generate_cv_from_inputs(user_data: dict) -> dict:
    prompt = build_cv_prompt(user_data)

    start_time = time.time()  # ← start timer

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You are an expert CV writer. Output ONLY the CV, no commentary."},
            {"role": "user", "content": prompt},
        ],
    )

    inference_time = round(time.time() - start_time, 2)  # ← stop timer

    text = add_cv_spacing(response.choices[0].message.content.strip())
    return {
        "generated_cv": text,
        "inference_time": inference_time,  # ← add to return
        "model_used": GROQ_MODEL,
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
def improve_cv_text(cv_text: str, target_role: str, match_notes: str = "") -> dict:
    cv_text     = (cv_text     or "").strip()
    target_role = (target_role or "").strip()
    start_time = time.time()

    if not cv_text:
        return {
            "improved_text":    "",
            "improved_cv_only": "",
            "weaknesses_fixed": [],
            "suggestions":      ["Please paste your CV text first."],
            "confidence":       "N/A",
        }

    contact = extract_contact_links(cv_text)
    forced_contact_line = " | ".join(v for v in contact.values() if v)

    match_block = ""
    if match_notes:
        match_block = f"""
⚠️ PRIORITY INSTRUCTIONS FROM JOB MATCH ANALYSIS ⚠️
You MUST follow these instructions. They are based on a real analysis of this CV against a specific job description.

{match_notes}

These are NOT optional suggestions. Incorporate selected keywords naturally into the CV.
Rewrite bullet points to reflect the tailoring suggestions above.
DO NOT ignore any of the above instructions.
──────────────────────────────────────────────────
"""

    prompt = f"{match_block}\n{improve_cv_prompt(cv_text, target_role, forced_contact_line)}"

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert CV consultant. "
                    "Never fabricate experience. "
                    "Incorporate all specified keywords naturally. "
                    "The CV MUST fit within 2 pages maximum."
                )
            },
            {"role": "user", "content": prompt},
        ],
    )

    content          = response.choices[0].message.content.strip()
    improved_cv_only = extract_improved_cv_block(content)
    weaknesses_fixed = extract_weaknesses_fixed_block(content)
    confidence       = extract_confidence(content)

    lines = [l.rstrip() for l in improved_cv_only.splitlines() if l.strip()]
    if lines:
        if len(lines) == 1:
            lines.insert(1, forced_contact_line)
        else:
            lines[1] = forced_contact_line
    improved_cv_only = add_cv_spacing("\n".join(lines))
    inference_time = round(time.time() - start_time, 2)

    return {
        "improved_text":     content,
        "improved_cv_only":  improved_cv_only,
        "weaknesses_fixed":  weaknesses_fixed,
        "suggestions":       [],
        "confidence":        confidence,
        "model_used":        GROQ_MODEL,
        "prompt_tokens":     response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens":      response.usage.total_tokens,
        "inference_time" : inference_time,
    }
def match_cv_to_job(cv_text: str, job_desc: str, target_role: str) -> dict:
    cv_text  = (cv_text  or "").strip()
    job_desc = (job_desc or "").strip()

    if not cv_text or not job_desc:
        return {"error": "Please provide both CV and job description."}

    prompt = match_cv_prompt(cv_text, job_desc, target_role)

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user",   "content": prompt},
        ],
    )

    content = response.choices[0].message.content.strip()

    try:
        result = json.loads(content)

        scores = hybrid_match_score(
            cv_text, job_desc,
            result.get("matched", []),
            result.get("missing", []),
        )
        result["tfidf_score"]   = scores["tfidf_score"]
        result["overlap_score"] = scores["overlap_score"]
        result["hybrid_score"]  = scores["hybrid_score"]

        # ✅ Save to session for "Send to Improver" feature
        session["match_cv_text"]     = cv_text
        session["match_suggestions"] = result.get("suggestions", [])
        session["match_missing"]     = result.get("missing", [])
        session["match_target_role"] = target_role

        return result

    except Exception:
        return {"error": "Model returned invalid JSON.", "raw": content}
# ================================================================
#  ROUTES — GENERAL
# ================================================================
@app.route("/")
def index():
    return render_template("index.html", reviews=REVIEWS)
@app.post("/review")
def add_review():
    name = (request.form.get("name") or "").strip()
    text = (request.form.get("review") or "").strip()
    if not name or not text:
        flash("Please enter both name and review.", "error")
        return redirect(url_for("index"))
    REVIEWS.insert(0, {
        "name": name[:40],
        "text": text[:400],
        "ts":   datetime.utcnow().strftime("%d %b %Y"),
    })
    flash("Thanks! Your review was posted.", "success")
    return redirect(url_for("index"))
@app.route("/reset")
def reset_session():
    clear_last_preview()
    for key in [
        "draft_improve_cv_text", "draft_improve_target_role",
        "template_id", "last_full_improve_output", "last_original_cv",
        "match_cv_text", "match_suggestions", "match_missing", "match_target_role",
        "prefill_cv_text", "prefill_target_role", "prefill_notes",
    ]:
        session.pop(key, None)
    flash("Started a new session.", "success")
    return redirect(url_for("index"))
@app.route("/internships")
def internships():
    return render_template("internships.html")
# ================================================================
#  ROUTES — BUILD
# ================================================================
@app.get("/start/build")
def start_build():
    session["template_next"] = "build"
    return redirect(url_for("choose_template", next="build"))
@app.route("/build", methods=["GET", "POST"])
def build_cv():
    generated_cv = None
    cv_struct    = None
    inference_time = None  # ← add this default

    if request.method == "GET":
        if session.pop("from_template", False) and session.get("last_generated_cv"):
            generated_cv = session.get("last_generated_cv")

    if request.method == "POST":
        user_data = {
            "full_name":      request.form.get("full_name", ""),
            "email":          request.form.get("email", ""),
            "phone":          request.form.get("phone", ""),
            "summary":        request.form.get("summary", ""),
            "education":      request.form.get("education", ""),
            "experience":     request.form.get("experience", ""),
            "projects":       request.form.get("projects", ""),
            "skills":         request.form.get("skills", ""),
            "certifications": request.form.get("certifications", ""),
            "target_role":    request.form.get("target_role", ""),
            "linkedin":       request.form.get("linkedin", ""),
            "github":         request.form.get("github", ""),
            "website":        request.form.get("website", ""),
        }
        result         = generate_cv_from_inputs(user_data)
        generated_cv   = result["generated_cv"]
        inference_time = result.get("inference_time", None)  # ← get from result

        session["last_generated_cv"] = generated_cv
        session["last_flow"]         = "build"

    template_id = session.get("template_id", "modern")
    if generated_cv:
        cv_struct = parse_prompt_cv(generated_cv)

    return render_template(
        "build.html",
        generated_cv=generated_cv,
        template_id=template_id,
        cv=cv_struct,
        inference_time=inference_time,  # ← always passed, None on GET
    )
# ================================================================
#  ROUTES — IMPROVE
# ================================================================
@app.get("/start/improve")
def start_improve():
    session["template_next"] = "improve"
    return redirect(url_for("choose_template", next="improve"))
@app.route("/improve", methods=["GET", "POST"])
def improve_cv():
    result    = None
    cv_struct = None
    inference_time = None  # ← add default

    prefill_cv_text     = session.pop("prefill_cv_text",     "")
    prefill_target_role = session.pop("prefill_target_role", "")
    prefill_notes       = session.pop("prefill_notes",       "")

    if request.method == "GET":
        if session.get("last_improved_cv"):
            improved_cv_only = session.get("last_improved_cv", "")
            result = {
                "improved_cv_only": improved_cv_only,
                "confidence":       session.get("last_confidence", ""),
                "weaknesses_fixed": session.get("last_weakness_fixed", []),
                "original_text":    session.get("last_original_cv", ""),
            }
            cv_struct = parse_prompt_cv(improved_cv_only)
        session.pop("from_template", None)

    if request.method == "POST":
        target_role  = request.form.get("target_role", "").strip()
        match_notes  = request.form.get("match_notes", "").strip()
        cv_text      = extract_uploaded_cv(request)

        result                  = improve_cv_text(cv_text, target_role, match_notes)
        result["original_text"] = cv_text
        inference_time          = result.get("inference_time", None)  # ← get from result

        improved_cv_only = result.get("improved_cv_only", "")
        cv_struct        = parse_prompt_cv(improved_cv_only)

        session["last_improved_cv"]    = improved_cv_only
        session["last_weakness_fixed"] = result.get("weaknesses_fixed", [])
        session["last_original_cv"]    = cv_text
        session["last_flow"]           = "improve"
        session["last_confidence"]     = result.get("confidence", "")

    template_id = session.get("template_id", "modern")
    return render_template(
        "improve.html",
        result=result,
        template_id=template_id,
        cv=cv_struct,
        prefill_cv_text=prefill_cv_text,
        prefill_target_role=prefill_target_role,
        prefill_notes=prefill_notes,
        inference_time=inference_time,  # ← pass to template
    )
# ================================================================
#  ROUTES — MATCH
# ================================================================
@app.route("/match", methods=["GET", "POST"])
def match_job():
    result = None

    if request.method == "POST":
        job_desc    = request.form.get("job_desc", "").strip()
        target_role = request.form.get("target_role", "").strip()
        cv_text     = ""

        uploaded_file = request.files.get("cv_file")
        if uploaded_file and uploaded_file.filename:
            filename   = secure_filename(uploaded_file.filename.lower())
            file_bytes = uploaded_file.read()
            if filename.endswith(".pdf"):
                cv_text = extract_text_from_pdf(file_bytes)
            elif filename.endswith(".docx"):
                cv_text = extract_text_from_docx(file_bytes)
            elif filename.endswith(".txt"):
                cv_text = extract_text_from_txt(file_bytes)
            else:
                result = {"error": "Unsupported file type. Upload PDF, DOCX, or TXT."}

        if not cv_text:
            cv_text = request.form.get("cv_text", "").strip()

        if not result:
            if not cv_text or not job_desc:
                result = {"error": "Please provide BOTH: a CV (paste or upload) and a job description."}
            else:
                result = match_cv_to_job(cv_text, job_desc, target_role)

    return render_template("match.html", result=result)
@app.get("/match/send-to-improve")
def match_send_to_improve():
    cv_text     = session.get("match_cv_text", "")
    target_role = session.get("match_target_role", "")

    if not cv_text:
        flash("No CV found from match. Please run a job match first.", "error")
        return redirect(url_for("match_job"))

    selected_keywords    = request.args.getlist("keyword")
    selected_suggestions = request.args.getlist("suggestion")

    notes = []
    if selected_keywords:
        notes.append(
            "The following skills and terms are relevant to the target role but are either "
            "missing or not clearly expressed in the CV. Where they genuinely reflect the "
            "candidate's experience, weave them in naturally — do NOT force keywords that "
            "the candidate has no real experience with: " + ", ".join(selected_keywords)
        )
    if selected_suggestions:
        notes.append(
            "A recruiter reviewing this CV against the job description made the following observations. "
            "Apply these only where they reflect the candidate's genuine background:"
        )
        for s in selected_suggestions:
            notes.append(f"- {s}")

    notes.append(
        "IMPORTANT: Do not rewrite this CV specifically for this one role. "
        "The improved CV should read as a strong general profile that happens to "
        "align well with this role — not as a targeted application. "
        "A recruiter should not be able to tell this CV was written for a specific job posting."
    )

    session["prefill_cv_text"]     = cv_text
    session["prefill_target_role"] = target_role
    session["prefill_notes"]       = "\n".join(notes)

    return redirect(url_for("improve_cv"))
# ================================================================
#  ROUTES — TEMPLATES & DOWNLOAD
# ================================================================
@app.get("/templates")
def choose_template():
    next_page = request.args.get("next", "build")
    session["template_next"] = next_page
    selected = session.get("template_id", "modern")
    return render_template(
        "templates.html",
        templates=TEMPLATES,
        selected=selected,
        next_page=next_page,
    )
@app.post("/templates")
def set_template():
    session["template_id"] = request.form.get("template_id", "modern")
    next_page = request.form.get("next_page") or session.get("template_next") or "build"
    session["from_template"] = True

    if next_page == "improve":
        return redirect(url_for("improve_cv"))
    return redirect(url_for("build_cv"))
@app.get("/download/cv")
def download_cv_pdf():
    template_id = session.get("template_id", "modern")
    flow        = session.get("last_flow")
    cv_text     = session.get("last_generated_cv" if flow == "build" else "last_improved_cv", "")

    if not cv_text:
        return redirect(url_for("index"))

    cv_struct = parse_prompt_cv(cv_text) if template_id == "sidebar" else None

    html = render_template(
        "download_cv.html",
        template_id=template_id,
        cv_text=cv_text,
        cv=cv_struct,
    )
    pdf_bytes = HTML(string=html, base_url=request.root_url).write_pdf()

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"cv_{template_id}.pdf",
    )
@app.get("/download/improve.pdf")
def download_improve_pdf():
    cv_text = session.get("last_improved_cv", "")
    if not cv_text:
        flash("No improved CV found. Please improve a CV first.", "error")
        return redirect(url_for("improve_cv"))

    pdf_bytes = cv_text_to_pdf_bytes("Improved CV", cv_text)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="improved_cv.pdf",
    )
@app.get("/print/improve")
def print_improve():
    cv_text = session.get("last_improved_cv", "")
    if not cv_text:
        flash("No improved CV found.", "error")
        return redirect(url_for("improve_cv"))
    return render_template(
        "print_cv.html",
        template_id=session.get("template_id", "modern"),
        cv=parse_prompt_cv(cv_text),
        title="Improved CV",
    )
@app.get("/print/build")
def print_build():
    cv_text = session.get("last_generated_cv", "")
    if not cv_text:
        flash("No generated CV found.", "error")
        return redirect(url_for("build_cv"))
    return render_template(
        "print_cv.html",
        template_id=session.get("template_id", "modern"),
        cv=parse_prompt_cv(cv_text),
        title="Generated CV",
    )
# ================================================================
#  ROUTES — COMMUNITY Q&A
# ================================================================
@app.route("/questions", methods=["GET", "POST"])
def questions():
    db  = get_db()
    cur = db.cursor()
    if request.method == "POST":
        q_text = request.form.get("question", "").strip()
        if q_text:
            cur.execute(
                "INSERT INTO questions (text, created_at) VALUES (?, ?)",
                (q_text, datetime.utcnow().isoformat()),
            )
            db.commit()
        return redirect(url_for("questions"))

    cur.execute("SELECT id, text FROM questions ORDER BY id DESC")
    rows      = cur.fetchall()
    questions = [{"id": r[0], "text": r[1]} for r in rows]
    db.close()
    return render_template("questions.html", questions=questions)
@app.route("/questions/<int:question_id>", methods=["GET", "POST"])
def question_detail(question_id):
    db  = get_db()
    cur = db.cursor()

    if request.method == "POST":
        a_text = request.form.get("answer", "").strip()
        if a_text:
            cur.execute(
                "INSERT INTO answers (question_id, text, created_at) VALUES (?, ?, ?)",
                (question_id, a_text, datetime.utcnow().isoformat()),
            )
            db.commit()
        return redirect(url_for("question_detail", question_id=question_id))

    cur.execute("SELECT text FROM questions WHERE id = ?", (question_id,))
    q_row = cur.fetchone()
    if not q_row:
        db.close()
        return "Question not found", 404

    cur.execute(
        "SELECT text FROM answers WHERE question_id = ? ORDER BY id ASC",
        (question_id,),
    )
    question = {
        "id":      question_id,
        "text":    q_row[0],
        "answers": [{"text": r[0]} for r in cur.fetchall()],
    }
    db.close()
    return render_template("question_detail.html", question=question)
#  MAIN
# ================================================================
if __name__ == "__main__":
    init_db()
    app.run(debug=True)