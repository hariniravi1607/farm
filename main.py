import os, re, json, hashlib, secrets
from datetime import datetime, timedelta
from collections import Counter
from markupsafe import Markup, escape
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.pool import NullPool
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
import httpx

# ---------- database ----------
engine = create_engine("sqlite:///farmnova.db", connect_args={"check_same_thread": False}, poolclass=NullPool)
SessionLocal = sessionmaker(engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(80)); email = Column(String(120), unique=True)
    pw = Column(String(200)); region = Column(String(60)); crop = Column(String(40))

class Report(Base):            # latest crop / disease results, shared across modules
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True); user_id = Column(Integer)
    kind = Column(String(20)); title = Column(String(80)); detail = Column(Text)
    at = Column(DateTime, default=datetime.utcnow)

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True); user_id = Column(Integer)
    text = Column(Text); at = Column(DateTime, default=datetime.utcnow)

class Follow(Base):
    __tablename__ = "follows"
    id = Column(Integer, primary_key=True); user_id = Column(Integer); target_id = Column(Integer)

class Like(Base):
    __tablename__ = "likes"
    id = Column(Integer, primary_key=True); post_id = Column(Integer); user_id = Column(Integer)

class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True); post_id = Column(Integer); user_id = Column(Integer)
    text = Column(Text); at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

