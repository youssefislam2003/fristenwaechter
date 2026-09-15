from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
import uuid
import os

os.environ['DEMO_MODE'] = 'true'

app = FastAPI(title="Fristenwächter - DEMO MODE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_EMAIL = "test@test.com"
DEMO_PASSWORD = "test123"
VALID_SESSION = "demo-valid-session"

@app.get("/health")
async def health():
    return {"status": "ok", "mode": "demo"}

@app.get("/")
async def root():
    return RedirectResponse(url="/login")

@app.get("/login")
async def login_page():
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Anmelden — Fristenwächter</title>
        <style>
            body {{ font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
            .wrapper {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .card {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }}
            h1 {{ margin-top: 0; color: #333; }}
            .demo-box {{ background: #c8e6c9; padding: 15px; border-left: 4px solid #4caf50; margin: 20px 0; border-radius: 4px; }}
            .demo-box strong {{ color: #2e7d32; display: block; margin-bottom: 10px; }}
            .credential {{ margin: 5px 0; font-family: monospace; background: white; padding: 8px; border-radius: 3px; }}
            form {{ margin-top: 20px; }}
            label {{ display: block; margin: 15px 0 5px; font-weight: 500; color: #333; }}
            input {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 16px; box-sizing: border-box; }}
            button {{ width: 100%; padding: 12px; background: #2196F3; color: white; border: none; border-radius: 4px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 20px; }}
            button:hover {{ background: #1976D2; }}
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="card">
                <h1>Anmelden</h1>
                <div class="demo-box">
                    <strong>✓ Demo Mode</strong>
                    Use these credentials to log in:
                    <div class="credential">Email: {DEMO_EMAIL}</div>
                    <div class="credential">Password: {DEMO_PASSWORD}</div>
                </div>
                <form method="post" action="/login">
                    <label>E-Mail</label>
                    <input type="email" name="email" required autofocus>
                    <label>Passwort</label>
                    <input type="password" name="password" required>
                    <button type="submit">Anmelden</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """)

@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")

    if email == DEMO_EMAIL and password == DEMO_PASSWORD:
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie("session", VALID_SESSION, httponly=True, max_age=86400)
        return response

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Anmelden — Fristenwächter</title>
        <style>
            body {{ font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
            .wrapper {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .card {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px; }}
            h1 {{ margin-top: 0; color: #333; }}
            .error {{ background: #ffebee; color: #c62828; padding: 12px; border-left: 4px solid #f44336; margin: 15px 0; border-radius: 4px; }}
            .demo-box {{ background: #c8e6c9; padding: 15px; border-left: 4px solid #4caf50; margin: 20px 0; border-radius: 4px; }}
            .demo-box strong {{ color: #2e7d32; display: block; margin-bottom: 10px; }}
            .credential {{ margin: 5px 0; font-family: monospace; background: white; padding: 8px; border-radius: 3px; }}
            form {{ margin-top: 20px; }}
            label {{ display: block; margin: 15px 0 5px; font-weight: 500; color: #333; }}
            input {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 16px; box-sizing: border-box; }}
            button {{ width: 100%; padding: 12px; background: #2196F3; color: white; border: none; border-radius: 4px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 20px; }}
            button:hover {{ background: #1976D2; }}
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="card">
                <h1>Anmelden</h1>
                <div class="error">Email or password is incorrect</div>
                <div class="demo-box">
                    <strong>✓ Demo Mode</strong>
                    Use these credentials:
                    <div class="credential">Email: {DEMO_EMAIL}</div>
                    <div class="credential">Password: {DEMO_PASSWORD}</div>
                </div>
                <form method="post" action="/login">
                    <label>E-Mail</label>
                    <input type="email" name="email" value="{email}" required autofocus>
                    <label>Passwort</label>
                    <input type="password" name="password" required>
                    <button type="submit">Anmelden</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """, status_code=401)

@app.get("/dashboard")
async def dashboard(request: Request):
    session = request.cookies.get("session")
    if session != VALID_SESSION:
        return RedirectResponse(url="/login")

    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dashboard — Fristenwächter</title>
        <style>
            body { font-family: system-ui; margin: 0; padding: 0; background: #f5f5f5; }
            .header { background: white; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); display: flex; justify-content: space-between; align-items: center; }
            h1 { margin: 0; color: #333; }
            .logout { background: #f44336; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
            .logout:hover { background: #d32f2f; }
            .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }
            .success-banner { background: #4caf50; color: white; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
            .success-banner strong { font-size: 18px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
            .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .card h2 { margin-top: 0; color: #2196F3; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Fristenwächter Dashboard</h1>
            <a href="/logout" class="logout">Logout</a>
        </div>
        <div class="container">
            <div class="success-banner">
                <strong>✓ You are logged in (Demo Mode)</strong><br>
                This is a preview environment with demo credentials.
            </div>
            <div class="grid">
                <div class="card">
                    <h2>Fahrzeuge</h2>
                    <p>Manage your fleet vehicles and inspection schedules.</p>
                </div>
                <div class="card">
                    <h2>Fahrer</h2>
                    <p>Track driver information and license validations.</p>
                </div>
                <div class="card">
                    <h2>Prüfungen</h2>
                    <p>View compliance checks and audit history.</p>
                </div>
                <div class="card">
                    <h2>Benachrichtigungen</h2>
                    <p>Configure alerts and notification preferences.</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session")
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
