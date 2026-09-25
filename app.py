import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, flash, redirect, url_for, session, send_file, jsonify
from flask_wtf import FlaskForm
from wtforms import DateField, IntegerField, SelectField, StringField, TextAreaField, TimeField
from wtforms.validators import DataRequired, Email, NumberRange, Optional
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
import google.generativeai as genai
from groq import Groq

# ------------------------------------------------------
# APP INITIALIZATION
# ------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Some local Windows/VS Code setups inject stale proxy variables that make
# the Groq SDK fail with WinError 10061. This app talks directly to Groq.
for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(proxy_var, None)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["WTF_CSRF_ENABLED"] = os.getenv("WTF_CSRF_ENABLED", "false").lower() == "true"

ESANJEEVANI_URL = "https://esanjeevani.mohfw.gov.in/#/"

MEDICAL_SYSTEM_PROMPT = """
You are Sanjeevani, a careful AI health assistant. Give educational guidance,
not a final diagnosis. Be warm, practical, and concise. Always mention urgent
red flags when relevant, advise consulting a qualified clinician, and avoid
prescribing exact dosages or claiming certainty.
"""

# ------------------------------------------------------
# AI CLIENTS (GEMINI PREFERRED, GROQ FALLBACK)
# ------------------------------------------------------
# Keep secrets in environment variables for deployment.
DIRECT_GEMINI_API_KEY = ""
DIRECT_GROQ_API_KEY = ""

ENV_GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = DIRECT_GEMINI_API_KEY or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GROQ_API_KEY = DIRECT_GROQ_API_KEY or ENV_GROQ_API_KEY

if not GEMINI_API_KEY and ENV_GROQ_API_KEY and ENV_GROQ_API_KEY.startswith("AIza"):
    GEMINI_API_KEY = ENV_GROQ_API_KEY
    GROQ_API_KEY = None

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


def provider_name():
    if GEMINI_API_KEY:
        return "Gemini"
    if client is not None:
        return "Groq"
    return None


