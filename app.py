import os
import io
import re
import smtplib
from email.message import EmailMessage
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    jsonify,
)
from groq import Groq
from werkzeug.utils import secure_filename

# --- NEW IMPORTS ---
import docx  # for .docx parsing
import PyPDF2  # for .pdf parsing

# ---- Configuration ----
UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"txt", "pdf", "docx"}  # now support txt, pdf, docx

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.secret_key = os.environ.get("FLASK_SECRET", "super-secret-key")

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Use GROQ_API_KEY from environment
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# SMTP config from environment
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USERNAME or "no-reply@example.com")

# Basic email regex
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_file(filepath: str) -> str:
    """Extract text depending on file extension."""
    ext = filepath.rsplit(".", 1)[1].lower()

    if ext == "txt":
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    elif ext == "pdf":
        text = []
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                try:
                    text.append(page.extract_text() or "")
                except Exception:
                    continue
        return "\n".join(text)

    elif ext == "docx":
        doc = docx.Document(filepath)
        return "\n".join([para.text for para in doc.paragraphs])

    return ""


def parse_recipients(raw: str):
    if not raw:
        return []
    parts = re.split(r"[,\n;\r]+", raw)
    emails = [p.strip() for p in parts if p.strip()]
    valid = [e for e in emails if EMAIL_REGEX.match(e)]
    return valid


def send_email_smtp(subject: str, html_body: str, recipients: list):
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD:
        raise RuntimeError("SMTP not configured. Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD.")

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content("This email contains an HTML summary. View with an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")

    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)

    try:
        server.ehlo()
        if SMTP_PORT in (587, 25) and not isinstance(server, smtplib.SMTP_SSL):
            server.starttls()
            server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        server.quit()


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    prompt = request.form.get("prompt", "").strip()

    transcript = ""
    file = request.files.get("transcript_file")
    if file and file.filename != "":
        filename = secure_filename(file.filename)
        if not allowed_file(filename):
            flash("Only .txt, .pdf, and .docx files are supported.")
            return redirect(url_for("index"))
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(save_path)
        transcript = extract_text_from_file(save_path)
    else:
        transcript = request.form.get("transcript_text", "").strip()

    if not transcript:
        flash("Please upload or paste a transcript.")
        return redirect(url_for("index"))

    system_message = {
        "role": "system",
        "content": "You are a concise, accurate meeting summarizer. Output clear, structured summaries.",
    }

    user_message = {"role": "user", "content": f"Instruction: {prompt}\n\nTranscript:\n{transcript}"}

    summary = ""
    if groq_client:
        try:
            response = groq_client.chat.completions.create(
                messages=[system_message, user_message],
                model="llama3-8b-8192",
                temperature=0.0,
                max_completion_tokens=800,
            )
            summary = response.choices[0].message.content
        except Exception as e:
            flash(f"Error generating summary: {e}")
            return redirect(url_for("index"))
    else:
        lines = [l.strip() for l in transcript.splitlines() if l.strip()]
        summary = "Stub summary (Groq not configured):\n" + "\n".join(lines[:10])

    return render_template("result.html", summary=summary, prompt=prompt, transcript=transcript)


@app.route("/download", methods=["POST"])
def download():
    edited_summary = request.form.get("edited_summary", "")
    if not edited_summary:
        flash("No summary to download.")
        return redirect(url_for("index"))

    buf = io.BytesIO()
    buf.write(edited_summary.encode("utf-8"))
    buf.seek(0)

    return send_file(buf, as_attachment=True, download_name="meeting_summary.txt", mimetype="text/plain")


@app.route("/send_email", methods=["POST"])
def send_email():
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    recipients = parse_recipients(data.get("recipients", ""))
    if not recipients:
        msg = "No valid recipient emails."
        if request.is_json:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg)
        return redirect(url_for("index"))

    subject = data.get("subject") or "Meeting Summary"
    edited_text = data.get("edited_summary") or ""
    edited_html = data.get("edited_summary_html") or None
    prompt = data.get("prompt") or ""
    transcript = data.get("transcript") or ""

    body_html = f"<pre style='white-space:pre-wrap'>{edited_html or edited_text}</pre>"
    if prompt:
        body_html = f"<p><strong>Prompt used:</strong> {prompt}</p>" + body_html
    if transcript:
        body_html += "<hr/><details><summary>Original Transcript</summary>"
        body_html += f"<pre style='white-space:pre-wrap'>{transcript}</pre></details>"

    try:
        send_email_smtp(subject, body_html, recipients)
    except Exception as e:
        msg = f"Failed to send email: {e}"
        if request.is_json:
            return jsonify({"ok": False, "error": msg}), 500
        flash(msg)
        return redirect(url_for("index"))

    success = f"Summary sent to: {', '.join(recipients)}"
    if request.is_json:
        return jsonify({"ok": True, "message": success})
    flash(success)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
