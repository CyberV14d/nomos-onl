import os
import uuid
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from mistralai.client import Mistral
from pinecone import Pinecone
from dotenv import load_dotenv

from io import BytesIO
import json

load_dotenv()

# --- PATH AND APP CONFIGURATION ---
basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = 'notion_style_secret_2026'
# Use absolute path for SQLite to ensure persistence on PythonAnywhere
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'workspace.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- API CLIENTS ---
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "WI1zRwkIonhv7wobRiJ9i601cbETjjVE")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY", "pcsk_3VZZi1_65uCB9XTUduFpem9ePikJbFQEXoZiZ76MrLwGFJWLbXifeKgXxLhpXL1CGvCzet")

mistral_client = Mistral(api_key=MISTRAL_API_KEY)
pc = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index("nomos-index")  # Must match the name in your Pinecone dashboard

# --- Database Models ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    projects = db.relationship('Project', backref='owner', lazy=True)

class Source(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    project_uuid = db.Column(db.String(50), unique=True, default=lambda: str(uuid.uuid4()).replace('-', '_'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    draft_content = db.Column(db.Text, default="")
    messages = db.relationship('Message', backref='project', lazy=True, cascade="all, delete-orphan")
    sources = db.relationship('Source', backref='project', lazy=True, cascade="all, delete-orphan")

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Vector Helper Function ---

def get_mistral_embedding(text):
    """Generates a 1024-dimension vector using Mistral for Pinecone storage."""
    res = mistral_client.embeddings.create(
        model="mistral-embed",
        inputs=[text]
    )
    return res.data[0].embedding

# --- Scraper ---

def scrape_url(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        wiki_body = soup.find(id="mw-content-text")
        text = wiki_body.get_text() if wiki_body else soup.get_text()
        return text[:8000].strip()
    except Exception as e:
        return f"SCRAPE_ERROR: {str(e)}"

# --- Authentication Routes ---

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if User.query.filter_by(email=email).first():
            return "Email exists. <a href='/login'>Login</a>"
        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(email=email, password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form.get('email')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            login_user(user)
            return redirect(url_for('dashboard'))
        return "Invalid login. <a href='/login'>Try again</a>"
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- Dashboard & Project Management ---

@app.route('/')
def root():
    return redirect(url_for('dashboard')) if current_user.is_authenticated else redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', projects=current_user.projects)

@app.route('/create_project', methods=['POST'])
@login_required
def create_project():
    name = request.form.get('name')
    if name:
        new_project = Project(name=name, user_id=current_user.id)
        db.session.add(new_project)
        db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/project/<p_uuid>')
@login_required
def view_project(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    return render_template('project.html', project=project, messages=project.messages)

# --- AI & Knowledge Management Routes ---

@app.route('/ingest/<p_uuid>', methods=['POST'])
@login_required
def ingest(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    url = request.json.get('url')
    raw_text = scrape_url(url)
    if "SCRAPE_ERROR" in raw_text:
        return jsonify({"error": "Failed to access page."}), 400
    try:
        response = mistral_client.chat.complete(
            model="mistral-medium-latest",
            messages=[{"role": "user", "content": f"Extract core facts from: {raw_text}"}]
        )
        clean_content = response.choices[0].message.content
        vector = get_mistral_embedding(clean_content)
        index.upsert(
            vectors=[{"id": str(uuid.uuid4()), "values": vector, "metadata": {"text": clean_content, "url": url}}],
            namespace=p_uuid
        )
        new_source = Source(url=url, project_id=project.id)
        db.session.add(new_source)
        db.session.commit()
        return jsonify({"status": "Source added!", "url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/chat/<p_uuid>', methods=['POST'])
@login_required
def chat(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    user_query = request.json.get('message')
    try:
        query_vec = get_mistral_embedding(user_query)
        search_res = index.query(namespace=p_uuid, vector=query_vec, top_k=3, include_metadata=True)
        context = "\n\n".join([m.metadata['text'] for m in search_res.matches]) if search_res.matches else "No context."
        response = mistral_client.chat.complete(
            model="mistral-medium-latest",
            messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {user_query}"}]
        )
        reply = response.choices[0].message.content
        db.session.add(Message(role='user', content=user_query, project_id=project.id))
        db.session.add(Message(role='ai', content=reply, project_id=project.id))
        db.session.commit()
        return jsonify({"reply": reply})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/graph/<p_uuid>')
@login_required
def get_graph_data(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    try:
        results = index.query(namespace=p_uuid, vector=[0.0]*1024, top_k=10, include_metadata=True)
        docs = [m.metadata['text'] for m in results.matches]

        if not docs:
            return jsonify({"nodes": [], "edges": []})

        all_text = " ".join(docs)[:7000]

        prompt = f"""
        Analyze the text below and extract a knowledge graph.
        Return ONLY a raw JSON object. Do not include any markdown formatting or extra text.
        Structure:
        {{
          "nodes": [ {{"id": "1", "label": "Name", "type": "Concept"}} ],
          "edges": [ {{"source": "1", "target": "2", "label": "verb"}} ]
        }}
        TEXT: {all_text}
        """

        response = mistral_client.chat.complete(
            model="mistral-medium-latest",
            messages=[{"role": "user", "content": prompt}]
        )

        raw_content = response.choices[0].message.content.strip()

        # CRITICAL FIX: Clean and Parse
        if "```" in raw_content:
            raw_content = raw_content.split("```")[1].replace("json", "").strip()

        # Parse into a dict to verify it's valid, then use jsonify
        graph_data = json.loads(raw_content)
        return jsonify(graph_data)

    except Exception as e:
        print(f"Graph Construction Error: {str(e)}")
        return jsonify({"nodes": [], "edges": []}), 500

# --- Writing Mode & Editor Routes ---

@app.route('/project/<p_uuid>/write')
@login_required
def writing_mode(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    return render_template('write.html', project=project)

@app.route('/project/<p_uuid>/save', methods=['POST'])
@login_required
def save_draft(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    project.draft_content = request.json.get('content', '')
    db.session.commit()
    return jsonify({"status": "Saved"})

@app.route('/project/<p_uuid>/new_page', methods=['POST'])
@login_required
def new_page(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    project.draft_content = ""
    db.session.commit()
    return jsonify({"status": "New page created"})

@app.route('/ai_assist/<p_uuid>', methods=['POST'])
@login_required
def ai_assist(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    instruction = request.json.get('instruction')
    current_text = request.json.get('content')
    try:
        query_vec = get_mistral_embedding(instruction)
        search_res = index.query(namespace=p_uuid, vector=query_vec, top_k=5, include_metadata=True)
        context = "\n\n".join([m.metadata['text'] for m in search_res.matches]) if search_res.matches else "No data."
        prompt = f"Using this research context:\n{context}\n\nHelp me with this draft:\n{current_text}\n\nInstruction: {instruction}\n\nReturn only the text to be added."
        response = mistral_client.chat.complete(
            model="mistral-medium-latest",
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify({"suggestion": response.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/project/<p_uuid>/export', methods=['GET'])
@login_required
def export_pdf(p_uuid):
    project = Project.query.filter_by(project_uuid=p_uuid, user_id=current_user.id).first_or_404()
    result = BytesIO()
    html = f"<h1>{project.name}</h1><hr>{project.draft_content}"
    pisa_status = pisa.CreatePDF(html, dest=result)
    if pisa_status.err:
        return "PDF Error", 500
    result.seek(0)
    return Response(result, mimetype='application/pdf',
                    headers={"Content-Disposition": f"attachment;filename={project.name}.pdf"})

# --- RUN BLOCK ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
