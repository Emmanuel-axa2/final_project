from flask import Flask, render_template, request

app = Flask(__name__)


# ---------- PLACEHOLDER LOGIC (TO BE REPLACED WITH LLM LATER) ----------

def generate_cv_from_inputs(user_data: dict) -> str:
    """
    Placeholder: formats user data into a simple CV-like string.
    Later this will call the LLM to improve wording and structure.
    """
    lines = []
    name = user_data.get("full_name", "").strip()
    email = user_data.get("email", "").strip()
    phone = user_data.get("phone", "").strip()

    if name:
        lines.append(name.upper())
    if email:
        lines.append(email)
    if phone:
        lines.append(phone)
    lines.append("")

    lines.append("PROFILE")
    lines.append(user_data.get("summary", ""))
    lines.append("")

    lines.append("EDUCATION")
    lines.append(user_data.get("education", ""))
    lines.append("")

    lines.append("EXPERIENCE")
    lines.append(user_data.get("experience", ""))
    lines.append("")

    lines.append("PROJECTS")
    lines.append(user_data.get("projects", ""))
    lines.append("")

    lines.append("SKILLS")
    lines.append(user_data.get("skills", ""))

    return "\n".join(lines).strip()


def improve_cv_text(cv_text: str, target_role: str) -> dict:
    """
    Placeholder: later this will call the LLM.
    For now, it returns the same text with some dummy suggestions.
    """
    cv_text = cv_text.strip()

    if not cv_text:
        return {
            "improved_text": "",
            "suggestions": ["Please paste your CV text first."],
            "confidence": "N/A",
        }

    suggestions = [
        "Try adding more measurable results (e.g. 'Increased X by 20%').",
        f"Highlight skills that match the role: {target_role or 'your target role'}."
    ]

    return {
        "improved_text": cv_text,  # later: improved by LLM
        "suggestions": suggestions,
        "confidence": "Medium (placeholder)",  # later: real confidence score
    }


# ---------- ROUTES ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/build", methods=["GET", "POST"])
def build_cv():
    generated_cv = None

    if request.method == "POST":
        user_data = {
            "full_name": request.form.get("full_name", ""),
            "email": request.form.get("email", ""),
            "phone": request.form.get("phone", ""),
            "summary": request.form.get("summary", ""),
            "education": request.form.get("education", ""),
            "experience": request.form.get("experience", ""),
            "projects": request.form.get("projects", ""),
            "skills": request.form.get("skills", ""),
            "target_role": request.form.get("target_role", ""),
        }

        generated_cv = generate_cv_from_inputs(user_data)

    return render_template("build.html", generated_cv=generated_cv)


@app.route("/improve", methods=["GET", "POST"])
def improve_cv():
    result = None

    if request.method == "POST":
        cv_text = request.form.get("cv_text", "")
        target_role = request.form.get("target_role", "")

        result = improve_cv_text(cv_text, target_role)

    return render_template("improve.html", result=result)


if __name__ == "__main__":
    app.run(debug=True)