def chat_messages_to_prompt(messages):
    lines = []
    for message in messages:
        role = message.get("role", "user").title()
        content = message.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def generate_ai_content(prompt=None, messages=None, temperature=0.3, max_tokens=700):
    if GEMINI_API_KEY:
        content = chat_messages_to_prompt(messages) if messages else f"System: {MEDICAL_SYSTEM_PROMPT}\n\nUser: {prompt}"
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(
            content,
            generation_config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
        )
        reply = getattr(response, "text", None)
        if not reply:
            raise RuntimeError("Gemini returned an empty response. Check the model name and API key permissions.")
        return reply

    if client is not None:
        groq_messages = messages or [
            {"role": "system", "content": MEDICAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=groq_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    raise RuntimeError("AI API key is not configured. Add GEMINI_API_KEY to your .env file and restart the app.")

# ------------------------------------------------------
# MEDICAL FORM
# ------------------------------------------------------
class SymptomForm(FlaskForm):
    name = StringField("Patient name", validators=[DataRequired()])
    age = IntegerField("Age", validators=[DataRequired(), NumberRange(min=0, max=120)])
    gender = SelectField(
        "Gender",
        choices=[
            ("", "Select"),
            ("Female", "Female"),
            ("Male", "Male"),
            ("Non-binary", "Non-binary"),
            ("Prefer not to say", "Prefer not to say"),
        ],
        validators=[DataRequired()],
    )
    symptoms = TextAreaField("Main symptoms", validators=[DataRequired()])
    start = StringField("When did symptoms start?", validators=[DataRequired()])
    changes = TextAreaField("How have they changed?", validators=[DataRequired()])
    factors = TextAreaField("What improves or worsens them?", validators=[DataRequired()])
    medications = TextAreaField("Current medicines or conditions", validators=[DataRequired()])


class MedicineForm(FlaskForm):
    medicine_name = StringField("Medicine name", validators=[DataRequired()])
    age = IntegerField("Patient age", validators=[DataRequired(), NumberRange(min=0, max=120)])
    taking_for = StringField("Taking it for", validators=[DataRequired()])
    current_medicines = TextAreaField("Other medicines or supplements", validators=[Optional()])
    conditions = TextAreaField("Known conditions or allergies", validators=[Optional()])
    question = TextAreaField("What do you want to know?", validators=[DataRequired()])


class ConsultationForm(FlaskForm):
    patient_name = StringField("Patient name", validators=[DataRequired()])
    email = StringField("Email", validators=[DataRequired(), Email()])
    phone = StringField("Phone number", validators=[DataRequired()])
    department = SelectField(
        "Department",
        choices=[
            ("General Medicine", "General Medicine"),
            ("Pediatrics", "Pediatrics"),
            ("Cardiology", "Cardiology"),
            ("Dermatology", "Dermatology"),
            ("Mental Health", "Mental Health"),
            ("Gynecology", "Gynecology"),
        ],
        validators=[DataRequired()],
    )
    visit_type = SelectField(
        "Consultation type",
        choices=[
            ("Video consultation", "Video consultation"),
            ("Phone consultation", "Phone consultation"),
            ("Chat follow-up", "Chat follow-up"),
        ],
        validators=[DataRequired()],
    )
    preferred_date = DateField("Preferred date", validators=[DataRequired()])
    preferred_time = TimeField("Preferred time", validators=[DataRequired()])
    concern = TextAreaField("Main concern", validators=[DataRequired()])

# ------------------------------------------------------
# AI MEDICAL REPORT GENERATOR
# ------------------------------------------------------
def generate_report(name, age, gender, symptoms, start, changes, factors, medications):
    prompt = f"""
Create a professional medical intake report in clean Markdown.

Formatting rules:
- Start with: # Medical Intake Report
- Use level-2 headings for each section.
- Use short paragraphs and bullet lists.
- Keep the tone clinical, clear, and cautious.
- Do not claim a confirmed diagnosis.
- For urgent symptoms, make the Emergency Red Flags section prominent.

Sections:
## Patient Snapshot
## Symptom Summary
## Possible Causes To Discuss With A Clinician
## Questions A Doctor May Ask
## Suggested Tests Or Measurements
## Care Steps And Monitoring
## Emergency Red Flags
## Plain-language Disclaimer

Patient Details:
Name: {name}
Age: {age}
Gender: {gender}
Symptoms: {symptoms}
Started: {start}
Changes: {changes}
Factors: {factors}
Medications: {medications}
"""
    return generate_ai_content(
        messages=[
            {"role": "system", "content": MEDICAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
        max_tokens=1200,
    )


def normalize_markdown(text):
    text = str(text or "").replace("\r\n", "\n")
    text = re.sub(r"^\*\*(.*?)\*\*\s*$", r"## \1", text, flags=re.MULTILINE)
    text = re.sub(r"^\*\*(\d+\.\s+.*?)\*\*\s*$", r"## \1", text, flags=re.MULTILINE)
    text = re.sub(r"^(\d+\.\s+[A-Z][^\n:]{3,})\s*$", r"## \1", text, flags=re.MULTILINE)
    return text.strip()


def markdown_blocks(text):
    blocks = []
    current = {"title": None, "lines": []}

    for raw_line in normalize_markdown(text).splitlines():
        line = raw_line.strip()
        if not line:
            if current["lines"] and current["lines"][-1] != "":
                current["lines"].append("")
            continue

        heading = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading:
            if current["title"] or current["lines"]:
                blocks.append(current)
            current = {"title": heading.group(1).strip("* "), "lines": []}
            continue

        current["lines"].append(line)

    if current["title"] or current["lines"]:
        blocks.append(current)

    return blocks


def generate_medicine_guidance(medicine_name, age, taking_for, current_medicines, conditions, question):
    prompt = f"""
Create patient-friendly medicine guidance in clean Markdown.

Formatting rules:
- Start with: # Medicine Guidance
- Use level-2 headings for each section.
- Use bullets for safety points.
- Keep it concise, professional, and easy to scan.
- Do not say the medicine will cure the underlying condition.
- Do not provide exact dose instructions unless the user provided a clinician-prescribed dose.
- If the user mentions Dolo, explain that Dolo commonly contains paracetamol and warn against accidentally doubling paracetamol-containing medicines.

Sections:
## Common Use
## Important Checks Before Taking
## Possible Side Effects
## Interaction And Allergy Cautions
## When To Seek Urgent Help
## Safe-use Reminders
## Bottom Line

Medicine: {medicine_name}
Age: {age}
Taking it for: {taking_for}
Other medicines/supplements: {current_medicines or "Not provided"}
Conditions/allergies: {conditions or "Not provided"}
Question: {question}
"""
    return generate_ai_content(
        messages=[
            {"role": "system", "content": MEDICAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
        max_tokens=900,
    )


def build_consultation_summary(booking):
    prompt = f"""
Create a concise pre-consultation summary for a doctor. Include:
1. Patient and appointment details
2. Main concern
3. Suggested questions for the doctor
4. What the patient should prepare before the call
5. Emergency warning note if symptoms sound urgent

Booking:
{booking}
"""
    if provider_name() is None:
        return "Consultation booked. Prepare your recent reports, medicine list, allergies, and a clear symptom timeline before the call."

    return generate_ai_content(
        messages=[
            {"role": "system", "content": MEDICAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
        max_tokens=700,
    )

# ------------------------------------------------------
# PDF GENERATION
# ------------------------------------------------------
def create_report_pdf(name, age, gender, symptoms, start, changes, factors, medications, diagnosis):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)

    width, height = letter
    margin = 48
    y = height - 54
    line_height = 14
    page = 1

    def footer():
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(colors.HexColor("#6b7f79"))
        pdf.drawString(margin, 28, "Sanjeevani AI report - educational support only, not a final diagnosis.")
        pdf.drawRightString(width - margin, 28, f"Page {page}")

    def new_page():
        nonlocal y, page
        footer()
        pdf.showPage()
        page += 1
        y = height - 54

    def ensure_space(amount):
        if y - amount < 54:
            new_page()

    def write(text, x, font="Helvetica", size=10.5, color=colors.HexColor("#24313f"), leading=None):
        nonlocal y
        leading = leading or size + 4
        pdf.setFont(font, size)
        pdf.setFillColor(color)
        clean = re.sub(r"\*\*(.*?)\*\*", r"\1", str(text)).strip()
        lines = simpleSplit(clean, font, size, width - margin - x)
        for line in lines:
            ensure_space(leading)
            pdf.drawString(x, y, line)
            y -= leading

    def pill(label, value, x, y_pos, box_width):
        pdf.setFillColor(colors.HexColor("#f4fbf8"))
        pdf.setStrokeColor(colors.HexColor("#d8ebe5"))
        pdf.roundRect(x, y_pos - 52, box_width, 48, 10, fill=True, stroke=True)
        pdf.setFillColor(colors.HexColor("#0f8f72"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x + 12, y_pos - 20, str(label).upper())
        pdf.setFillColor(colors.HexColor("#173f38"))
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(x + 12, y_pos - 38, simpleSplit(str(value), "Helvetica-Bold", 11, box_width - 24)[0])

    pdf.setTitle("Sanjeevani AI Health Report")
    pdf.setFillColor(colors.HexColor("#075f50"))
    pdf.rect(0, height - 110, width, 110, fill=True, stroke=False)
    pdf.setFillColor(colors.HexColor("#0f8f72"))
    pdf.circle(width - 72, height - 55, 48, fill=True, stroke=False)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(margin, height - 45, "Sanjeevani AI Health Report")
    pdf.setFont("Helvetica", 9.5)
    pdf.setFillColor(colors.HexColor("#d9fff7"))
    pdf.drawString(margin, height - 66, f"Generated {datetime.now().strftime('%d %b %Y, %I:%M %p')}")
    pdf.drawString(margin, height - 84, "Prepared for clinical discussion. Review with a qualified clinician.")
    y = height - 136

    card_width = (width - (margin * 2) - 16) / 3
    pill("Patient", name, margin, y, card_width)
    pill("Age", age, margin + card_width + 8, y, card_width)
    pill("Gender", gender, margin + (card_width + 8) * 2, y, card_width)
    y -= 76

    pdf.setFillColor(colors.HexColor("#ffffff"))
    pdf.setStrokeColor(colors.HexColor("#d8ebe5"))
    pdf.roundRect(margin, y - 112, width - margin * 2, 104, 12, fill=True, stroke=True)
    y -= 26
    write(f"Primary symptoms: {symptoms}", margin + 16, "Helvetica-Bold", 10.5, colors.HexColor("#173f38"))
    write(f"Started: {start}", margin + 16, "Helvetica", 10, colors.HexColor("#405c56"))
    write(f"Changes: {changes}", margin + 16, "Helvetica", 10, colors.HexColor("#405c56"))
    write(f"Triggers / relief: {factors}", margin + 16, "Helvetica", 10, colors.HexColor("#405c56"))
    write(f"Medicines / conditions: {medications}", margin + 16, "Helvetica", 10, colors.HexColor("#405c56"))
    y -= 28

    for block in markdown_blocks(diagnosis):
        title = block["title"]
        lines = [line for line in block["lines"] if line != ""]
        if title and "Medical Intake Report" in title:
            continue
        if title:
            ensure_space(34)
            pdf.setFillColor(colors.HexColor("#e9f8f2"))
            pdf.setStrokeColor(colors.HexColor("#c9f1e2"))
            pdf.roundRect(margin, y - 26, width - margin * 2, 24, 8, fill=True, stroke=True)
            pdf.setFillColor(colors.HexColor("#075f50"))
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(margin + 12, y - 18, title)
            y -= 38

        for line in lines:
            if line.startswith("- "):
                write(f"• {line[2:]}", margin + 14, "Helvetica", 10, colors.HexColor("#24313f"))
            else:
                write(line, margin, "Helvetica", 10, colors.HexColor("#24313f"))
            y -= 2
        y -= 8

    footer()

    pdf.save()
    buffer.seek(0)
    return buffer

# ------------------------------------------------------
# STATIC ROUTES
# ------------------------------------------------------
@app.route("/")
def load():
    return render_template("index.html")

@app.route("/home")
def home():
    return redirect(url_for("chat_page"))

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/telemedicine")
def telemedicine():
    bookings = session.get("consultations", [])
    return render_template("telemedicine.html", bookings=bookings, esanjeevani_url=ESANJEEVANI_URL)

@app.route("/medicines", methods=["GET", "POST"])
def medicines():
    form = MedicineForm()
    guidance = None

    if form.validate_on_submit():
        try:
            guidance = generate_medicine_guidance(
                form.medicine_name.data,
                form.age.data,
                form.taking_for.data,
                form.current_medicines.data,
                form.conditions.data,
                form.question.data,
            )
        except Exception as exc:
            flash(str(exc), "error")

    return render_template("medicines.html", form=form, guidance=guidance)

@app.route("/consultation", methods=["GET", "POST"])
def consultation():
    form = ConsultationForm()
    booking = None
    summary = None

    if request.method == "POST" and not form.validate():
        flash("Please complete all required consultation fields correctly.", "error")

    if form.validate_on_submit():
        booking = {
            "patient_name": form.patient_name.data,
            "email": form.email.data,
            "phone": form.phone.data,
            "department": form.department.data,
            "visit_type": form.visit_type.data,
            "preferred_date": form.preferred_date.data.strftime("%d %b %Y"),
            "preferred_time": form.preferred_time.data.strftime("%I:%M %p"),
            "concern": form.concern.data,
            "status": "Requested",
            "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        }
        summary = build_consultation_summary(booking)
        bookings = session.get("consultations", [])
        bookings.insert(0, booking)
        session["consultations"] = bookings[:5]
        session["consultation_summary"] = summary
        flash("Consultation request created. This demo stores it in your browser session.", "success")

    return render_template(
        "consultation.html",
        form=form,
        booking=booking,
        summary=summary,
        esanjeevani_url=ESANJEEVANI_URL,
    )

@app.route("/esanjeevani")
def esanjeevani_redirect():
    return redirect(ESANJEEVANI_URL)

# ------------------------------------------------------
# MEDICAL FORM ROUTE
# ------------------------------------------------------
@app.route("/form", methods=["GET", "POST"])
def llama_form():
    form = SymptomForm()
    if form.validate_on_submit():

        patient = {
            "name": form.name.data,
            "age": form.age.data,
            "gender": form.gender.data,
            "symptoms": form.symptoms.data,
            "start": form.start.data,
            "changes": form.changes.data,
            "factors": form.factors.data,
            "medications": form.medications.data
        }

        try:
            diagnosis = generate_report(**patient)
        except Exception as exc:
            flash(str(exc), "error")
            return render_template("form.html", form=form), 503

        session["patient_data"] = patient
        session["diagnosis"] = diagnosis

        return redirect(url_for("report_preview"))

    return render_template("form.html", form=form)

@app.route("/report")
def report_preview():
    patient = session.get("patient_data")
    diagnosis = session.get("diagnosis")

    if not patient or not diagnosis:
        flash("Please complete the report form first.", "error")
        return redirect(url_for("llama_form"))

    return render_template("report.html", patient=patient, diagnosis=diagnosis)

# ------------------------------------------------------
# GENERATE PDF
# ------------------------------------------------------
@app.route("/generate-pdf")
def generate_pdf():
    patient = session.get("patient_data")
    diagnosis = session.get("diagnosis")

    if not patient or not diagnosis:
        flash("Please fill the form first.", "error")
        return redirect(url_for("llama_form"))

    pdf_buffer = create_report_pdf(
        patient["name"], patient["age"], patient["gender"],
        patient["symptoms"], patient["start"], patient["changes"],
        patient["factors"], patient["medications"], diagnosis
    )

    filename = f"sanjeevani-report-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(pdf_buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")

# ------------------------------------------------------
# CHAT PAGE
# ------------------------------------------------------
@app.route("/chat")
def chat_page():
    return render_template("index.html", data=session.get("data", []))

@app.route("/clear-chat", methods=["POST"])
def clear_chat():
    session.pop("data", None)
    return jsonify({"ok": True})

# ------------------------------------------------------
# CHAT API — FIXED VERSION (NO 500 ERRORS)
# ------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    payload = request.get_json(silent=True)

    if not payload or "text" not in payload:
        return jsonify({"error": "Invalid request"}), 400

    text = payload["text"].strip()

    if not text:
        return jsonify({"error": "Empty message"}), 400

    try:
        history = session.get("data", [])[-6:]
        messages = [{"role": "system", "content": MEDICAL_SYSTEM_PROMPT}]
        for entry in history:
            messages.append({"role": "user", "content": entry.get("input", "")})
            messages.append({"role": "assistant", "content": entry.get("result", "")})
        messages.append({"role": "user", "content": text})

        if provider_name() is None:
            return jsonify({"error": "AI API key is not configured. Add GEMINI_API_KEY to a .env file and restart the app."}), 503

        reply = generate_ai_content(
            messages=messages,
            temperature=0.3,
            max_tokens=650,
        )

    except Exception as e:
        print(f"ERROR FROM {provider_name() or 'AI PROVIDER'}:", e)
        return jsonify({"error": f"{provider_name() or 'AI'} API failed: {e}"}), 500

    history.append({"input": text, "result": reply})
    session["data"] = history[-20:]

    return jsonify({"result": reply})

# ------------------------------------------------------
# RUN APP
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