def hash_pw(p, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", p.encode(), salt.encode(), 60000).hex()

def check_pw(p, stored):
    return hash_pw(p, stored.split("$")[0]) == stored

# ---------- reference data (replace with your own tables later) ----------
REGIONS = {"coimbatore": (11.02, 76.96), "madurai": (9.92, 78.12), "thanjavur": (10.79, 79.14),
           "salem": (11.66, 78.15), "chennai": (13.08, 80.27)}

CENTRES = {
 "coimbatore": [("Soil Testing Lab, Agricultural University", "Lawley Road", "0422-2431222", "Mon-Sat, 9am to 5pm", "Rs 100"),
                ("Green Earth Agro Lab", "RS Puram", "0422-2550141", "Mon-Sat, 9am to 6pm", "Rs 150"),
                ("Krishi Vigyan Kendra Soil Unit", "Thondamuthur", "0422-2611000", "Mon-Fri, 10am to 4pm", "Free")],
 "madurai": [("District Soil Testing Laboratory", "Race Course Road", "0452-2530110", "Mon-Fri, 9am to 5pm", "Free"),
             ("Vaigai Agro Labs", "Anna Nagar", "0452-2380199", "Mon-Sat, 9am to 6pm", "Rs 120")],
 "thanjavur": [("Delta Soil Testing Centre", "Medical College Road", "04362-231905", "Mon-Sat, 9am to 5pm", "Rs 80"),
               ("Cauvery Agri Lab", "Orathanadu", "04372-233011", "Mon-Fri, 10am to 5pm", "Rs 100")],
 "salem": [("Salem Soil Health Laboratory", "Fairlands", "0427-2318220", "Mon-Fri, 9am to 5pm", "Free"),
           ("Steel City Agro Labs", "Hasthampatti", "0427-2411890", "Mon-Sat, 9am to 6pm", "Rs 130")],
 "chennai": [("Soil Testing Laboratory, Guindy", "Guindy", "044-22350101", "Mon-Fri, 9am to 5pm", "Rs 100"),
             ("Metro Agri Testing", "Ambattur", "044-26580322", "Mon-Sat, 9am to 6pm", "Rs 160")],
}

SCHEMES = [
 ("PM-KISAN", "Rs 6,000 a year as income support, paid in three instalments.", "All crops", "Land records, Aadhaar, bank passbook"),
 ("PM Fasal Bima Yojana", "Low-premium insurance against crop loss from weather, pests and disease.", "Rice,Maize,Cotton,Groundnut,Banana", "Sowing certificate, land records"),
 ("Soil Health Card", "Free soil analysis with nutrient advice for every holding.", "All crops", "Land survey number"),
 ("Kisan Credit Card", "Short-term crop loans at a subsidised 4 percent interest.", "All crops", "Aadhaar, land documents, photo"),
 ("PM Krishi Sinchayee Yojana", "Subsidy of up to 55 percent on drip and sprinkler irrigation.", "Banana,Cotton,Groundnut,Maize", "Land records, quotation from supplier"),
 ("Paramparagat Krishi Vikas Yojana", "Support for clusters moving to organic farming.", "Rice,Banana,Maize", "Group of 20 farmers, land records"),
]

CROPS = {  # N, P, K, pH, temperature, humidity, rainfall  (fallback profile, used if no trained model is found)
 "Rice": (80, 48, 40, 6.5, 24, 82, 220), "Maize": (78, 48, 20, 6.3, 23, 65, 85),
 "Cotton": (118, 46, 20, 6.9, 24, 80, 80), "Banana": (100, 82, 50, 6.0, 27, 80, 105),
 "Chickpea": (40, 68, 80, 7.2, 19, 17, 80), "Coffee": (100, 28, 30, 6.8, 25, 58, 158),
 "Groundnut": (21, 47, 20, 6.7, 28, 85, 48), "Apple": (20, 134, 200, 6.0, 22, 92, 112)}
SCALE = (30, 40, 60, 1.2, 8, 25, 100)

DISEASES = [("Rice Blast", "Spray Tricyclazole 75 WP at 0.6 g per litre. Reduce urea and add potash (MOP) at 20 kg per acre."),
            ("Bacterial Leaf Blight", "Spray Copper Oxychloride at 3 g per litre. Switch to a balanced 19:19:19 NPK mix."),
            ("Brown Spot", "Spray Mancozeb at 2.5 g per litre and apply zinc sulphate at 10 kg per acre."),
            ("Powdery Mildew", "Spray wettable sulphur at 2 g per litre. Avoid watering the leaves from above.")]

# ---------- model hooks: plug your trained models in here ----------
def predict_crop(v):
    """v = [N, P, K, pH, temperature, humidity, rainfall]. Returns list of (crop, score 0-100)."""
    path = "models/crop_model.pkl"
    if os.path.exists(path):
        import joblib
        m = joblib.load(path)
        # CHANGE the column order below to match how YOU trained the model
        x = [[v[0], v[1], v[2], v[4], v[5], v[3], v[6]]]
        p = m.predict_proba(x)[0]
        top = sorted(zip(m.classes_, p), key=lambda t: -t[1])[:5]
        return [(str(c).title(), round(s * 100, 1)) for c, s in top]
    out = []
    for name, prof in CROPS.items():
        d = sum(((a - b) / s) ** 2 for a, b, s in zip(v, prof, SCALE)) ** 0.5
        out.append((name, round(100 / (1 + d), 1)))
    return sorted(out, key=lambda t: -t[1])[:5]

def predict_disease(img_bytes):
    """Load your ResNet-50 here (models/disease_model.*), preprocess img_bytes, return (name, confidence, advice)."""
    i = len(img_bytes) % len(DISEASES)
    return DISEASES[i][0], 88 + len(img_bytes) % 10, DISEASES[i][1]

# ---------- helpers used by the templates ----------
# Visual scale for the sliders and charts. Edit these to match the ranges your model was trained on.
RANGES = {"N": (0, 140), "P": (5, 145), "K": (5, 205), "pH": (3.5, 9.9), "temp": (8, 44), "hum": (14, 100), "rain": (20, 300)}
FIELDS = [("N", "Nitrogen", "kg/ha"), ("P", "Phosphorus", "kg/ha"), ("K", "Potassium", "kg/ha"), ("pH", "Soil pH", ""),
          ("temp", "Temperature", "C"), ("hum", "Humidity", "%"), ("rain", "Rainfall", "mm")]

def pct(k, val):
    lo, hi = RANGES[k]
    return max(0, min(100, round((float(val) - lo) / (hi - lo) * 100)))

def phnote(v):
    v = float(v)
    return ("strongly acidic" if v < 5.5 else "acidic" if v < 6.5 else "neutral" if v <= 7.5
            else "alkaline" if v <= 8.5 else "strongly alkaline")

def build_compare(crop, v):
    """Compare entered values with the reference profile of the crop (only for crops in CROPS)."""
    ref = CROPS.get(crop)
    if not ref: return None
    rows = []
    for i, (k, label, unit) in enumerate(FIELDS):
        d = abs(v[i] - ref[i]) / SCALE[i]
        rows.append(dict(key=k, label=label, unit=unit, you=v[i], ref=ref[i], you_pct=pct(k, v[i]), ref_pct=pct(k, ref[i]),
                         fit="Close" if d <= 1 else "Slightly off" if d <= 2 else "Far",
                         cls="ok" if d <= 1 else "mid" if d <= 2 else "far"))
    return rows

def tagify(t):
    return Markup(re.sub(r"#(\w+)", r'<span class="ht">#\1</span>', str(escape(t))).replace("\n", "<br>"))

def ago(d):
    s = int((datetime.utcnow() - d).total_seconds())
    if s < 60: return "just now"
    if s < 3600: return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h"
    return f"{s // 86400}d"

HEADLINES = {
 "ravi@farmnova.demo": "Banana and paddy grower, Thanjavur",
 "meena@farmnova.demo": "Rice farmer and soil health volunteer, Madurai",
 "suresh@farmnova.demo": "Agriculture Officer, Salem block office",
 "arun@farmnova.demo": "Cotton grower, Salem",
 "lakshmi@farmnova.demo": "Organic rice farmer, Thanjavur",
 "karthik@farmnova.demo": "Groundnut grower, Coimbatore"}

def headline(u):
    return HEADLINES.get(u.email) or f"{u.crop} grower in {u.region}"

def seed():
    """Adds a few demo farmers and posts the first time the app runs, so Farmer Connect is never empty."""
    db = SessionLocal()
    if db.query(User).filter(User.email.like("%@farmnova.demo")).count():
        db.close(); return
    people = [("Ravi Kumar", "ravi", "Banana", "Thanjavur"), ("Meena Sundaram", "meena", "Rice", "Madurai"),
              ("Dr. Suresh Babu", "suresh", "Maize", "Salem"), ("Arun Prakash", "arun", "Cotton", "Salem"),
              ("Lakshmi Devi", "lakshmi", "Rice", "Thanjavur"), ("Karthik Raja", "karthik", "Groundnut", "Coimbatore")]
    ids = {}
    for n, e, c, r in people:
        u = User(name=n, email=f"{e}@farmnova.demo", pw=hash_pw(secrets.token_hex(4)), region=r, crop=c)
        db.add(u); db.commit(); ids[e] = u.id
    posts = [
     ("karthik", "Rain is forecast this week, so I am postponing fertilizer application until the field dries out. #Weather #Groundnut", 3000),
     ("arun", "Which cotton variety is working well for black soil in your area? Planning my next season and would like to hear from people who have tried a few. #Cotton", 1700),
     ("lakshmi", "Spotted brown patches on a few rice leaves yesterday. Scanned a photo on FarmNova and followed the spray advice. Will post an update in a week. #RiceDisease #Rice", 900),
     ("suresh", "A reminder for all farmers in the block: Soil Health Card and PM-KISAN registration are free. Visit the nearest agriculture office with Aadhaar and land records. #Schemes #SoilHealth", 420),
     ("meena", "Got my soil tested at the district lab before sowing. The pH came back on the acidic side, so I applied lime as the lab advised. The test fee was small compared to what I would have lost on the wrong inputs. #SoilTesting #Rice", 200),
     ("ravi", "Switched to drip irrigation on my banana plot this season. The leaves look healthier and I am using less water. Happy to share how I set it up if anyone is interested. #DripIrrigation #Banana", 60)]
    pid = {}
    for e, t, m in posts:
        p = Post(user_id=ids[e], text=t, at=datetime.utcnow() - timedelta(minutes=m)); db.add(p); db.commit(); pid[e] = p.id
    for e, who in [("ravi", ["meena", "arun", "suresh", "lakshmi"]), ("meena", ["ravi", "lakshmi", "suresh"]), ("suresh", ["ravi", "meena", "arun", "karthik", "lakshmi"]),
                   ("lakshmi", ["meena", "ravi"]), ("arun", ["ravi"]), ("karthik", ["meena"])]:
        for w in who: db.add(Like(post_id=pid[e], user_id=ids[w]))
    for e, w, t in [("ravi", "meena", "Would love to see the layout. How many acres is the plot?"), ("ravi", "suresh", "Good move. PMKSY has a subsidy for drip systems, worth checking."),
                    ("arun", "karthik", "Try asking at your block office, they usually know which varieties suit the soil locally."),
                    ("lakshmi", "suresh", "Good that you caught it early. Keep the field drained and avoid extra urea for now."),
                    ("meena", "ravi", "Totally agree, testing first saves money.")]:
        db.add(Comment(post_id=pid[e], user_id=ids[w], text=t))
    for a, b in [("ravi", "meena"), ("ravi", "suresh"), ("meena", "ravi"), ("lakshmi", "ravi"), ("arun", "ravi"), ("karthik", "suresh")]:
        db.add(Follow(user_id=ids[a], target_id=ids[b]))
    db.commit(); db.close()

seed()

# ---------- app ----------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="farmnova-demo-key")

@app.middleware("http")
async def close_db(request: Request, call_next):
    try:
        return await call_next(request)
    finally:
        db = getattr(request.state, "db", None)
        if db is not None: db.close()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters.update(ago=ago, tagify=tagify)
templates.env.globals.update(pct=pct, phnote=phnote, headline=headline, ranges=RANGES, fields=FIELDS)

def get_db(request):
    """One database session per request, always closed by the middleware below."""
    db = getattr(request.state, "db", None)
    if db is None:
        db = request.state.db = SessionLocal()
    return db

def me(request):
    db = get_db(request)
    uid = request.session.get("uid")
    return (db.get(User, uid) if uid else None), db

def page(request, name, **ctx):
    u, db = me(request)
    ctx.update(user=u, active=name.split(".")[0])
    r = templates.TemplateResponse(request, name, ctx)
    if db: db.close()
    return r

def latest(db, uid, kind):
    return db.query(Report).filter_by(user_id=uid, kind=kind).order_by(Report.id.desc()).first()

@app.get("/")
def root(request: Request):
    if request.session.get("uid"): return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("e")})

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    # demo mode: any email and password is accepted; a new user is created on first login
    db = get_db(request); email = email.lower().strip()
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(name=email.split("@")[0].replace(".", " ").title(), email=email,
                 pw=hash_pw(password), region="Coimbatore", crop="Rice")
        db.add(u); db.commit()
    request.session["uid"] = u.id
    return RedirectResponse("/dashboard", 303)

