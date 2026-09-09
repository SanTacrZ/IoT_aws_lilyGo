#!/bin/bash
set -e
dnf install -y python3-pip
pip3 install --break-system-packages flask

mkdir -p /opt/flask
cat > /opt/flask/app.py <<'PYEOF'
from flask import Flask, request

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        return f"POST recibido: {data}", 201
    return "GET recibido. Usa POST para enviar datos."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
PYEOF

cat > /etc/systemd/system/flask-s2.service <<'SVCEOF'
[Unit]
Description=Flask s2 en puerto 80
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/flask/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable --now flask-s2
