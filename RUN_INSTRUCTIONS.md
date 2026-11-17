# How to Run the Vehicle Reservation System

Complete step-by-step instructions to run the CityPark Vehicle Reservation System.

---

## Prerequisites

- Python 3.10 or higher
- Windows 10/11 (or Linux/Mac with minor command adjustments)
- Git (optional, for version control)

---

## Quick Start (3 Steps)

### Step 1: Activate Virtual Environment

```bash
# Windows (PowerShell or CMD)
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate
```

You should see `(.venv)` in your terminal prompt.

### Step 2: Start MCP Server (Terminal 1)

```bash
python start_mcp_server.py
```

**Expected Output:**
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8001
```

**Leave this terminal running!**

### Step 3: Start Web Server (Terminal 2 - New Terminal)

```bash
# Activate venv in new terminal first
.venv\Scripts\activate

# Start web server
uvicorn app.web.server:app --reload
```

**Expected Output:**
```
INFO:     Will watch for changes in these directories: ['C:\\Users\\...\\vehicle-reservation']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Chatbot ready
INFO:     Application startup complete.
```

**✅ System is now running!**

---

## Access the System

### User Interface (Web Browser)

**Chat Interface:**
1. Open browser: http://127.0.0.1:8000
2. Start chatting with the bot!

**API Endpoint:**
```bash
POST http://127.0.0.1:8000/chat
{
  "message": "What are your opening hours?",
  "session_id": "your-session-id"
}
```

### Admin Interface

**Web Dashboard:**
1. Open browser: http://127.0.0.1:8000/admin
2. View pending reservations
3. Approve or refuse bookings

**Admin CLI (Terminal 3 - New Terminal):**
```bash
# Activate venv first
.venv\Scripts\activate

# Run admin CLI
python -m app.admin.cli
```

**Expected Output:**
```
CityPark Administrator CLI
Backend: llm=offline, db=citypark.db

2 reservation(s) awaiting approval:

[1] #13 — Sathwik Balu
    Plate: TS21DS0000
    From:  2026-11-21T09:00
    To:    2026-11-21T19:00
    Created: 2026-11-12T13:57:35+00:00

[2] #14 — John Smith
    Plate: ABC123
    From:  2026-11-25T09:00
    To:    2026-11-25T17:00
    Created: 2026-11-17T09:56:32+00:00

Enter number to decide (or 'q' to quit):
```

---

## Complete Setup (If Starting Fresh)

### 1. Clone/Navigate to Project

```bash
cd C:\Users\DasariDivyaBindu\vehicle-reservation
```

### 2. Create Virtual Environment (if not exists)

```bash
python -m venv .venv
```

### 3. Activate Virtual Environment

```bash
# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure Environment

**Copy example config:**
```bash
# Windows
copy .env.example .env

# Linux/Mac
cp .env.example .env
```

**Edit .env file:**
```env
# Choose provider: "dial" or "openai"
LLM_PROVIDER=dial

# For offline mode (testing), leave API keys empty
DIAL_API_KEY=

# For EPAM DIAL (if you have access)
DIAL_API_KEY=your-dial-api-key-here
DIAL_ENDPOINT=https://ai-proxy.lab.epam.com

# For OpenAI (if you prefer)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-openai-key-here

# Admin token (required for admin API)
ADMIN_TOKEN=your-secure-random-token-here

# MCP server token (required for persistence)
MCP_SERVER_TOKEN=your-mcp-secure-token-here
```

### 6. Initialize Database (Optional - auto-creates on first run)

```bash
python -c "from app.chatbot import Chatbot; bot = Chatbot(); print('Database initialized!')"
```

### 7. Run Tests (Optional)

```bash
pytest -v
```

**Expected:** All 111 tests should pass.

---

## Testing the System

### Test 1: Simple Question

**Using curl (Terminal):**
```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"What are your opening hours?\", \"session_id\": \"test-1\"}"
```

**Using browser:** Visit http://127.0.0.1:8000 and type:
```
What are your opening hours?
```

**Expected Response:** Information about opening hours.

### Test 2: Location Question (Phase 2 Fix)

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Where are you located?\", \"session_id\": \"test-1\"}"
```

**Expected Response:** "CityPark is located at 15 Market Street..."

### Test 3: Make a Reservation

**Step-by-step conversation:**
```
You: I'd like to book a parking spot
Bot: Let's book your spot. What's your first name?

You: Alice
Bot: Thanks! And your last name?

You: Johnson
Bot: What's your car's licence plate number?

You: XYZ789
Bot: When should the reservation start? (e.g. 2026-11-20 09:00)

You: 2026-12-01 09:00
Bot: And when should it end? (e.g. 2026-11-20 18:00)

You: 2026-12-01 17:00
Bot: Here's your reservation request #XX:
     Name: Alice Johnson
     Plate: XYZ789
     From: 2026-12-01T09:00
     To: 2026-12-01T17:00
     Status: pending administrator confirmation.
```

### Test 4: Approve Reservation

**Option A: Using Admin Dashboard**
1. Open http://127.0.0.1:8000/admin
2. Find your reservation
3. Click "Approve" button

**Option B: Using Admin CLI**
```bash
python -m app.admin.cli
```
Follow the prompts to approve.

**Option C: Using API**
```bash
curl -X POST http://127.0.0.1:8000/admin/api/reservations/XX/decision \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: your-admin-token-here" \
  -d "{\"decision\": \"approve\", \"reason\": \"All good!\"}"
```

### Test 5: Check Status

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"What is the status of my booking?\", \"session_id\": \"test-1\"}"
```