@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db = get_db(request); email = email.lower().strip()
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(name=name, email=email, pw=hash_pw(password), region="Coimbatore", crop="Rice")
        db.add(u); db.commit()
    request.session["uid"] = u.id
    return RedirectResponse("/dashboard", 303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear(); return RedirectResponse("/")

@app.get("/dashboard")
def dashboard(request: Request):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    crop = latest(db, u.id, "crop"); dis = latest(db, u.id, "disease")
    posts = db.query(Post).order_by(Post.id.desc()).limit(3).all()
    names = {x.id: x.name for x in db.query(User).all()}
    matched = len([s for s in SCHEMES if s[2] == "All crops" or (crop.title if crop else u.crop) in s[2]])
    ctx = dict(crop=crop, dis=dis, soil=json.loads(crop.detail) if crop else None, posts=posts, names=names,
               matched=matched, scans=db.query(Report).filter_by(user_id=u.id, kind="disease").count(), coords=REGIONS.get(u.region.lower(), REGIONS["coimbatore"]))
    db.close(); return page(request, "dashboard.html", **ctx)

@app.get("/soil")
def soil(request: Request, region: str = ""):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    q = region.strip().lower() or u.region.lower()
    return page(request, "soil.html", q=q, found=CENTRES.get(q), regions=list(CENTRES))

@app.api_route("/crop", methods=["GET", "POST"])
async def crop(request: Request):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    result = vals = compare = None
    if request.method == "POST":
        f = await request.form()
        keys = ["N", "P", "K", "pH", "temp", "hum", "rain"]
        v = [float(f[k]) for k in keys]
        result = predict_crop(v); vals = dict(zip(keys, v))
        db.add(Report(user_id=u.id, kind="crop", title=result[0][0], detail=json.dumps(vals))); db.commit()
        compare = build_compare(result[0][0], v)
    return page(request, "crop.html", result=result, vals=vals, compare=compare)

@app.api_route("/disease", methods=["GET", "POST"])
async def disease(request: Request, image: UploadFile = File(None)):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    result = None
    if request.method == "POST" and image is not None:
        name, conf, advice = predict_disease(await image.read())
        db.add(Report(user_id=u.id, kind="disease", title=name, detail=advice)); db.commit()
        result = dict(name=name, conf=conf, advice=advice)
    return page(request, "disease.html", result=result)

@app.get("/schemes")
def schemes(request: Request):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    c = latest(db, u.id, "crop"); crop_name = c.title if c else u.crop
    rows = [(s, s[2] == "All crops" or crop_name in s[2]) for s in SCHEMES]
    return page(request, "schemes.html", rows=rows, crop_name=crop_name, from_module=bool(c))

@app.api_route("/connect", methods=["GET", "POST"])
async def connect(request: Request):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    if request.method == "POST":
        f = await request.form()
        if f.get("text", "").strip():
            db.add(Post(user_id=u.id, text=f["text"].strip())); db.commit()
        return RedirectResponse("/connect", 303)
    posts = db.query(Post).order_by(Post.id.desc()).limit(40).all()
    people = {x.id: x for x in db.query(User).all()}
    following = {f.target_id for f in db.query(Follow).filter_by(user_id=u.id)}
    likes, comments = {}, {}
    for l in db.query(Like).all(): likes.setdefault(l.post_id, set()).add(l.user_id)
    for c in db.query(Comment).order_by(Comment.id).all(): comments.setdefault(c.post_id, []).append(c)
    stats = dict(followers=db.query(Follow).filter_by(target_id=u.id).count(), following=len(following),
                 posts=db.query(Post).filter_by(user_id=u.id).count())
    tags = Counter(t.lower() for p in db.query(Post).all() for t in re.findall(r"#(\w+)", p.text)).most_common(5)
    groups = Counter(x.crop for x in people.values()).most_common(4)
    return page(request, "connect.html", posts=posts, people=people, following=following, likes=likes, comments=comments,
                stats=stats, tags=tags, groups=groups, prefill=request.query_params.get("share", ""),
                last_crop=latest(db, u.id, "crop"), last_dis=latest(db, u.id, "disease"))

@app.post("/follow/{tid}")
def follow(request: Request, tid: int):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    f = db.query(Follow).filter_by(user_id=u.id, target_id=tid).first()
    db.delete(f) if f else db.add(Follow(user_id=u.id, target_id=tid)); db.commit()
    return RedirectResponse("/connect", 303)

@app.post("/like/{pid}")
def like(request: Request, pid: int):
    u, db = me(request)
    if not u: return JSONResponse({}, 401)
    l = db.query(Like).filter_by(post_id=pid, user_id=u.id).first()
    db.delete(l) if l else db.add(Like(post_id=pid, user_id=u.id)); db.commit()
    return {"liked": not l, "count": db.query(Like).filter_by(post_id=pid).count()}

@app.post("/comment/{pid}")
def comment(request: Request, pid: int, text: str = Form(...)):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    if text.strip(): db.add(Comment(post_id=pid, user_id=u.id, text=text.strip())); db.commit()
    return RedirectResponse(f"/connect#p{pid}", 303)

@app.post("/repost/{pid}")
def repost(request: Request, pid: int):
    u, db = me(request)
    if not u: return RedirectResponse("/")
    p = db.get(Post, pid); a = db.get(User, p.user_id) if p else None
    if p: db.add(Post(user_id=u.id, text=f"Reposted from {a.name if a else 'a farmer'}: {p.text}")); db.commit()
    return RedirectResponse("/connect", 303)

@app.get("/nova")
def nova(request: Request):
    if not request.session.get("uid"): return RedirectResponse("/")
    return page(request, "nova.html", q=request.query_params.get("q", ""))

@app.post("/api/nova")
async def nova_api(request: Request):
    u, db = me(request)
    if not u: return JSONResponse({"reply": "Please log in first."}, 401)
    q = (await request.json())["message"]
    c, d = latest(db, u.id, "crop"), latest(db, u.id, "disease")
    key = os.getenv("GROQ_API_KEY")
    if key:
        system = ("You are Nova, a farming guide for young Indian farmers. Answer in simple English, "
                  "as a short checklist where each step starts with '- '. "
                  f"Farmer region: {u.region}. Main crop: {u.crop}. "
                  f"Recommended crop: {c.title if c else 'none yet'}. Last disease scan: {d.title if d else 'none'}.")
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "llama-3.1-8b-instant", "messages": [
                        {"role": "system", "content": system}, {"role": "user", "content": q}]})
            db.close(); return {"reply": r.json()["choices"][0]["message"]["content"]}
        except Exception:
            pass
    l = q.lower(); name = next((k for k in CROPS if k.lower() in l), None)
    if name and "suit" in l:
        p = CROPS[name]
        rep = (f"{name} can work in {u.region} if the soil and weather match.\n- Get your soil tested at a nearby centre\n"
               f"- Aim for about {p[6]} mm of rainfall and pH near {p[3]}\n- Enter the report in Crop Recommendation\n- Check the Schemes page")
    elif any(w in l for w in ("blast", "disease", "leaf", "spot")):
        rep = "- Upload a clear leaf photo in Disease Prediction\n- Remove badly infected plants\n- Spray the fungicide it suggests\n- Cut extra nitrogen for two weeks"
    elif any(w in l for w in ("scheme", "loan", "insurance", "subsid")):
        rep = "- Open the Schemes page to see what you qualify for\n- Keep Aadhaar, land records and bank passbook ready\n- Visit your nearest agriculture office to apply"
    else:
        rep = "I can help with crop choice, soil, diseases and schemes. Try: Is cotton suitable for my region?"
    db.close(); return {"reply": rep}