**Expected Response:** "Reservation #XX status: confirmed."

### Test 6: Verify MCP File

```bash
cat confirmed_reservations.txt
```

**Expected:** Your reservation should be in the file:
```
Alice Johnson | XYZ789 | 2026-12-01T09:00 to 2026-12-01T17:00 | 2026-XX-XXT...
```

---

## Health Checks

### Check Web Server
```bash
curl http://127.0.0.1:8000/health
```

**Expected Output:**
```json
{
  "status": "ok",
  "vector_backend": "memory",
  "llm": "offline"
}
```

### Check MCP Server
```bash
curl http://127.0.0.1:8001/health
```

**Expected Output:**
```json
{
  "status": "healthy",
  "service": "citypark-mcp-server",
  "version": "1.0.0",
  "file_path": "...",
  "file_exists": true
}
```

---

## Troubleshooting

### Problem: "Module not found" error

**Solution:**
```bash
# Make sure virtual environment is activated
.venv\Scripts\activate

# Reinstall dependencies
pip install -r requirements.txt
```

### Problem: Port already in use (8000 or 8001)

**Solution:**
```bash
# Find and kill the process (Windows)
netstat -ano | findstr :8000
taskkill /PID <process-id> /F

# Or use different ports
uvicorn app.web.server:app --port 8002
python start_mcp_server.py  # Edit file to change port
```

### Problem: MCP server not reachable

**Solution:**
1. Check MCP server terminal - is it running?
2. Check .env has `MCP_ENABLED=True`
3. Check firewall settings
4. Try: `curl http://127.0.0.1:8001/health`

### Problem: Admin API returns 401 Unauthorized

**Solution:**
```bash
# Check .env has ADMIN_TOKEN set
# Use that exact token in X-Admin-Token header
curl -X GET http://127.0.0.1:8000/admin/api/reservations \
  -H "X-Admin-Token: 0z382_ZUOz0D-bgK4Tli2oegqgdYzpb8qqigXdArmtc"
```

### Problem: Tests failing

**Solution:**
```bash
# Make sure no servers are running during tests
# Kill web and MCP servers, then:
pytest -v

# For specific test file:
pytest tests/test_chatbot.py -v
```

### Problem: Database locked

**Solution:**
```bash
# Close all connections, restart servers
# Or delete and recreate database
rm citypark.db
python -c "from app.chatbot import Chatbot; Chatbot()"
```

---

## Stopping the System

### Graceful Shutdown

1. **Stop Web Server:** Press `Ctrl+C` in Terminal 2
2. **Stop MCP Server:** Press `Ctrl+C` in Terminal 1
3. **Deactivate venv:** Type `deactivate`

### Force Stop (if needed)

**Windows:**
```powershell
# Find processes
Get-Process | Where-Object {$_.Path -like "*python*"}

# Kill specific processes
Stop-Process -Name python -Force
```

**Linux/Mac:**
```bash
# Find processes
ps aux | grep python

# Kill specific PID
kill <pid>

# Or kill all uvicorn processes
pkill -f uvicorn
```

---

## Directory Structure

```
vehicle-reservation/
├── app/                    # Main application code
│   ├── admin/             # Admin approval system
│   ├── web/               # Web server & UI
│   ├── data/              # Static knowledge
│   ├── chatbot.py         # Main chatbot
│   ├── config.py          # Configuration
│   └── ...
├── mcp_server/            # MCP server for persistence
│   ├── server.py          # MCP server
│   └── client.py          # MCP client
├── tests/                 # Test suite (111 tests)
├── .env                   # Environment config (gitignored)
├── .env.example           # Example config
├── citypark.db            # SQLite database
├── confirmed_reservations.txt  # MCP output file
├── requirements.txt       # Dependencies
├── start_mcp_server.py   # MCP server launcher
└── README.md             # Main documentation
```

---

## Environment Variables Reference

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `LLM_PROVIDER` | "dial" or "openai" | dial | No |
| `DIAL_API_KEY` | EPAM DIAL API key | (empty=offline) | No |
| `OPENAI_API_KEY` | OpenAI API key | (empty) | If using OpenAI |
| `ADMIN_TOKEN` | Admin API authentication | (empty) | For admin API |
| `MCP_SERVER_TOKEN` | MCP authentication | (empty) | For MCP security |
| `MCP_ENABLED` | Enable MCP persistence | True | No |
| `SQLITE_PATH` | Database file path | citypark.db | No |

---

## Quick Command Reference

```bash
# Activate environment
.venv\Scripts\activate

# Start MCP server
python start_mcp_server.py

# Start web server
uvicorn app.web.server:app --reload

# Run admin CLI
python -m app.admin.cli

# Run tests
pytest -v

# Run specific test
pytest tests/test_chatbot.py -v

# Check health
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8001/health

# View confirmed reservations
cat confirmed_reservations.txt

# View database
sqlite3 citypark.db "SELECT * FROM reservations ORDER BY id DESC LIMIT 5;"
```

---

## Next Steps

1. ✅ Follow Quick Start (3 steps)
2. ✅ Test with simple questions
3. ✅ Make a test reservation
4. ✅ Approve via admin interface
5. ✅ Verify file persistence
6. 🚀 Use with real API keys (optional)
7. 📊 Run load tests (optional)

---

## Support

- **Documentation:** See README.md, FIX_COMPLETION_REPORT.md
- **Test Results:** See test_results.txt
- **Demo Results:** See RUNNING_DEMO.md

All 111 tests passing ✅ | Production ready 🚀
